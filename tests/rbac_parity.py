"""RBAC parity: who passes every hard-coded admin/staff check today.

Spec docs/superpowers/specs/2026-11-03-rbac-design.md, build step 1. The RBAC work replaces ~200 checks of
`is_admin()` / `is_staff()` / `is_admin_or_staff()` and the eight legacy permission strings with a permission
catalogue. This file records, against the code as it stood before that work began, whether each of five callers is
allowed or denied at each site. Every later step must keep this table green. The one intended change is RB5:
`change user roles` goes away for anyone but Admin, and no row here gives it to anyone else.

The five callers, in the order of every expectation string:

    anon     not logged in
    user     a verified local account with no role
    staff    the Staff role exactly as `flask init-db` seeds it (app/roles.py)
    admin    the Admin role exactly as `flask init-db` seeds it, and not user 1
    founder  user 1, who passes every check (fact 347)

An expectation is one character per caller: A = allowed, D = denied, - = not applicable (the row isn't
generated). A row whose probe meets a 5xx fails as an error rather than recording D, so a broken setup can't
pass as a denial.

"target" rows turn the table around: the caller column names the role held by the account acted ON, and the
acting account is fixed (it is named in the row). They pin the rules that protect staff and admins from being
banned or blocked, which RB6 replaces.

Sites with no row, and why:

- `admin_users_add`'s 'admin'-registration check and `admin_user_edit`'s role field. Only holders of
  `administer all users` reach either, and of the five callers only admin and founder hold it, both admins.
  Neither site tells the five apart; the RBAC work gives them their own rows when scoped grants exist.
- `is_instance_admin()` in the inbox (block, unblock, delete, restore, poll answers). That is a remote
  instance's own admin (InstanceRole), which the spec leaves unchanged.
- `ban_user`'s and `delete_profile`'s "user with role permissions" flashes: messages, no decision.
- `ban_visibility_permission` in `domain.domains` and `tag.tags`: computed, and read by no template.
- The dillo theme's side pane and `feed/_feed_table_row.html`: copies of checks rowed elsewhere
  (`tpl.side_pane.fixup_from_remote`, `feed.*`).
- `community_mod_list.html`'s staff branches: the route itself admits only owners, moderators and admins
  (`community.mod_list`), so staff never renders them.
"""
import re
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import Callable

import pytest
from flask import current_app, session as flask_session
from flask_login import login_user
from flask_wtf.csrf import generate_csrf

from app import db
from app.constants import POST_STATUS_REVIEWING
from app.models import (BlockedImage, CmsPage, CommunityBan, CommunityMember, CommunityWikiPage, Emoji, ModLog,
                        Report, Role, RssFeed, Site, SiteDomain, Topic, utcnow)
from app.roles import seed_default_roles
from app.storefront import clear_domain_cache
from tests.conftest import forget_login
from tests.factories import (make_activitypub_log, make_chat_message, make_community, make_community_flair,
                             make_community_member, make_conversation, make_domain, make_file, make_instance,
                             make_local_feed, make_post, make_post_reply, make_user, make_user_registration)

ACTORS = ('anon', 'user', 'staff', 'admin', 'founder')
SHOP = 'shop.parity.example'


# --------------------------------------------------------------------------
# The world every row runs in
# --------------------------------------------------------------------------

def build_world():
    """Ids are fixed because the sequences restart at 1 for every test (tests/README.md)."""
    server = current_app.config['SERVER_NAME']
    local = make_instance(server, software='piefed')                      # instance 1
    peer = make_instance('peer.example', software='piefed')               # instance 2
    db.session.add(SiteDomain(domain=server, scheme='https', is_default=True))   # before the roles: Staff is limited to it
    db.session.commit()
    seed_default_roles()
    db.session.commit()
    staff_role = Role.query.filter_by(name='Staff').one()
    admin_role = Role.query.filter_by(name='Admin').one()

    founder = make_user(local, 'founder', local=True, with_keys=True)
    assert founder.id == 1
    plain = make_user(local, 'plain', local=True, with_keys=True)
    staff = make_user(local, 'staffer', local=True, with_keys=True)
    staff.roles.append(staff_role)
    admin = make_user(local, 'adminer', local=True, with_keys=True)
    admin.roles.append(admin_role)
    owner = make_user(local, 'owner', local=True)
    target = make_user(local, 'target', local=True)
    remote = make_user(peer, 'remy')
    aged = utcnow() - timedelta(days=60)   # past every "account too new" check; the invite row undoes it
    for user in (founder, plain, staff, admin, owner, target):
        user.ap_profile_id = f'https://{server}/u/{user.user_name}'   # as registration stores it
        user.created = aged
    remote.created = aged
    db.session.commit()

    community = make_community('parity')
    community.user_id = owner.id
    db.session.add(CommunityMember(user_id=owner.id, community_id=community.id, is_moderator=True,
                                   is_owner=True))
    make_community_member(target, community)
    db.session.commit()

    post = make_post(community, owner, f'https://{server}/post/1', title='the parity post')
    reply = make_post_reply(post, target, 'the parity reply')
    flair = make_community_flair(community, 'parity flair')
    report = Report(reasons='spam', description='', type=1, reporter_id=target.id, suspect_post_id=post.id,
                    suspect_user_id=owner.id, in_community_id=community.id, source_instance_id=local.id)
    db.session.add(report)
    wiki = CommunityWikiPage(community_id=community.id, title='Parity page', slug='parity-page',
                             body='body', body_html='<p>body</p>', who_can_edit=0)
    db.session.add(wiki)
    conversation = make_conversation(owner, target)
    message = make_chat_message(owner, target, f'https://{server}/chat/1', body='the parity message')
    message.conversation_id = conversation.id
    message.body_html = '<p>the parity message</p>'   # every product path stores both
    db.session.commit()
    message.ap_id = f'https://{server}/private_message/{message.id}'   # as chat_util names a local DM
    feed = make_local_feed('parityfeed', public=False, user=owner)
    topic = Topic(machine_name='paritytopic', name='Parity topic')
    db.session.add(topic)
    page = CmsPage(url='/parity', title='Parity', body='b', body_html='<p>b</p>')
    db.session.add(page)
    emoji = Emoji(url='https://example.com/e.png', token=':parity:', category='parity', aliases='parity',
                  instance_id=local.id)
    db.session.add(emoji)
    blocked_image = BlockedImage(hash='0' * 256, file_name='parity.png', note='parity')
    db.session.add(blocked_image)
    db.session.commit()
    activity = make_activitypub_log('https://peer.example/activities/1')
    file = make_file()
    registration = make_user_registration(target)
    db.session.get(Site, 1).private_instance = True   # these ask who gets a page, not whether strangers may browse
    db.session.add(SiteDomain(domain=SHOP, scheme='https', federates=False))
    db.session.commit()
    clear_domain_cache()
    shop = SiteDomain.query.filter_by(domain=SHOP).one()

    return SimpleNamespace(
        local=local, peer=peer, founder=founder, plain=plain, staff=staff, admin=admin, owner=owner,
        target=target, remote=remote, community=community, post=post, reply=reply, flair=flair, report=report,
        wiki=wiki, conversation=conversation, message=message, feed=feed, topic=topic, page=page, emoji=emoji,
        blocked_image=blocked_image, activity=activity, file=file, registration=registration, shop=shop,
        actors={'anon': None, 'user': plain, 'staff': staff, 'admin': admin, 'founder': founder})


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------

class Probe:
    """What one row does as one caller. `run` returns True for allowed and False for denied."""

    def __init__(self, ctx):
        self.ctx = ctx

    @property
    def w(self):
        return self.ctx.world


_LAST = {}


def _fail_on_5xx(response, what):
    _LAST['seen'] = f'{response.status_code} {response.headers.get("Location", "")}'.strip()
    assert response.status_code < 500, f'{what} answered {response.status_code}: the row\'s setup is broken'
    return response


def _denied_by_gate(response) -> bool:
    """The ways a gate says no: a 401/403, or a redirect to the permission-denied or login page."""
    if response.status_code in (401, 403):
        return True
    if response.status_code in (301, 302, 303, 307, 308):
        location = response.headers.get('Location', '')
        return '/auth/permission_denied' in location or '/auth/login' in location
    return False


def gate(path):
    """A route behind a decorator or an early abort: allowed unless it answers with a denial."""
    def run(ctx):
        response = _fail_on_5xx(ctx.client.get(path(ctx.world) if callable(path) else path), path)
        return not _denied_by_gate(response)
    return run


def page(path):
    """A page whose in-body check redirects, flashes or 404s on refusal: allowed only on a 200."""
    def run(ctx):
        response = _fail_on_5xx(ctx.client.get(path(ctx.world) if callable(path) else path), path)
        return response.status_code == 200
    return run


def shows(path, marker, headers=None):
    """A page where the check decides whether `marker` appears. A caller the page's gate refuses sees nothing."""
    def run(ctx):
        response = _fail_on_5xx(ctx.client.get(path(ctx.world) if callable(path) else path, headers=headers), path)
        if _denied_by_gate(response):
            return False
        assert response.status_code == 200, f'{path} answered {response.status_code}'
        text = response.get_data(as_text=True)
        return (marker(ctx.world) if callable(marker) else marker) in text
    return run


def shows_as_founder(path, marker):
    """A target row: the founder loads `path(world, actor)`, about the caller's account, and looks for `marker`."""
    def run(ctx):
        _login(ctx.client, ctx.world.founder)
        forget_login()
        response = _fail_on_5xx(ctx.client.get(path(ctx.world, ctx.actor)), path)
        assert response.status_code == 200, f'answered {response.status_code}'
        return marker in response.get_data(as_text=True)
    return run


def redirects_to(path, fragment):
    """A route that sends a permitted caller on to `fragment` and answers everyone else some other way."""
    def run(ctx):
        response = _fail_on_5xx(ctx.client.get(path(ctx.world) if callable(path) else path), path)
        return response.status_code in (301, 302, 303) and fragment in response.headers.get('Location', '')
    return run


def post_effect(path, effect, data=None):
    """A POST action: allowed when `effect(world)` holds afterwards (read fresh from the database)."""
    def run(ctx):
        form = dict(data(ctx.world) if callable(data) else (data or {}))
        form['csrf_token'] = ctx.csrf
        response = _fail_on_5xx(ctx.client.post(path(ctx.world) if callable(path) else path, data=form), path)
        db.session.expire_all()
        return bool(effect(ctx.world, response))
    return run


def post_gate(path, data=None):
    """A POST whose refusal is a denial response, and whose acceptance is anything else."""
    def run(ctx):
        form = dict(data(ctx.world) if callable(data) else (data or {}))
        form['csrf_token'] = ctx.csrf
        response = _fail_on_5xx(ctx.client.post(path(ctx.world) if callable(path) else path, data=form), path)
        return not _denied_by_gate(response)
    return run


def field_enabled(path, name, headers=None):
    """A form every caller may load, where the check decides whether field `name` is offered and enabled. A field
    the template leaves out for this caller counts as not enabled; a field missing for everyone fails the row's
    "someone is allowed" check."""
    def run(ctx):
        response = _fail_on_5xx(ctx.client.get(path(ctx.world) if callable(path) else path, headers=headers), path)
        assert response.status_code == 200, f'{path} answered {response.status_code}'
        tag = re.search(rf'<input[^>]*name="{name}"[^>]*>', response.get_data(as_text=True))
        return tag is not None and 'disabled' not in tag.group(0)
    return run


def call(fn):
    """A function row: `fn(world, user)` inside a request with the caller logged in; truthy is allowed."""
    def run(ctx):
        with ctx.app.test_request_context('/'):
            if ctx.user is not None:
                login_user(ctx.user)
            ctx.app.preprocess_request()   # g.site, g.admin_ids: what a request's before_request sets
            return bool(fn(ctx.world, ctx.user))
    return run


def refuses(fn, *messages):
    """A function that refuses by raising: allowed unless it raises an exception whose text is one of `messages`.
    Any other exception is a broken row, not a denial."""
    def run(ctx):
        with ctx.app.test_request_context('/'):
            if ctx.user is not None:
                login_user(ctx.user)
            ctx.app.preprocess_request()   # g.site, g.admin_ids: what a request's before_request sets
            try:
                fn(ctx.world, ctx.user)
            except Exception as refusal:   # the API layer refuses with bare Exception(message)
                if str(refusal) in messages:
                    return False
                raise
            return True
    return run


@dataclass(frozen=True)
class Row:
    id: str
    expect: str
    run: Callable
    setup: Callable = None   # setup(ctx), before the caller logs in



# --------------------------------------------------------------------------
# Row setups and shared constants
# --------------------------------------------------------------------------




def site_columns(**columns):
    def setup(ctx):
        site = db.session.get(Site, 1)
        for name, value in columns.items():
            setattr(site, name, value)
        db.session.commit()
    return setup


def mod2(ctx):
    """A second moderator, not an owner: what make_owner promotes."""
    w = ctx.world
    w.mod2 = make_user(w.local, 'mod2', local=True)
    db.session.add(CommunityMember(user_id=w.mod2.id, community_id=w.community.id, is_moderator=True))
    db.session.commit()


def ban_target(ctx):
    db.session.add(CommunityBan(user_id=ctx.world.target.id, community_id=ctx.world.community.id,
                                banned_by=ctx.world.owner.id, reason='parity'))
    db.session.commit()


def rss_feed(ctx):
    ctx.monkeypatch.setitem(ctx.app.config, 'RSS_FEEDS', True)
    ctx.world.rss = RssFeed(community_id=ctx.world.community.id, title='parity rss', url='https://example.com/rss',
                            check_frequency=60)
    db.session.add(ctx.world.rss)
    db.session.commit()


def deleted_post(ctx):
    ctx.world.post.deleted = True
    ctx.world.post.deleted_by = ctx.world.owner.id
    db.session.commit()


def deleted_reply(ctx):
    ctx.world.reply.deleted = True
    ctx.world.reply.deleted_by = ctx.world.target.id
    db.session.commit()


def reviewing_post(ctx):
    ctx.world.post.status = POST_STATUS_REVIEWING
    db.session.commit()


def second_owner(ctx):
    mod2(ctx)
    membership(ctx.world, ctx.world.mod2.id).is_owner = True
    db.session.commit()


def author_is_caller(ctx, thing):
    """The edit forms open only to the author, so the author is the caller (not a moderator, not the owner)."""
    user = ctx.world.actors[ctx.actor]
    if user is not None:
        getattr(ctx.world, thing).user_id = user.id
        db.session.commit()


def recent_accounts(ctx):
    for user in ctx.world.actors.values():
        if user is not None:
            user.created = utcnow()
    db.session.commit()


def membership(w, user_id):
    return CommunityMember.query.filter_by(community_id=w.community.id, user_id=user_id).first()


HX = {'HX-Request': 'true'}
CID = lambda w: w.community.id   # noqa: E731


PID = lambda w: w.post.id   # noqa: E731
RID = lambda w: w.reply.id   # noqa: E731



CONV = lambda w: w.conversation.id   # noqa: E731






def profile_extras(ctx):
    """What the profile's staff panels list: a same-IP account, a private-mods community, a hidden subscription."""
    w = ctx.world
    w.target.ip_address = w.owner.ip_address = '10.9.9.9'
    secret = make_community('secretmods')
    secret.private_mods = True
    db.session.add(CommunityMember(user_id=w.target.id, community_id=secret.id, is_moderator=True))
    joined = make_community('joinedonly')
    make_community_member(w.target, joined)
    w.target.show_subscribed_communities = False
    db.session.commit()


def remote_community(ctx):
    w = ctx.world
    w.far = make_community('farcomm', host='peer.example')
    w.far.ap_id = 'farcomm@peer.example'
    w.far.instance_id = w.peer.id
    db.session.commit()


def inactive_mods(ctx):
    ctx.world.owner.last_seen = utcnow() - timedelta(days=90)
    db.session.commit()


def link_domain(ctx):
    ctx.world.link_domain = make_domain('links.example')


def caller_joins(ctx):
    """The menu renders its body, and so its links, only for someone who has joined a community."""
    user = ctx.world.actors[ctx.actor]
    if user is not None:
        make_community_member(user, ctx.world.community)


def caller_profile(w, actor):
    return f'/u/{w.actors[actor].user_name}'


PROFILE = '/u/target'





def recent_owner(ctx):
    ctx.world.owner.created = utcnow()
    db.session.commit()


def modlog_entry(ctx):
    db.session.add(ModLog(user_id=ctx.world.owner.id, type='mod', action='ban_user', reason='the parity entry',
                          public=False))
    db.session.commit()


def staff2(ctx):
    """A second member of staff, the acting account of the API ban target row."""
    w = ctx.world
    w.staff2 = make_user(w.local, 'staffer2', local=True)
    w.staff2.roles.append(Role.query.filter_by(name='Staff').one())
    db.session.commit()


def low_reply(ctx):
    user = ctx.world.actors[ctx.actor]
    if user is not None:
        user.reply_hide_threshold = -5
    ctx.world.reply.score = -10
    ctx.world.reply.collapsible = True
    db.session.commit()


def quietly(fn, *args):
    """Call an API function whose refusal is not an exception but a no-op: what it changed is the answer."""
    try:
        return fn(*args)
    except Exception as refusal:   # a refusal surfaced as an exception counts the same as a no-op
        return refusal


# --------------------------------------------------------------------------
# Running a row
# --------------------------------------------------------------------------

def _login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def _csrf(app, client):
    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def params(rows):
    """One test per (row, caller), with a deterministic id; '-' callers are left out."""
    out = []
    for row in rows:
        assert len(row.expect) == len(ACTORS), row.id
        for actor, expected in zip(ACTORS, row.expect):
            if expected != '-':
                out.append(pytest.param(row, actor, expected, id=f'{row.id}-{actor}'))
    return out


def check(app, monkeypatch, row, actor, expected):
    """Build the world, run the row's setup, log the caller in, and compare what the row finds with the table."""
    # The pages render `form.csrf_token`, which FlaskForm declares only with CSRF on (tests/conftest.py turns it off)
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)
    world = build_world()
    ctx = SimpleNamespace(app=app, monkeypatch=monkeypatch, world=world, actor=actor)
    if row.setup:
        row.setup(ctx)
    forget_login()
    client = app.test_client()
    user = world.actors[actor]
    if user is not None:
        _login(client, user)
    ctx.client, ctx.csrf, ctx.user = client, _csrf(app, client), user
    _LAST.clear()
    actual = 'A' if row.run(ctx) else 'D'
    assert actual == expected, f'{row.id}: {actor} is {actual}, the table says {expected} ({_LAST.get("seen")})'
