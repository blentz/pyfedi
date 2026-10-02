"""Final-review fixes for the visibility plan: a visible reply must not carry its hidden parent post, and the
remaining surfaces obey the predicate."""
from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.models import Language, Site
from tests.factories import bearer, make_post_reply, make_visibility_world
from tests.test_visibility_single_object import MISSING, client_as

SECRET_TITLE = 'Secret Parent Title'
SECRET_BODY = 'secret parent body'
PUBLIC_REPLY = 'zebrafinch public answer'


@pytest.fixture
def world(app, db_session, monkeypatch):
    w = make_visibility_world()
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.admin_ids = []
    g.site = site
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.add(Language(code='en', name='English'))
    db.session.commit()
    site.language_id = Language.query.filter_by(code='en').one().id
    w.post.title = SECRET_TITLE
    w.post.body = SECRET_BODY
    w.post.body_html = f'<p>{SECRET_BODY}</p>'
    # a PUBLIC reply, by a local follower, under the followers-only post
    w.open_reply = make_post_reply(w.post, w.follower, PUBLIC_REPLY)
    w.open_reply.ap_id = 'https://m.example/r/77'
    db.session.commit()
    return w


def api_get(app, path, user, **params):
    headers = {'Authorization': bearer(user)} if user is not None else {}
    return client_as(app, None).get(path, query_string=params, headers=headers)


def assert_no_secret(text, where):
    assert SECRET_TITLE not in text, where
    assert SECRET_BODY not in text, where


# C1
@pytest.mark.parametrize('who', ['stranger', None])
def test_post_replies_of_a_hidden_post_is_not_found(app, world, who):
    w = world
    user = getattr(w, who) if who else None
    hidden = api_get(app, '/api/alpha/post/replies', user, post_id=w.post.id)
    missing = api_get(app, '/api/alpha/post/replies', user, post_id=MISSING)
    assert hidden.status_code == missing.status_code != 200
    assert hidden.get_data(as_text=True) == missing.get_data(as_text=True)
    via_parent = api_get(app, '/api/alpha/post/replies', user, parent_id=w.open_reply.id)
    assert via_parent.status_code != 200
    assert_no_secret(via_parent.get_data(as_text=True), 'parent_id branch')


def test_post_replies_of_a_hidden_post_open_to_a_follower(app, world):
    w = world
    response = api_get(app, '/api/alpha/post/replies', w.follower, post_id=w.post.id)
    assert response.status_code == 200, response.get_data(as_text=True)
    assert PUBLIC_REPLY in response.get_data(as_text=True)


# R1 / C2: every reply-bearing surface, for an anonymous visitor and a logged-in stranger
def _api_surfaces(w, who):
    surfaces = [('comment', '/api/alpha/comment', {'id': w.open_reply.id}, True),
                ('list community', '/api/alpha/comment/list', {'community_id': w.community.id}, True),
                ('list post', '/api/alpha/comment/list', {'post_id': w.post.id}, False),
                ('list person', '/api/alpha/comment/list', {'person_id': w.follower.id}, True),
                ('list search', '/api/alpha/comment/list', {'q': 'zebrafinch'}, False)]
    if who is not None:
        surfaces += [('list saved', '/api/alpha/comment/list', {'saved_only': 'true'}, True),
                     ('list liked', '/api/alpha/comment/list', {'liked_only': 'true'}, True),
                     ('resolve', '/api/alpha/resolve_object', {'q': w.open_reply.ap_id}, True)]
    return surfaces


def _web_surfaces(w, who):
    surfaces = [('community comments', '/c/microblogs?content_type=comments'),
                ('topic comments', '/topic/news?content_type=comments'),
                ('search', '/search?q=zebrafinch&search_for=comments'),
                ('profile', f'/u/{w.follower.user_name}')]
    if who is not None:
        surfaces.append(('bookmarks', '/bookmarks/comments'))
    return surfaces


@pytest.fixture
def swept(world):
    from app.models import Topic
    from tests.factories import make_post_reply_bookmark, make_post_reply_vote
    w = world
    topic = Topic(name='news', machine_name='news', num_communities=1, show_posts_in_children=False)
    db.session.add(topic)
    db.session.commit()
    w.community.topic_id = topic.id
    w.post.indexable = True
    w.open_reply.indexable = True
    db.session.commit()
    for user in (w.stranger, w.follower):
        make_post_reply_bookmark(user, w.open_reply)
        make_post_reply_vote(user, w.open_reply, 1)
    return w


@pytest.mark.parametrize('who', ['stranger', None])
def test_a_visible_reply_never_carries_its_hidden_parent_through_the_api(app, swept, who):
    w = swept
    user = getattr(w, who) if who else None
    for name, path, params, listed in _api_surfaces(w, who):
        response = api_get(app, path, user, **params)
        text = response.get_data(as_text=True)
        assert_no_secret(text, name)
        assert 'alice' not in text, name
        if listed:
            assert response.status_code == 200, (name, text)
            assert PUBLIC_REPLY in text, name


@pytest.mark.parametrize('who', ['stranger', None])
def test_a_visible_reply_never_carries_its_hidden_parent_on_the_web(app, swept, who):
    w = swept
    for name, path in _web_surfaces(w, who):
        response = client_as(app, getattr(w, who) if who else None).get(path)
        text = response.get_data(as_text=True)
        assert response.status_code == 200, name
        assert_no_secret(text, name)
        if name != 'search':  # search needs the full-text index, which the test rows may not carry
            assert PUBLIC_REPLY in text, name


def test_the_nntp_subject_of_a_reply_does_not_name_a_hidden_parent(app, swept):
    from app.nntp.server import _reply_to_info
    w = swept
    info = _reply_to_info(w.open_reply, 'piefed.test', 1)
    assert SECRET_TITLE not in info.subject
    w.post.visibility = 'public'
    db.session.commit()
    assert SECRET_TITLE in _reply_to_info(w.open_reply, 'piefed.test', 1).subject


def test_a_follower_sees_the_parent_title(app, swept):
    w = swept
    api = api_get(app, '/api/alpha/comment', w.follower, id=w.open_reply.id)
    assert api.status_code == 200
    assert api.get_json()['comment_view']['post']['title'] == SECRET_TITLE
    web = client_as(app, w.follower).get('/c/microblogs?content_type=comments').get_data(as_text=True)
    assert SECRET_TITLE in web


def test_the_stub_parent_names_nothing_but_ids(app, swept):
    w = swept
    post = api_get(app, '/api/alpha/comment', w.stranger, id=w.open_reply.id).get_json()['comment_view']['post']
    assert post['id'] == w.post.id and post['visibility'] == 'followers'
    assert post['title'] == '' and post['user_id'] == 0 and not post.get('body') and not post.get('url')


# C3
def test_cross_posts_do_not_carry_a_hidden_post(app, world):
    w = world
    w.public_post.cross_posts = [w.post.id]
    w.post.cross_posts = [w.public_post.id]
    db.session.commit()
    for user in (w.stranger, None):
        response = api_get(app, '/api/alpha/post', user, id=w.public_post.id)
        assert response.status_code == 200, response.get_data(as_text=True)
        assert_no_secret(response.get_data(as_text=True), 'cross_posts')
        assert 'alice' not in str(response.get_json()['cross_posts'])
    follower = api_get(app, '/api/alpha/post', w.follower, id=w.public_post.id).get_json()
    assert follower['cross_posts'][0]['post']['title'] == SECRET_TITLE


# R2
def _stub_of(comments, reply_id):
    for c in comments:
        if c['comment']['id'] == reply_id:
            return c
        found = _stub_of(c.get('replies') or [], reply_id)
        if found:
            return found


@pytest.mark.parametrize('path, params', [('/api/alpha/post/replies', 'post_id'),
                                          ('/api/alpha/comment/list', 'post_id')])
def test_a_reply_stub_carries_neutral_objects_not_nulls(app, world, path, params):
    w = world
    w.reply.body = 'secret reply'
    db.session.commit()
    response = api_get(app, path, w.stranger, **{params: w.public_post.id})
    assert response.status_code == 200, response.get_data(as_text=True)
    stub = _stub_of(response.get_json()['comments'], w.reply.id)
    assert stub['visibility'] == 'followers' and stub['comment']['body'] is None
    for key in ('creator', 'counts', 'post', 'community'):
        assert isinstance(stub[key], dict), key
    assert stub['creator']['id'] == 0 and stub['creator']['user_name'] == ''
    assert stub['counts']['score'] == 0 and stub['counts']['comment_id'] == w.reply.id
    assert stub['post']['id'] == w.public_post.id and stub['post']['title'] == '' and stub['post']['user_id'] == 0
    text = response.get_data(as_text=True)
    assert 'secret reply' not in text and '"alice"' not in text


# R3 / I1
@pytest.fixture
def modlogged(world):
    from app.models import ModLog
    from app.utils import add_to_modlog, set_setting
    from tests.factories import grant_permission, make_user
    w = world
    set_setting('public_modlog', True)
    w.admin = make_user(w.stranger.instance, 'ada', local=True)
    role = grant_permission(w.admin, 'administer all communities')
    role.name = 'Admin'
    w.reply.body = 'secret reply'
    db.session.commit()
    for action in ('delete_post', 'lock_post', 'featured_post'):
        add_to_modlog(action, actor=w.admin, target_user=w.author, community=w.community, post=w.post,
                      link=f'post/{w.post.id}', link_text=SECRET_TITLE)
    add_to_modlog('delete_post_reply', actor=w.admin, target_user=w.author, community=w.community, reply=w.reply,
                  link=f'post/{w.public_post.id}#comment_{w.reply.id}', link_text='secret reply')
    w.modlog = ModLog.query.all()
    return w


def test_the_modlog_keeps_the_target_user(app, modlogged):
    w = modlogged
    assert len(w.modlog) == 4
    for entry in w.modlog:
        assert entry.target_user_id == w.author.id
        assert entry.link_text == 'followers-only content'


@pytest.mark.parametrize('who', ['stranger', None])
def test_the_api_modlog_names_nothing_hidden_to_a_non_admin(app, modlogged, who):
    w = modlogged
    response = api_get(app, '/api/alpha/modlog', getattr(w, who) if who else None)
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    for key in ('removed_posts', 'locked_posts', 'featured_posts', 'removed_comments'):
        assert len(body[key]) == 1, key
    text = response.get_data(as_text=True)
    assert_no_secret(text, 'modlog')
    assert 'secret reply' not in text and 'alice' not in text
    removed = body['removed_comments'][0]
    assert removed['commenter']['id'] == 0 and removed['comment']['body'] in ('', None)
    filtered = api_get(app, '/api/alpha/modlog', getattr(w, who) if who else None, other_person_id=w.author.id)
    assert all(filtered.get_json()[key] == [] for key in ('removed_posts', 'locked_posts', 'featured_posts',
                                                          'removed_comments'))


def test_the_api_modlog_shows_an_admin_the_target_user(app, modlogged):
    w = modlogged
    g.admin_ids = [w.admin.id]
    response = api_get(app, '/api/alpha/modlog', w.admin, other_person_id=w.author.id)
    body = response.get_json()
    assert len(body['removed_comments']) == 1 and len(body['removed_posts']) == 1
    assert body['removed_comments'][0]['commenter']['id'] == w.author.id


def test_the_web_modlog_filter_does_not_match_hidden_entries_for_a_non_admin(app, modlogged):
    from app.models import ModLog
    w = modlogged

    def shown(user):
        with patch('app.main.routes.render_template', return_value=app.response_class('rendered')) as render:
            assert client_as(app, user).get('/modlog?suspect_user_name=alice@m.example').status_code == 200
        return [e.id for e in render.call_args.kwargs['modlog_entries'].items]
    assert shown(w.stranger) == [] and shown(None) == []
    assert len(shown(w.admin)) == 4
    w.post.visibility = 'public'
    w.reply.visibility = 'public'
    db.session.commit()
    assert len(shown(w.stranger)) == 4


# I2
def test_the_moderator_comments_page_obeys_the_predicate(app, world):
    from tests.factories import make_community_member
    w = world
    make_community_member(w.stranger, w.community, is_moderator=True)
    make_community_member(w.follower, w.community, is_moderator=True)
    w.reply.body = 'secret reply'
    db.session.commit()

    def shown(user):
        with patch('app.community.routes.render_template', return_value=app.response_class('rendered')) as render:
            assert client_as(app, user).get('/community/microblogs/moderate/comments').status_code == 200
        return [r.id for r in render.call_args.kwargs['post_replies'].items]
    assert w.reply.id not in shown(w.stranger) and w.public_child.id in shown(w.stranger)
    assert w.reply.id in shown(w.follower)


# I3
def test_check_url_already_posted_obeys_the_predicate(app, world):
    from tests.factories import make_post
    w = world
    url = 'https://news.example/story'
    hidden = make_post(w.community, w.author, 'https://m.example/s/9', title='hidden link')
    hidden.url = url
    hidden.visibility = 'followers'
    db.session.commit()

    def shown(user):
        with patch('app.community.routes.retrieve_metadata_of_url', return_value=('', '')), \
                patch('app.community.routes.flask.render_template', return_value='rendered') as render:
            assert client_as(app, user).get('/community/check_url_already_posted', query_string={'link_url': url}).status_code == 200
        return [p.id for p in render.call_args.kwargs['posts']]
    assert shown(w.stranger) == []
    assert shown(w.follower) == [hidden.id]


# I5
def test_a_poll_vote_on_a_followers_only_poll_is_not_relayed(app, db_session, monkeypatch):
    from app.activitypub import routes as activitypub_routes
    from app.models import PollChoiceVote
    from tests.test_inbox_dispatch_votes import _seed_poll_scenario
    voter, post, choice = _seed_poll_scenario()
    post.visibility = 'followers'
    db.session.commit()
    calls = []
    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers', lambda *a, **k: calls.append(a))
    request_json = {'id': 'https://peer.example/activities/1', 'object': post.ap_id, 'choice_text': 'yes'}
    activitypub_routes.process_poll_vote(voter, False, request_json, False)
    assert PollChoiceVote.query.filter_by(user_id=voter.id, choice_id=choice.id).count() == 1
    assert calls == []


# M2
def test_mark_as_read_skips_missing_and_hidden_ids_in_a_batch(app, world):
    from app.models import read_posts
    w = world
    response = client_as(app, None).post('/api/alpha/post/mark_as_read',
                                          json={'post_ids': [w.public_post.id, w.post.id, MISSING], 'read': True},
                                          headers={'Authorization': bearer(w.stranger)})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json() == {'success': True}
    marked = {row.read_post_id for row in db.session.execute(
        db.select(read_posts).where(read_posts.c.user_id == w.stranger.id))}
    assert marked == {w.public_post.id}
