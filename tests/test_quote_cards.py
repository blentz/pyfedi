"""C2 quote cards: resolution and the card-or-line decision."""
from unittest.mock import patch

import pytest
from flask import g, render_template_string
from sqlalchemy import event

from app import cache, db
from app.constants import VISIBILITY_FOLLOWERS
from app.models import BannedInstances, QuoteAuthorization
from app.quotes import quote_state, quote_target, resolve_quotes
from tests.factories import (make_community, make_community_ban, make_community_block, make_community_member,
                             make_follow, make_instance, make_post, make_post_reply, make_user, make_user_block)

PEER = 'remote.test'


@pytest.fixture
def world(app, db_session):
    local = make_instance(app.config['SERVER_NAME'], software='piefed')
    cache.clear()
    remote = make_instance(PEER)
    quoted_author = make_user(remote, 'quoted')
    quoter = make_user(remote, 'quoter')
    viewer = make_user(local, 'viewer', local=True)
    community = make_community('cards')  # after the users: it is owned by user 1
    for u in (quoted_author, quoter):
        make_community_member(u, community)
    target = make_post(community, quoted_author, ap_id=f'https://{PEER}/post/9')
    target.body = 'the quoted words'
    quoting = make_post(community, quoter, ap_id=f'https://{PEER}/post/10')
    quoting.quote_ap_id = target.ap_id
    db.session.commit()
    g.pop('quote_targets', None)
    return locals()


def test_no_quote_is_none(world):
    assert quote_state(world['target'], world['viewer']) is None


def test_known_remote_target_is_a_card(world):
    state = quote_state(world['quoting'], world['viewer'])
    assert state['kind'] == 'card' and state['target'] is world['target'] and state['href'] == f"/post/{world['target'].id}"


def test_unknown_target_is_a_line(world):
    world['quoting'].quote_ap_id = 'https://nowhere.test/1'
    state = quote_state(world['quoting'], world['viewer'])
    assert state == {'kind': 'line', 'url': 'https://nowhere.test/1', 'target': None, 'href': None,
                     'author': None, 'community': None, 'text': None, 'posted_at': None}


def test_quoted_comment_links_to_its_anchor(world):
    reply = make_post_reply(world['target'], world['quoted_author'], body='a comment')
    reply.ap_id = f'https://{PEER}/comment/3'
    world['quoting'].quote_ap_id = reply.ap_id
    db.session.commit()
    state = quote_state(world['quoting'], world['viewer'])
    assert state['href'] == f"/post/{world['target'].id}#comment_{reply.id}"


def test_local_post_slug_form_resolves_by_id(world, app):
    world['quoting'].quote_ap_id = f"{app.config['SERVER_URL']}/post/{world['target'].id}"
    assert quote_target(world['quoting']) is world['target']


def test_deleted_target_is_a_line(world):
    world['target'].deleted = True
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'


def test_followers_only_quoted_post_is_a_line_for_a_stranger(world):
    world['target'].visibility = VISIBILITY_FOLLOWERS
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'


def test_blocked_author_is_a_line(world):
    make_user_block(world['viewer'], world['quoted_author'])
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'


def test_blocked_community_is_a_line(world):
    make_community_block(world['viewer'], world['community'])
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'


def test_banned_from_community_is_a_line(world):
    make_community_ban(world['viewer'], world['community'])
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'


def test_gone_community_is_a_line(world):
    from app.models import utcnow
    world['community'].ap_deleted_at = utcnow()
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'


def test_site_banned_instance_is_a_line(world):
    db.session.add(BannedInstances(domain=PEER))
    db.session.commit()
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'


@pytest.mark.parametrize('flag,setting,kind', [('nsfw', 0, 'card'), ('nsfw', 1, 'line'), ('nsfw', 2, 'line'),
                                               ('nsfl', 0, 'card'), ('nsfl', 1, 'line')])
def test_sensitive_target_follows_the_viewer_setting(world, flag, setting, kind):
    setattr(world['target'], flag, True)
    setattr(world['viewer'], 'hide_' + flag, setting)
    assert quote_state(world['quoting'], world['viewer'])['kind'] == kind


def test_sensitive_target_is_a_line_for_anonymous(world, app):
    from flask_login import AnonymousUserMixin
    world['target'].nsfw = True
    assert quote_state(world['quoting'], AnonymousUserMixin())['kind'] == 'line'


def test_local_open_target_is_a_card(world):
    world['quoted_author'].instance_id = world['local'].id
    world['quoted_author'].ap_id = None
    world['target'].ap_id = None
    world['quoting'].quote_ap_id = f"https://{world['local'].domain}/post/{world['target'].id}"
    db.session.commit()
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'card'


def test_local_followers_only_target_needs_an_authorization(world):
    world['quoted_author'].instance_id = world['local'].id
    world['quoted_author'].ap_id = None
    world['target'].ap_id = None
    world['target'].visibility = VISIBILITY_FOLLOWERS
    world['quoting'].quote_ap_id = f"https://{world['local'].domain}/post/{world['target'].id}"
    # the viewer follows the author, so can_view passes; only authorization decides:
    make_follow(world['viewer'], world['quoted_author'])
    db.session.commit()
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'
    db.session.add(QuoteAuthorization(post_id=world['target'].id, quoting_uri=world['quoting'].ap_id))
    db.session.commit()
    g.pop('quote_targets', None)
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'card'


def test_resolve_quotes_with_no_ids_queries_nothing(world):
    assert resolve_quotes(set()) == {}


def test_priming_resolves_a_page_in_one_query_per_table(world, app):
    posts = []
    for n in range(20):
        p = make_post(world['community'], world['quoter'], ap_id=f'https://{PEER}/post/q{n}')
        p.quote_ap_id = f'https://{PEER}/post/target{n}'
        posts.append(p)
    db.session.commit()
    statements = []
    listener = lambda *args: statements.append(args[2])
    event.listen(db.engine, 'before_cursor_execute', listener)
    try:
        with app.test_request_context('/'):
            render_template_string('{% for p in posts %}{{ quote_state(p).kind }}{% endfor %}', posts=posts)
    finally:
        event.remove(db.engine, 'before_cursor_execute', listener)
    lookups = [s for s in statements if '.ap_id IN' in s]  # resolve_quotes' batch selects, one per table
    assert len(lookups) <= 2
    assert not [s for s in statements if '.ap_id = ' in s]  # and no per-item lookups


def test_priming_scan_matched_the_batch_select_it_counts(world, app):
    """The query-count test's matcher must actually see the batch select, or it passes by seeing nothing."""
    statements = []
    listener = lambda *args: statements.append(args[2])
    event.listen(db.engine, 'before_cursor_execute', listener)
    try:
        with app.test_request_context('/'):
            render_template_string('{{ quote_state(p).kind }}', p=world['quoting'])
    finally:
        event.remove(db.engine, 'before_cursor_execute', listener)
    assert [s for s in statements if 'post.ap_id IN' in s]


def test_scan_walks_lists_tuples_pagination_and_comment_trees(world):
    from app.quotes import _quoting_rows
    quoting, target = world['quoting'], world['target']
    reply = make_post_reply(target, world['quoter'])
    reply.quote_ap_id = 'https://elsewhere.test/c/1'
    nested = make_post_reply(target, world['quoter'])
    nested.quote_ap_id = 'https://elsewhere.test/c/2'
    shared = [quoting]

    class Page:
        items = [reply]

    context = {'a': (quoting,), 'b': Page(), 'c': {'comment': target, 'replies': [{'comment': nested, 'replies': []}]},
               'd': {'comment': target, 'replies': None}, 'e': {'unrelated': quoting}, 'f': shared, 'g': shared,
               'h': 'text', 'i': target}
    rows = _quoting_rows(context)
    assert sorted(r.quote_ap_id for r in rows) == sorted([quoting.quote_ap_id] * 2 + [reply.quote_ap_id, nested.quote_ap_id])


def test_a_missed_row_falls_back_to_one_memoised_lookup(world, app):
    with app.test_request_context('/'):
        assert quote_target(world['quoting']) is world['target']
        statements = []
        listener = lambda *args: statements.append(args[2])
        event.listen(db.engine, 'before_cursor_execute', listener)
        try:
            assert quote_target(world['quoting']) is world['target']
        finally:
            event.remove(db.engine, 'before_cursor_execute', listener)
    assert statements == []


def test_local_comment_url_form_resolves_by_id(world, app):
    reply = make_post_reply(world['target'], world['quoted_author'])
    for url, expected in ((f"{app.config['SERVER_URL']}/comment/{reply.id}", reply),
                          (f"{app.config['SERVER_URL']}/comment/999999", None),
                          (f"{app.config['SERVER_URL']}/u/someone", None)):
        world['quoting'].quote_ap_id = url
        g.pop('quote_targets', None)
        assert quote_target(world['quoting']) is expected


def test_a_quoted_comment_on_a_sensitive_post_follows_the_setting(world):
    reply = make_post_reply(world['target'], world['quoted_author'])
    reply.ap_id = f'https://{PEER}/comment/4'
    world['quoting'].quote_ap_id = reply.ap_id
    world['target'].nsfw = True
    db.session.commit()
    assert quote_target(world['quoting']) is reply
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'
    world['viewer'].hide_nsfw = 0
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'card'


def test_local_comment_target_needs_an_authorization(world):
    world['quoted_author'].instance_id = world['local'].id
    world['quoted_author'].ap_id = None
    reply = make_post_reply(world['target'], world['quoted_author'])
    reply.visibility = VISIBILITY_FOLLOWERS
    make_follow(world['viewer'], world['quoted_author'])
    world['quoting'].quote_ap_id = f'https://{PEER}/comment/77'
    reply.ap_id = world['quoting'].quote_ap_id
    db.session.commit()
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'
    db.session.add(QuoteAuthorization(post_reply_id=reply.id, quoting_uri=world['quoting'].ap_id))
    db.session.commit()
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'card'


def test_default_viewer_is_the_request_user_and_titleless_text_is_the_body(world, app):
    world['target'].title = ''
    with app.test_request_context('/'):
        state = quote_state(world['quoting'])
    assert state['kind'] == 'card' and state['text'] == 'the quoted words'


@pytest.mark.parametrize('status', [-2, -1, 0])
def test_unpublished_target_is_a_line_unless_the_viewer_is_its_author(world, status):
    world['target'].status = status
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'
    assert quote_state(world['quoting'], world['quoted_author'])['kind'] == 'card'


def test_private_community_target_is_a_card_only_for_a_member(world):
    from flask_login import AnonymousUserMixin
    world['community'].private = True
    db.session.commit()
    assert quote_state(world['quoting'], AnonymousUserMixin())['kind'] == 'line'
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'
    make_community_member(world['viewer'], world['community'])
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'card'


def test_banned_community_target_is_a_line(world):
    world['community'].banned = True
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'


@pytest.mark.parametrize('change', [{'deleted': True}, {'visibility': VISIBILITY_FOLLOWERS}, {'status': 0}])
def test_a_quoted_comment_is_a_line_when_its_post_is_not_viewable(world, change):
    reply = make_post_reply(world['target'], world['quoted_author'])
    reply.ap_id = f'https://{PEER}/comment/5'
    world['quoting'].quote_ap_id = reply.ap_id
    db.session.commit()
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'card'
    for name, value in change.items():
        setattr(world['target'], name, value)
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'


def test_sensitive_target_is_a_line_in_a_restricted_country(world):
    world['target'].nsfw = True
    world['viewer'].hide_nsfw = 0
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'card'
    with patch('app.quotes.user_in_restricted_country', return_value=True):
        assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'
