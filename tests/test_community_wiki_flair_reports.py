"""The community moderation tools that are not banning: wiki, flair, reports.

Sub-project 80, slice B. Five routes here took two ids -- a community (or
actor) and a resource -- checked the caller's authority over the **community**,
and then acted on the **resource** without asking whether the two were related.
So a moderator of any community on the instance could rewrite or delete any
other community's wiki pages and flair by passing their ids (D969, D970, D971).

Eight more state-changing routes still had no `current_user.banned` check
(D972), and slice A's ratchet had passed anyway, because it only flagged a rule
that answered 200 and every one of these redirects (D973). The per-route rows
below are what actually pin them; the ratchet is a floor that notices routes
nobody wrote a row for, and it found a ninth -- `community_report` -- that the
reading never listed.
"""
from unittest.mock import patch

import pytest

from app import db
from app.models import (CommunityFlair, CommunityWikiPage,
                        CommunityWikiPageRevision, Report, User)
from tests.factories import (make_community, make_community_member,
                             make_instance, make_user)

pytestmark = pytest.mark.usefixtures('site')

REPORT_STATE_NEW = 0


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394."""
    from app.models import Instance

    existing = Instance.query.filter_by(domain=domain).first()
    return existing if existing is not None else make_instance(domain,
                                                               software=software)


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def csrf(app, client):
    """Fact 355."""
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def url(app, endpoint, **values):
    from flask import url_for

    with app.test_request_context():
        return url_for(endpoint, **values)


@pytest.fixture
def two_communities(app, db_session):
    """A moderator of `mine`, and a second community `theirs` they have no
    relationship with at all. Every cross-community row below acts as that
    moderator on a resource owned by `theirs`."""
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    moderator = make_user(local, 'modofmine', local=True)
    outsider = make_user(local, 'outsider', local=True)
    mine = make_community('mine')
    theirs = make_community('theirs')
    db.session.commit()
    make_community_member(moderator, mine, is_moderator=True)
    db.session.commit()
    return mine, theirs, moderator, outsider


@pytest.fixture
def mod_client(app, two_communities):
    mine, theirs, moderator, outsider = two_communities
    client = app.test_client()
    login(client, moderator)
    return client, csrf(app, client)


def _flair(community, text='their flair'):
    flair = CommunityFlair(community_id=community.id, flair=text,
                           text_color='#000000', background_color='#ffffff')
    db.session.add(flair)
    db.session.commit()
    return flair


def _wiki_page(community, title='Guide', slug='guide', body='original',
               who_can_edit=0):
    page = CommunityWikiPage(community_id=community.id, title=title, slug=slug,
                             body=body, body_html=f'<p>{body}</p>',
                             who_can_edit=who_can_edit)
    db.session.add(page)
    db.session.commit()
    return page


def _revision(page, community, body='revision body'):
    revision = CommunityWikiPageRevision(wiki_page_id=page.id, user_id=1,
                                         community_id=community.id,
                                         title=page.title, body=body,
                                         body_html=f'<p>{body}</p>')
    db.session.add(revision)
    db.session.commit()
    return revision


# --------------------------------------------------------------------------
# D969: a wiki page belongs to a community
# --------------------------------------------------------------------------


def test_a_moderator_cannot_edit_another_communitys_wiki_page(app,
                                                              two_communities,
                                                              mod_client):
    """D969's pin, inverted.

    `CommunityWikiPage.can_edit(user, community)` took the community as an
    ARGUMENT and never consulted `self.community_id`, so it answered "may this
    user edit some page of that community" -- while every caller takes the
    community from the URL and the page from an id. Measured:

        PROBE w1 status 200 | other community's wiki body now: HIJACKED
    """
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    page = _wiki_page(theirs)

    with patch('app.community.routes.render_template', return_value='rendered'):
        client.post(url(app, 'community.community_wiki_edit', actor=mine.name,
                        page_id=page.id),
                    data={'title': 'Guide', 'slug': 'guide', 'body': 'HIJACKED',
                          'who_can_edit': '0', 'submit': 'Save',
                          'csrf_token': token})

    db.session.expire_all()
    assert db.session.get(CommunityWikiPage, page.id).body == 'original'


def test_a_moderator_can_still_edit_their_own_communitys_wiki_page(
        app, two_communities, mod_client):
    """The control. Scoping `can_edit` must not break the feature it guards,
    and a row asserting only the refusal would pass against `return False`."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    page = _wiki_page(mine)

    with patch('app.community.routes.render_template', return_value='rendered'):
        client.post(url(app, 'community.community_wiki_edit', actor=mine.name,
                        page_id=page.id),
                    data={'title': 'Guide', 'slug': 'guide', 'body': 'rewritten',
                          'who_can_edit': '0', 'submit': 'Save',
                          'csrf_token': token})

    db.session.expire_all()
    assert db.session.get(CommunityWikiPage, page.id).body == 'rewritten'


def test_can_edit_refuses_a_page_from_another_community_directly(app,
                                                                 two_communities):
    """The method, not the route -- three templates call it too, to decide
    whether to show an edit link, and a template that offers a link the route
    refuses is its own bug."""
    mine, theirs, moderator, outsider = two_communities
    page = _wiki_page(theirs, who_can_edit=3)  # 3 = anyone may edit

    assert page.can_edit(moderator, theirs) is True
    assert page.can_edit(moderator, mine) is False, (
        'can_edit answered for the community it was handed, not the page\'s own')


def test_can_edit_refuses_when_no_community_is_given(app, two_communities):
    """`community is None` -- `actor_to_community` returns None for an actor it
    cannot resolve, and the wiki routes pass the result straight in."""
    mine, theirs, moderator, outsider = two_communities
    page = _wiki_page(mine, who_can_edit=3)

    assert page.can_edit(moderator, None) is False


def _untrusted(name='newcomer'):
    """`trustworthy()` is False only for an account that was created recently
    AND has reputation under 100 (app/models.py:1286)."""
    from app.models import utcnow

    user = make_user(instance(), name, local=True)
    user.created = utcnow()
    user.reputation = 0
    db.session.commit()
    assert user.trustworthy() is False
    return user


def _trusted(name='established'):
    """The other side of `trustworthy()`: an account is trusted unless it was
    created recently AND has reputation under 100. `make_user` makes a brand
    new account with reputation 0, so a row that wants a trusted non-moderator
    has to say so -- asserting `outsider.trustworthy() is True` fails."""
    user = make_user(instance(), name, local=True)
    user.reputation = 500
    db.session.commit()
    assert user.trustworthy() is True
    return user


@pytest.mark.parametrize('who_can_edit, untrusted, trusted, member', [
    # Each row names the answer for three DIFFERENT people, so every level is
    # separated from its neighbours by at least one of them. Asking only about
    # an outsider who is neither trustworthy nor a member -- which is what this
    # row did at first -- makes levels 0, 1 and 2 indistinguishable, and the
    # mutants widening level 0 to trustworthy and narrowing level 2 away from
    # members both survived.
    (0, False, False, False),   # moderators only
    (1, False, True, False),    # + trustworthy
    (2, False, True, True),     # + members
    (3, True, True, True),      # anyone
])
def test_each_who_can_edit_level(app, two_communities, who_can_edit, untrusted,
                                 trusted, member):
    """The four arms, each asked of the person the next level admits."""
    mine, theirs, moderator, outsider = two_communities
    page = _wiki_page(mine, who_can_edit=who_can_edit)
    newcomer = _untrusted()
    established = _trusted()
    joiner = make_user(instance(), 'joiner', local=True)
    db.session.commit()
    make_community_member(joiner, mine)
    assert not mine.is_member(established)

    assert page.can_edit(newcomer, mine) is untrusted
    assert page.can_edit(established, mine) is trusted
    assert page.can_edit(joiner, mine) is member


def test_an_anonymous_visitor_can_never_edit(app, two_communities):
    """`if user.is_anonymous: return False`, even at level 3."""
    from flask_login import AnonymousUserMixin

    mine, theirs, moderator, outsider = two_communities
    page = _wiki_page(mine, who_can_edit=3)

    assert page.can_edit(AnonymousUserMixin(), mine) is False


# --------------------------------------------------------------------------
# D970: flair belongs to a community
# --------------------------------------------------------------------------


def test_a_moderator_cannot_delete_another_communitys_flair(app,
                                                            two_communities,
                                                            mod_client):
    """D970's pin, inverted.

    `community_flair_delete` deleted `CommunityFlair.id == flair_id` outright,
    with no `community_id`, and cascaded to that flair's `post_flair` and
    `CommunityFlairBlock` rows. Measured:

        PROBE f1 status 302 | other community's flair still exists? False
    """
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    flair = _flair(theirs)

    with patch('app.community.routes.task_selector'):
        response = client.post(url(app, 'community.community_flair_delete',
                                   community_id=mine.id, flair_id=flair.id),
                               data={'csrf_token': token})

    assert response.status_code == 404
    assert db.session.get(CommunityFlair, flair.id) is not None


def test_a_moderator_cannot_rewrite_another_communitys_flair(app,
                                                             two_communities,
                                                             mod_client):
    """D970's other half.

    `db.session.get(CommunityFlair, flair_id)` took whatever id was in the URL.
    Measured:

        PROBE f2 status 302 | other community's flair text now: HIJACKED

    Under the fix the foreign id reads as absent, so the route takes its
    add-a-new-one path -- which must create the flair in THIS community, not
    touch the other one.
    """
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    flair = _flair(theirs)

    with patch('app.community.routes.task_selector'):
        with patch('app.community.routes.render_template', return_value='rendered'):
            client.post(url(app, 'community.community_flair_edit',
                            community_id=mine.id, flair_id=flair.id),
                        data={'flair': 'HIJACKED', 'text_color': '#000000',
                              'background_color': '#ffffff', 'submit': 'Save',
                              'csrf_token': token})

    db.session.expire_all()
    assert db.session.get(CommunityFlair, flair.id).flair == 'their flair'
    created = CommunityFlair.query.filter_by(community_id=mine.id).all()
    assert [row.flair for row in created] == ['HIJACKED']


def test_a_moderator_can_edit_their_own_communitys_flair(app, two_communities,
                                                         mod_client):
    """The control for the scoping fix."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    flair = _flair(mine, 'mine')

    with patch('app.community.routes.task_selector'):
        with patch('app.community.routes.render_template', return_value='rendered'):
            client.post(url(app, 'community.community_flair_edit',
                            community_id=mine.id, flair_id=flair.id),
                        data={'flair': 'renamed', 'text_color': '#111111',
                              'background_color': '#eeeeee', 'submit': 'Save',
                              'csrf_token': token})

    db.session.expire_all()
    updated = db.session.get(CommunityFlair, flair.id)
    assert (updated.flair, updated.text_color) == ('renamed', '#111111')
    assert CommunityFlair.query.count() == 1, 'a duplicate was added'


def test_a_moderator_can_delete_their_own_communitys_flair(app,
                                                           two_communities,
                                                           mod_client):
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    flair = _flair(mine, 'mine')

    with patch('app.community.routes.task_selector') as task:
        response = client.post(url(app, 'community.community_flair_delete',
                                   community_id=mine.id, flair_id=flair.id),
                               data={'csrf_token': token})

    assert response.status_code == 302
    assert db.session.get(CommunityFlair, flair.id) is None
    assert task.call_args.args == ('edit_community',)


def test_adding_a_flair_gives_it_an_ap_id(app, two_communities, mod_client):
    """A new flair is committed once to get an id and again to store the
    `ap_id` built from it -- the identifier peers use, so a flair without one
    cannot federate."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client

    with patch('app.community.routes.task_selector'):
        with patch('app.community.routes.render_template', return_value='rendered'):
            client.post(url(app, 'community.community_flair_edit',
                            community_id=mine.id, flair_id=0),
                        data={'flair': 'new one', 'text_color': '#000000',
                              'background_color': '#ffffff', 'submit': 'Save',
                              'csrf_token': token})

    created = CommunityFlair.query.filter_by(community_id=mine.id).one()
    assert created.ap_id.endswith(f'/tag/{created.id}')


def test_an_outsider_cannot_touch_flair(app, two_communities):
    """The route's own authorization, separate from the scoping."""
    mine, theirs, moderator, outsider = two_communities
    flair = _flair(mine, 'mine')
    client = app.test_client()
    login(client, outsider)
    token = csrf(app, client)

    edit = client.post(url(app, 'community.community_flair_edit',
                           community_id=mine.id, flair_id=flair.id),
                       data={'flair': 'x', 'text_color': '#000000',
                             'background_color': '#ffffff', 'submit': 'Save',
                             'csrf_token': token})
    delete = client.post(url(app, 'community.community_flair_delete',
                             community_id=mine.id, flair_id=flair.id),
                         data={'csrf_token': token})

    assert (edit.status_code, delete.status_code) == (401, 401)
    db.session.expire_all()
    assert db.session.get(CommunityFlair, flair.id).flair == 'mine'


# --------------------------------------------------------------------------
# D971: a revision belongs to a page
# --------------------------------------------------------------------------


def test_a_revision_from_another_page_cannot_be_viewed(app, two_communities,
                                                       mod_client):
    """D971's pin, inverted, for the read side.

    The page is scoped by `community_id` and the revision was fetched with a
    bare `db.session.get`, so any revision on the instance could be displayed
    through any page -- including from a community the reader cannot see.
    """
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    _wiki_page(mine, slug='guide')
    other_page = _wiki_page(theirs, slug='theirs-guide')
    other_revision = _revision(other_page, theirs, body='their secret draft')

    with patch('app.community.routes.render_template', return_value='rendered'):
        response = client.get(url(app, 'community.community_wiki_view_revision',
                                  actor=mine.name, slug='guide',
                                  revision_id=other_revision.id))

    assert response.status_code == 404


def test_a_revision_from_another_page_cannot_be_reverted_into_this_one(
        app, two_communities, mod_client):
    """D971's write side, and the sharper half: reverting copies the
    revision's body INTO this page, so an unscoped revision id was a way to
    pull any content on the instance into a page you control."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    page = _wiki_page(mine, slug='guide', body='mine')
    other_page = _wiki_page(theirs, slug='theirs-guide')
    other_revision = _revision(other_page, theirs, body='their secret draft')

    response = client.get(url(app, 'community.community_wiki_revert_revision',
                              actor=mine.name, slug='guide',
                              revision_id=other_revision.id))

    assert response.status_code == 404
    db.session.expire_all()
    assert db.session.get(CommunityWikiPage, page.id).body == 'mine'


def test_a_revision_of_this_page_can_be_viewed_and_reverted(app,
                                                            two_communities,
                                                            mod_client):
    """The control for both. Reverting writes the old body back AND records a
    new revision, so the history is append-only -- asserting only the body
    would pass against a fix that dropped the audit trail."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    page = _wiki_page(mine, slug='guide', body='current')
    revision = _revision(page, mine, body='the old text')

    with patch('app.community.routes.render_template', return_value='rendered'):
        viewed = client.get(url(app, 'community.community_wiki_view_revision',
                                actor=mine.name, slug='guide',
                                revision_id=revision.id))
    reverted = client.get(url(app, 'community.community_wiki_revert_revision',
                              actor=mine.name, slug='guide',
                              revision_id=revision.id))

    assert viewed.status_code == 200
    assert reverted.status_code == 302
    db.session.expire_all()
    assert db.session.get(CommunityWikiPage, page.id).body == 'the old text'
    assert CommunityWikiPageRevision.query.filter_by(wiki_page_id=page.id).count() == 2


def test_a_missing_page_is_a_404(app, two_communities, mod_client):
    """`if page is None ... abort(404)` -- the slug comes from the URL."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    page = _wiki_page(mine, slug='guide')
    revision = _revision(page, mine)

    response = client.get(url(app, 'community.community_wiki_view_revision',
                              actor=mine.name, slug='no-such-slug',
                              revision_id=revision.id))

    assert response.status_code == 404


# --------------------------------------------------------------------------
# D972: the banned check, per route
# --------------------------------------------------------------------------


def test_a_banned_moderator_cannot_add_a_wiki_page(app, two_communities):
    """D972, one row per route, because slice A's ratchet cannot drive these:
    each needs a valid form body, a real slug or a resolvable actor before any
    work happens, so the request stops early whether or not the guard is there.
    D973 is that finding."""
    mine, theirs, moderator, outsider = two_communities
    moderator.banned = True
    db.session.commit()
    client = app.test_client()
    login(client, moderator)
    token = csrf(app, client)

    with patch('app.community.routes.render_template', return_value='rendered'):
        client.post(url(app, 'community.community_wiki_add', actor=mine.name),
                    data={'title': 'New', 'slug': 'new', 'body': 'text',
                          'who_can_edit': '0', 'submit': 'Save',
                          'csrf_token': token})

    assert CommunityWikiPage.query.count() == 0


def test_a_banned_moderator_cannot_edit_a_wiki_page(app, two_communities):
    mine, theirs, moderator, outsider = two_communities
    page = _wiki_page(mine, body='original')
    moderator.banned = True
    db.session.commit()
    client = app.test_client()
    login(client, moderator)
    token = csrf(app, client)

    with patch('app.community.routes.render_template', return_value='rendered'):
        client.post(url(app, 'community.community_wiki_edit', actor=mine.name,
                        page_id=page.id),
                    data={'title': 'Guide', 'slug': 'guide', 'body': 'changed',
                          'who_can_edit': '0', 'submit': 'Save',
                          'csrf_token': token})

    db.session.expire_all()
    assert db.session.get(CommunityWikiPage, page.id).body == 'original'


def test_a_banned_moderator_cannot_revert_a_wiki_page(app, two_communities):
    mine, theirs, moderator, outsider = two_communities
    page = _wiki_page(mine, slug='guide', body='current')
    revision = _revision(page, mine, body='the old text')
    moderator.banned = True
    db.session.commit()
    client = app.test_client()
    login(client, moderator)

    client.get(url(app, 'community.community_wiki_revert_revision',
                   actor=mine.name, slug='guide', revision_id=revision.id))

    db.session.expire_all()
    assert db.session.get(CommunityWikiPage, page.id).body == 'current'


@pytest.mark.parametrize('endpoint', [
    'community.community_flair_edit',
    'community.community_flair_delete',
])
def test_a_banned_moderator_cannot_touch_flair(app, two_communities, endpoint):
    mine, theirs, moderator, outsider = two_communities
    flair = _flair(mine, 'mine')
    moderator.banned = True
    db.session.commit()
    client = app.test_client()
    login(client, moderator)
    token = csrf(app, client)

    with patch('app.community.routes.task_selector'):
        with patch('app.community.routes.render_template', return_value='rendered'):
            client.post(url(app, endpoint, community_id=mine.id,
                            flair_id=flair.id),
                        data={'flair': 'changed', 'text_color': '#000000',
                              'background_color': '#ffffff', 'submit': 'Save',
                              'csrf_token': token})

    db.session.expire_all()
    surviving = db.session.get(CommunityFlair, flair.id)
    assert surviving is not None and surviving.flair == 'mine'


def test_a_banned_user_cannot_report_a_community(app, two_communities):
    """The ninth route, found by the strengthened ratchet rather than by
    reading -- `community_report` has no authorization construct in it, so the
    survey that listed the other eight never saw it. Every report raises a
    Notification for the instance admin, so a banned account could flood the
    admin queue."""
    mine, theirs, moderator, outsider = two_communities
    outsider.banned = True
    db.session.commit()
    client = app.test_client()
    login(client, outsider)
    token = csrf(app, client)

    with patch('app.community.routes.render_template', return_value='rendered'):
        client.post(url(app, 'community.community_report', community_id=mine.id),
                    data={'reasons': ['1'], 'description': 'spam',
                          'submit': 'Report', 'csrf_token': token})

    assert Report.query.count() == 0


# --------------------------------------------------------------------------
# The report handlers
# --------------------------------------------------------------------------


def _report(community, reporter):
    report = Report(reasons='spam', description='original description',
                    type=0, reporter_id=reporter.id,
                    in_community_id=community.id, status=REPORT_STATE_NEW,
                    source_instance_id=1)
    db.session.add(report)
    db.session.commit()
    return report


@pytest.mark.parametrize('endpoint, extra', [
    ('community.community_moderate_report_escalate', {'reason': 'needs admin'}),
    ('community.community_moderate_report_resolve', {}),
    ('community.community_moderate_report_ignore', {}),
])
def test_a_banned_moderator_cannot_act_on_a_report(app, two_communities,
                                                   endpoint, extra):
    mine, theirs, moderator, outsider = two_communities
    report = _report(mine, outsider)
    moderator.banned = True
    db.session.commit()
    client = app.test_client()
    login(client, moderator)
    token = csrf(app, client)

    with patch('app.community.routes.render_template', return_value='rendered'):
        client.post(url(app, endpoint, community_id=mine.id,
                        report_id=report.id),
                    data={'submit': 'Go', 'csrf_token': token, **extra})

    db.session.expire_all()
    assert db.session.get(Report, report.id).status == REPORT_STATE_NEW


@pytest.mark.parametrize('endpoint, extra', [
    ('community.community_moderate_report_escalate', {'reason': 'needs admin'}),
    ('community.community_moderate_report_resolve', {}),
    ('community.community_moderate_report_ignore', {}),
])
def test_an_outsider_cannot_act_on_a_report(app, two_communities, endpoint,
                                            extra):
    mine, theirs, moderator, outsider = two_communities
    report = _report(mine, outsider)
    client = app.test_client()
    login(client, outsider)
    token = csrf(app, client)

    with patch('app.community.routes.render_template', return_value='rendered'):
        response = client.post(url(app, endpoint, community_id=mine.id,
                                   report_id=report.id),
                               data={'submit': 'Go', 'csrf_token': token, **extra})

    assert response.status_code == 401
    db.session.expire_all()
    assert db.session.get(Report, report.id).status == REPORT_STATE_NEW


def test_escalating_a_report_notifies_the_admin_and_records_the_reason(
        app, two_communities, mod_client):
    """Escalation moves the report to the instance admins, so both halves
    matter: the status, and the Notification that is the only thing telling
    them to look."""
    from app.models import Notification

    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    report = _report(mine, outsider)

    with patch('app.community.routes.render_template', return_value='rendered'):
        response = client.post(
            url(app, 'community.community_moderate_report_escalate',
                community_id=mine.id, report_id=report.id),
            data={'reason': 'this needs an admin', 'submit': 'Escalate',
                  'csrf_token': token})

    assert response.status_code == 302
    db.session.expire_all()
    updated = db.session.get(Report, report.id)
    assert updated.status != REPORT_STATE_NEW
    assert updated.description == 'this needs an admin'
    notification = Notification.query.filter_by(user_id=1).one()
    assert notification.title == 'Escalated report'


def test_the_escalate_form_is_prefilled_with_the_existing_description(
        app, two_communities, mod_client):
    """The GET arm -- the moderator edits what the reporter wrote rather than
    starting from nothing."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    report = _report(mine, outsider)

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get(url(app, 'community.community_moderate_report_escalate',
                       community_id=mine.id, report_id=report.id))

    assert render.call_args.kwargs['form'].reason.data == 'original description'


@pytest.mark.parametrize('action', ['resolve', 'ignore'])
def test_a_report_from_another_community_cannot_be_resolved_or_ignored(
        app, two_communities, mod_client, action):
    """`filter_by(in_community_id=community.id, ...)` on the other two
    handlers. Escalate had this row and these two did not, so the mutant
    dropping the scope from `resolve` survived -- a moderator of any community
    could clear any other community's queue."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    report = _report(theirs, outsider)

    with patch('app.community.routes.render_template', return_value='rendered'):
        response = client.post(
            url(app, f'community.community_moderate_report_{action}',
                community_id=mine.id, report_id=report.id),
            data={'submit': 'Go', 'csrf_token': token})

    assert response.status_code == 404
    db.session.expire_all()
    assert db.session.get(Report, report.id).status == REPORT_STATE_NEW


def test_a_report_from_another_community_cannot_be_escalated(app,
                                                             two_communities,
                                                             mod_client):
    """`Report.query.filter_by(in_community_id=community.id, id=report_id,
    status=REPORT_STATE_NEW)` -- the report handlers scope correctly, unlike
    the flair and wiki routes. Pinned so that stays true."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    report = _report(theirs, outsider)

    with patch('app.community.routes.render_template', return_value='rendered'):
        client.post(url(app, 'community.community_moderate_report_escalate',
                        community_id=mine.id, report_id=report.id),
                    data={'reason': 'x', 'submit': 'Escalate',
                          'csrf_token': token})

    db.session.expire_all()
    assert db.session.get(Report, report.id).status == REPORT_STATE_NEW


# --------------------------------------------------------------------------
# Resolving and ignoring: the counters, and the sweep
# --------------------------------------------------------------------------


def _reported_post(community, author):
    from app.models import Post

    post = Post(user_id=author.id, community_id=community.id, title='reported',
                ap_id='https://test.piefed.local/post/reported', reports=3)
    db.session.add(post)
    db.session.commit()
    return post


def _reported_reply(community, author, post):
    from app.models import PostReply

    reply = PostReply(user_id=author.id, post_id=post.id,
                      community_id=community.id, body='reported',
                      ap_id='https://test.piefed.local/comment/reported',
                      reports=3)
    db.session.add(reply)
    db.session.commit()
    return reply


@pytest.mark.parametrize('subject', ['post', 'reply', 'user'])
@pytest.mark.parametrize('action, status_name, counter', [
    ('resolve', 'REPORT_STATE_RESOLVED', 0),
    ('ignore', 'REPORT_STATE_DISCARDED', -1),
])
def test_acting_on_a_report_resets_the_subjects_counter(app, two_communities,
                                                        mod_client, subject,
                                                        action, status_name,
                                                        counter):
    """The three-way `if suspect_post_reply_id / elif suspect_post_id / elif
    suspect_user_id` in both handlers, over both actions.

    The counter values differ and that is the point: resolving sets it to **0**
    -- handled, may be reported again -- and ignoring sets it to **-1**, which
    is how the rest of the codebase marks "do not surface this again". A row
    that only checked the report's status would miss either.
    """
    from app import constants

    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    post = _reported_post(mine, outsider)
    reply = _reported_reply(mine, outsider, post)
    outsider.reports = 3
    db.session.commit()

    report = _report(mine, outsider)
    if subject == 'post':
        report.suspect_post_id = post.id
    elif subject == 'reply':
        report.suspect_post_reply_id = reply.id
    else:
        report.suspect_user_id = outsider.id
    db.session.commit()

    with patch('app.community.routes.render_template', return_value='rendered'):
        response = client.post(
            url(app, f'community.community_moderate_report_{action}',
                community_id=mine.id, report_id=report.id),
            data={'submit': 'Go', 'csrf_token': token})

    assert response.status_code == 302
    db.session.expire_all()
    assert db.session.get(Report, report.id).status == getattr(constants, status_name)
    subject_row = {'post': db.session.get(type(post), post.id),
                   'reply': db.session.get(type(reply), reply.id),
                   'user': db.session.get(User, outsider.id)}[subject]
    assert subject_row.reports == counter


@pytest.mark.parametrize('action, status_name', [
    ('resolve', 'REPORT_STATE_RESOLVED'),
    ('ignore', 'REPORT_STATE_DISCARDED'),
])
@pytest.mark.parametrize('subject', ['post', 'reply'])
def test_the_sweep_moves_every_other_report_about_the_same_thing(
        app, two_communities, mod_client, action, status_name, subject):
    """`if form.also_resolve_others.data:` -- one piece of content usually
    draws several reports, and handling them one at a time leaves the queue
    full of duplicates. The sweep is asserted on a SECOND report about the same
    subject, which is the only thing that distinguishes it from the single
    update above.
    """
    from app import constants

    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    post = _reported_post(mine, outsider)
    reply = _reported_reply(mine, outsider, post)

    reports = [_report(mine, outsider) for _ in range(2)]
    for report in reports:
        if subject == 'post':
            report.suspect_post_id = post.id
        else:
            report.suspect_post_reply_id = reply.id
    db.session.commit()

    with patch('app.community.routes.render_template', return_value='rendered'):
        client.post(url(app, f'community.community_moderate_report_{action}',
                        community_id=mine.id, report_id=reports[0].id),
                    data={'also_resolve_others': 'y', 'also_ignore_others': 'y',
                          'submit': 'Go', 'csrf_token': token})

    db.session.expire_all()
    expected = getattr(constants, status_name)
    assert [db.session.get(Report, r.id).status for r in reports] == [expected, expected]


def test_without_the_sweep_only_the_one_report_moves(app, two_communities,
                                                     mod_client):
    """The false arm. Leaving the box unticked must leave the sibling reports
    for somebody to look at."""
    from app import constants

    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    post = _reported_post(mine, outsider)
    reports = [_report(mine, outsider) for _ in range(2)]
    for report in reports:
        report.suspect_post_id = post.id
    db.session.commit()

    with patch('app.community.routes.render_template', return_value='rendered'):
        client.post(url(app, 'community.community_moderate_report_resolve',
                        community_id=mine.id, report_id=reports[0].id),
                    data={'submit': 'Go', 'csrf_token': token})

    db.session.expire_all()
    assert db.session.get(Report, reports[0].id).status == constants.REPORT_STATE_RESOLVED
    assert db.session.get(Report, reports[1].id).status == REPORT_STATE_NEW


def test_the_resolve_form_renders_on_a_get(app, two_communities, mod_client):
    """The `else` of `validate_on_submit` -- the confirmation page."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    report = _report(mine, outsider)

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.community_moderate_report_resolve',
                                  community_id=mine.id, report_id=report.id))

    assert response.status_code == 200
    assert render.call_args.args == ('community/community_moderate_report_resolve.html',)


@pytest.mark.parametrize('action', ['resolve', 'ignore', 'escalate'])
def test_acting_on_a_report_that_does_not_exist_is_a_404(app, two_communities,
                                                         mod_client, action):
    """`else: abort(404)`.

    All three handlers used to fall off the end here, and Flask answers that
    with `TypeError: The view function ... did not return a valid response` --
    a 500 on the ordinary act of opening a report somebody else has already
    dealt with, or following a stale link from a notification.
    """
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client

    with patch('app.community.routes.render_template', return_value='rendered'):
        response = client.post(
            url(app, f'community.community_moderate_report_{action}',
                community_id=mine.id, report_id=999999),
            data={'reason': 'x', 'submit': 'Go', 'csrf_token': token})

    assert response.status_code == 404


def test_escalating_a_report_that_is_already_handled_is_a_404(app,
                                                              two_communities,
                                                              mod_client):
    """Escalate's query requires `status=REPORT_STATE_NEW`, so a report another
    moderator has already resolved takes the same path as a missing one -- and
    two moderators working the queue at once is the normal case."""
    from app import constants

    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    report = _report(mine, outsider)
    report.status = constants.REPORT_STATE_RESOLVED
    db.session.commit()

    with patch('app.community.routes.render_template', return_value='rendered'):
        response = client.post(
            url(app, 'community.community_moderate_report_escalate',
                community_id=mine.id, report_id=report.id),
            data={'reason': 'x', 'submit': 'Escalate', 'csrf_token': token})

    assert response.status_code == 404


# --------------------------------------------------------------------------
# community_report
# --------------------------------------------------------------------------


def test_reporting_a_community_files_it_and_notifies_the_admin(app,
                                                               two_communities):
    """Both halves: the Report row is the record, and the Notification plus the
    admin's unread counter are the only things that bring anyone to look at
    it."""
    from app.models import Notification

    mine, theirs, moderator, outsider = two_communities
    founder = db.session.get(User, 1)
    before = founder.unread_notifications or 0
    client = app.test_client()
    login(client, outsider)
    token = csrf(app, client)

    with patch('app.community.routes.render_template', return_value='rendered'):
        response = client.post(url(app, 'community.community_report',
                                   community_id=theirs.id),
                               data={'reasons': ['1'], 'description': 'spam',
                                     'submit': 'Report', 'csrf_token': token})

    assert response.status_code == 302
    report = Report.query.one()
    assert report.reporter_id == outsider.id
    # `theirs`, not `mine`: `mine` is community id 1, so a mutant hard-coding 1
    # as the suspect was invisible to this assertion.
    assert report.suspect_community_id == theirs.id
    assert theirs.id != 1
    assert report.description == 'spam'
    notification = Notification.query.filter_by(user_id=1).one()
    assert notification.title == 'A community has been reported'
    db.session.expire_all()
    assert db.session.get(User, 1).unread_notifications == before + 1


def test_the_report_form_renders_on_a_get(app, two_communities):
    mine, theirs, moderator, outsider = two_communities
    client = app.test_client()
    login(client, outsider)

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.community_report',
                                  community_id=mine.id))

    assert response.status_code == 200
    assert render.call_args.kwargs['form'] is not None


def test_reporting_a_remote_community_can_forward_the_report(app,
                                                             two_communities):
    """`if not community.is_local() and form.report_remote.data:` -- the body
    is `...` and a todo, so the only thing to assert is that a local community
    never takes the branch and a remote one with the box ticked does not
    raise."""
    mine, theirs, moderator, outsider = two_communities
    remote_community = make_community('remote_one', host='remote.example')
    # Community.is_local() is `ap_id is None or profile_id() startswith
    # SERVER_URL` (app/models.py:795), so an ap_id is what makes a community
    # remote -- not instance_id, and not the ap_profile_id host alone.
    remote_community.ap_id = 'remote_one@remote.example'
    remote_community.instance_id = instance('remote.example', 'lemmy').id
    db.session.commit()
    assert not remote_community.is_local()
    client = app.test_client()
    login(client, outsider)
    token = csrf(app, client)

    with patch('app.community.routes.render_template', return_value='rendered'):
        response = client.post(url(app, 'community.community_report',
                                   community_id=remote_community.id),
                               data={'reasons': ['1'], 'description': 'spam',
                                     'report_remote': 'y', 'submit': 'Report',
                                     'csrf_token': token})

    assert response.status_code == 302
    assert Report.query.count() == 1


def test_reporting_a_community_that_does_not_exist_is_a_404(app,
                                                            two_communities):
    mine, theirs, moderator, outsider = two_communities
    client = app.test_client()
    login(client, outsider)

    assert client.get(url(app, 'community.community_report',
                          community_id=999999)).status_code == 404


def test_ignoring_a_report_cannot_be_driven_by_a_bare_get(app, two_communities,
                                                          mod_client):
    """D976's pin, inverted -- D955's shape, second instance.

    `community_moderate_report_ignore` has no form and acted on whichever
    method arrived, and `login_required` validates CSRF only for POST. The
    template rendered it as a plain `<a href>`, so a moderator who loaded the
    link from anywhere discarded that report with no token involved. Escalate
    and Resolve are safe as GET links because both render a confirmation form
    first; this one never did.
    """
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    report = _report(mine, outsider)

    response = client.get(url(app, 'community.community_moderate_report_ignore',
                              community_id=mine.id, report_id=report.id))

    assert response.status_code == 405
    db.session.expire_all()
    assert db.session.get(Report, report.id).status == REPORT_STATE_NEW


def test_ignoring_a_report_about_a_user_actually_marks_it(app, two_communities,
                                                          mod_client):
    """D975's pin, inverted.

    The function never set `report.status` on the report it was handed. The
    sweep below it only updates rows sharing a `suspect_post_id` or
    `suspect_post_reply_id`, so a report whose subject is a USER was marked by
    neither: the moderator pressed Ignore, the user's counter went to -1, and
    the report stayed REPORT_STATE_NEW in the queue forever.

    Found by a parameterised row that asked the same question of all three
    subject kinds -- the post and reply cases passed, because the sweep
    happened to cover them.
    """
    from app import constants

    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    report = _report(mine, outsider)
    report.suspect_user_id = outsider.id
    db.session.commit()

    client.post(url(app, 'community.community_moderate_report_ignore',
                    community_id=mine.id, report_id=report.id),
                data={'csrf_token': token})

    db.session.expire_all()
    assert db.session.get(Report, report.id).status == constants.REPORT_STATE_DISCARDED
    assert db.session.get(User, outsider.id).reports == -1


# --------------------------------------------------------------------------
# The listing and viewing pages
# --------------------------------------------------------------------------


@pytest.mark.parametrize('endpoint, template', [
    ('community.community_wiki_list', 'community/community_wiki_list.html'),
    ('community.community_flair', 'community/community_flair.html'),
])
def test_the_moderation_listings_render_for_a_moderator(app, two_communities,
                                                        mod_client, endpoint,
                                                        template):
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    _wiki_page(mine, title='Guide', slug='guide')
    _flair(mine, 'mine')

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, endpoint, actor=mine.name))

    assert response.status_code == 200
    assert render.call_args.args == (template,)
    assert render.call_args.kwargs['community'].id == mine.id


@pytest.mark.parametrize('endpoint', ['community.community_wiki_list',
                                      'community.community_flair'])
def test_the_moderation_listings_refuse_an_outsider(app, two_communities,
                                                    endpoint):
    mine, theirs, moderator, outsider = two_communities
    client = app.test_client()
    login(client, outsider)

    assert client.get(url(app, endpoint, actor=mine.name)).status_code == 401


@pytest.mark.parametrize('endpoint', ['community.community_wiki_list',
                                      'community.community_flair',
                                      'community.community_wiki_add'])
def test_the_moderation_pages_are_404_for_an_unknown_actor(app,
                                                           two_communities,
                                                           mod_client,
                                                           endpoint):
    """`else: abort(404)` -- `actor_to_community` returns None for an actor it
    cannot resolve, and the actor comes straight from the URL."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client

    assert client.get(url(app, endpoint,
                          actor='no-such-community')).status_code == 404


def test_the_wiki_list_shows_only_this_communitys_pages(app, two_communities,
                                                        mod_client):
    """`filter(CommunityWikiPage.community_id == community.id)` -- the listing
    scopes correctly, unlike the routes that took a page id. Pinned so it stays
    that way."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    _wiki_page(mine, title='Ours', slug='ours')
    _wiki_page(theirs, title='Theirs', slug='theirs')

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get(url(app, 'community.community_wiki_list', actor=mine.name))

    assert [page.title for page in render.call_args.kwargs['pages']] == ['Ours']


def test_adding_a_wiki_page_records_an_initial_revision(app, two_communities,
                                                        mod_client):
    """Two commits: the page, then the revision that needs its id. A page with
    no initial revision has nothing to revert to, so both are asserted."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client

    with patch('app.community.routes.render_template', return_value='rendered'):
        response = client.post(url(app, 'community.community_wiki_add',
                                   actor=mine.name),
                               data={'title': 'New page', 'slug': 'new-page',
                                     'body': '# hello', 'who_can_edit': '2',
                                     'submit': 'Save', 'csrf_token': token})

    assert response.status_code == 302
    page = CommunityWikiPage.query.one()
    assert (page.title, page.slug, page.who_can_edit) == ('New page', 'new-page', 2)
    assert '<h1>hello</h1>' in page.body_html
    revision = CommunityWikiPageRevision.query.filter_by(wiki_page_id=page.id).one()
    assert (revision.body, revision.user_id) == ('# hello', moderator.id)


def test_the_add_page_form_renders_on_a_get(app, two_communities, mod_client):
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.community_wiki_add',
                                  actor=mine.name))

    assert response.status_code == 200
    assert render.call_args.kwargs['form'] is not None


def test_an_outsider_cannot_add_a_wiki_page(app, two_communities):
    mine, theirs, moderator, outsider = two_communities
    client = app.test_client()
    login(client, outsider)
    token = csrf(app, client)

    with patch('app.community.routes.render_template', return_value='rendered'):
        response = client.post(url(app, 'community.community_wiki_add',
                                   actor=mine.name),
                               data={'title': 'x', 'slug': 'x', 'body': 'x',
                                     'who_can_edit': '0', 'submit': 'Save',
                                     'csrf_token': token})

    assert response.status_code == 401
    assert CommunityWikiPage.query.count() == 0


def test_a_wiki_page_renders_with_its_breadcrumbs(app, two_communities,
                                                  mod_client):
    """`community_wiki_view` builds a breadcrumb trail, and the topic arm walks
    `parent_id` up to the root -- so a nested topic is what exercises the
    loop."""
    from app.models import Topic

    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    parent = Topic(name='Parent', machine_name='parent', num_communities=1)
    db.session.add(parent)
    db.session.commit()
    child = Topic(name='Child', machine_name='child', parent_id=parent.id,
                  num_communities=1)
    db.session.add(child)
    db.session.commit()
    mine.topic_id = child.id
    db.session.commit()
    _wiki_page(mine, title='Guide', slug='guide')

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.community_wiki_view',
                                  actor=mine.name, slug='guide'))

    assert response.status_code == 200
    texts = [crumb.text for crumb in render.call_args.kwargs['breadcrumbs']]
    assert texts[0] == 'Home'
    assert 'Topics' in texts


def test_a_wiki_page_renders_without_a_topic(app, two_communities, mod_client):
    """`if community.topic_id:` -- the false arm. A community with no topic
    still needs a breadcrumb trail."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    mine.topic_id = None
    db.session.commit()
    _wiki_page(mine, title='Guide', slug='guide')

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.community_wiki_view',
                                  actor=mine.name, slug='guide'))

    assert response.status_code == 200
    # Home, then Communities -- and no Topics crumb, which is the arm under
    # test. A community with no topic cannot have a topic trail.
    texts = [crumb.text for crumb in render.call_args.kwargs['breadcrumbs']]
    assert texts == ['Home', 'Communities']


def test_a_wiki_page_that_does_not_exist_is_a_404(app, two_communities,
                                                  mod_client):
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client

    assert client.get(url(app, 'community.community_wiki_view', actor=mine.name,
                          slug='no-such-page')).status_code == 404


def test_the_revisions_list_shows_only_this_pages_revisions(app,
                                                            two_communities,
                                                            mod_client):
    """`community_wiki_revisions` -- the page is scoped by community and the
    revisions by page, which is the pairing D971 was about."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    page = _wiki_page(mine, slug='guide')
    _revision(page, mine, body='ours')
    other_page = _wiki_page(theirs, slug='theirs')
    _revision(other_page, theirs, body='theirs')

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.community_wiki_revisions',
                                  actor=mine.name, page_id=page.id))

    assert response.status_code == 200
    bodies = [r.body for r in render.call_args.kwargs['revisions']]
    assert 'theirs' not in bodies


def test_the_edit_form_is_prefilled_from_the_page(app, two_communities,
                                                  mod_client):
    """`community_wiki_edit`'s GET arm."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    page = _wiki_page(mine, title='Guide', slug='guide', body='original',
                      who_can_edit=2)

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.community_wiki_edit',
                                  actor=mine.name, page_id=page.id))

    assert response.status_code == 200
    form = render.call_args.kwargs['form']
    assert (form.title.data, form.slug.data, form.body.data) == \
        ('Guide', 'guide', 'original')
    assert form.who_can_edit.data == 2


def test_editing_a_wiki_page_records_a_revision(app, two_communities,
                                                mod_client):
    """Every edit appends to the history, which is what makes revert possible
    at all."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    page = _wiki_page(mine, slug='guide', body='original')

    with patch('app.community.routes.render_template', return_value='rendered'):
        client.post(url(app, 'community.community_wiki_edit', actor=mine.name,
                        page_id=page.id),
                    data={'title': 'Guide', 'slug': 'guide', 'body': 'second',
                          'who_can_edit': '0', 'submit': 'Save',
                          'csrf_token': token})

    db.session.expire_all()
    assert db.session.get(CommunityWikiPage, page.id).body == 'second'
    revisions = CommunityWikiPageRevision.query.filter_by(wiki_page_id=page.id).all()
    assert [r.body for r in revisions] == ['second']


def test_the_flair_edit_form_is_prefilled(app, two_communities, mod_client):
    """`community_flair_edit`'s GET arm, for an existing flair."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    flair = _flair(mine, 'mine')

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.community_flair_edit',
                                  community_id=mine.id, flair_id=flair.id))

    assert response.status_code == 200
    assert render.call_args.kwargs['form'].flair.data == 'mine'


# --------------------------------------------------------------------------
# The last arms
# --------------------------------------------------------------------------


def test_viewing_a_revision_builds_the_topic_breadcrumbs(app, two_communities,
                                                         mod_client):
    """`community_wiki_view_revision` builds the same trail as
    `community_wiki_view`, and walks `parent_id` to the root -- so a nested
    topic is what exercises the `while` loop, and the order it produces
    (outermost first) is what makes the trail navigable."""
    from app.models import Topic

    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    parent = Topic(name='Parent', machine_name='parent', num_communities=1)
    db.session.add(parent)
    db.session.commit()
    child = Topic(name='Child', machine_name='child', parent_id=parent.id,
                  num_communities=1)
    db.session.add(child)
    db.session.commit()
    mine.topic_id = child.id
    db.session.commit()
    page = _wiki_page(mine, slug='guide')
    revision = _revision(page, mine, body='the old text')

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.community_wiki_view_revision',
                                  actor=mine.name, slug='guide',
                                  revision_id=revision.id))

    assert response.status_code == 200
    texts = [crumb.text for crumb in render.call_args.kwargs['breadcrumbs']]
    assert texts == ['Home', 'Topics', 'Parent', 'Child']


def test_viewing_a_revision_without_a_topic(app, two_communities, mod_client):
    """The `else` arm of the same block."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    mine.topic_id = None
    db.session.commit()
    page = _wiki_page(mine, slug='guide')
    revision = _revision(page, mine)

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get(url(app, 'community.community_wiki_view_revision',
                       actor=mine.name, slug='guide', revision_id=revision.id))

    texts = [crumb.text for crumb in render.call_args.kwargs['breadcrumbs']]
    assert texts == ['Home', 'Communities']


def test_reverting_refuses_somebody_who_cannot_edit(app, two_communities):
    """`else: abort(401)` on the revert path -- `can_edit` is the same gate the
    edit route uses, so a page nobody but moderators may edit cannot be
    reverted by a member either."""
    mine, theirs, moderator, outsider = two_communities
    page = _wiki_page(mine, slug='guide', body='current', who_can_edit=0)
    revision = _revision(page, mine, body='the old text')
    client = app.test_client()
    login(client, outsider)

    response = client.get(url(app, 'community.community_wiki_revert_revision',
                              actor=mine.name, slug='guide',
                              revision_id=revision.id))

    assert response.status_code == 401
    db.session.expire_all()
    assert db.session.get(CommunityWikiPage, page.id).body == 'current'


@pytest.mark.parametrize('return_to, endpoint_fragment', [
    ('list', '/moderate/wiki'),
    ('page', '/wiki/guide'),
])
def test_saving_a_wiki_edit_honours_the_return_parameter(app, two_communities,
                                                         mod_client, return_to,
                                                         endpoint_fragment):
    """`request.args.get('return')` -- the edit form is reached from two
    places, and each sends the moderator back where they came from."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    page = _wiki_page(mine, slug='guide', body='original')

    with patch('app.community.routes.render_template', return_value='rendered'):
        response = client.post(
            url(app, 'community.community_wiki_edit', actor=mine.name,
                page_id=page.id) + f'?return={return_to}',
            data={'title': 'Guide', 'slug': 'guide', 'body': 'changed',
                  'who_can_edit': '0', 'submit': 'Save', 'csrf_token': token})

    assert response.status_code == 302
    assert endpoint_fragment in response.headers['Location']


def test_saving_a_wiki_edit_without_a_return_renders_the_form_again(
        app, two_communities, mod_client):
    """Neither `return` value -- the function falls through to
    `render_template` rather than redirecting, which is the third arm."""
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    page = _wiki_page(mine, slug='guide', body='original')

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.post(
            url(app, 'community.community_wiki_edit', actor=mine.name,
                page_id=page.id),
            data={'title': 'Guide', 'slug': 'guide', 'body': 'changed',
                  'who_can_edit': '0', 'submit': 'Save', 'csrf_token': token})

    assert response.status_code == 200
    assert render.call_args.args == ('community/community_wiki_edit.html',)
    db.session.expire_all()
    assert db.session.get(CommunityWikiPage, page.id).body == 'changed'


@pytest.mark.parametrize('endpoint, kwargs', [
    ('community.community_wiki_revisions', {'page_id': 1}),
    ('community.community_wiki_edit', {'page_id': 1}),
])
def test_the_wiki_pages_are_404_for_an_unknown_actor(app, two_communities,
                                                     mod_client, endpoint,
                                                     kwargs):
    mine, theirs, moderator, outsider = two_communities
    client, token = mod_client
    _wiki_page(mine, slug='guide')

    response = client.get(url(app, endpoint, actor='no-such-community',
                              **kwargs))

    assert response.status_code == 404


def test_an_outsider_cannot_list_revisions(app, two_communities):
    """`else: abort(401)` on community_wiki_revisions."""
    mine, theirs, moderator, outsider = two_communities
    page = _wiki_page(mine, slug='guide', who_can_edit=0)
    client = app.test_client()
    login(client, outsider)

    response = client.get(url(app, 'community.community_wiki_revisions',
                              actor=mine.name, page_id=page.id))

    assert response.status_code == 401
