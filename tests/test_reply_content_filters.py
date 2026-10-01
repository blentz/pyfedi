"""R168, fixed (owner ruling): a user's reply keyword filters are applied.

`Filter.filter_replies` was a setting the filter form offered and nothing read.
Reader views of `post/_post_reply_teaser.html` now ask one Jinja global,
`reply_filter_keyword`, and render a matching reply as a collapsed
'Filtered: <keyword>' stub -- for 'hide completely' filters too -- while its
replies stay visible below it. The admin content page and the moderation queue
pass `no_content_filter` and show everything. The comment API gains a
`filtered` flag with the post API's meaning: a 'hide completely' filter matched.
"""
from unittest.mock import patch

import pytest
from flask import current_app

from app import db
from app.models import Filter, Instance, Site
from app.utils import reply_filter_keyword
from tests.factories import (bearer, make_community, make_community_member, make_instance, make_post,
                             make_post_reply, make_user, web_ctx)

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def env(app, db_session):
    from types import SimpleNamespace

    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = Instance.query.filter_by(domain='test.piefed.local').first() or \
        make_instance('test.piefed.local', software='piefed')
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # the admin
    mod = make_user(local, 'mod', local=True)
    author = make_user(local, 'author', local=True)
    reader = make_user(local, 'reader', local=True)
    community = make_community('general')
    db.session.commit()
    make_community_member(mod, community, is_moderator=True)
    post = make_post(community, author, 'https://test.piefed.local/p/1', title='a post')
    parent = _reply(post, author, 'politics again today')
    child = _reply(post, author, 'CHILDTEXT', parent=parent)
    return SimpleNamespace(founder=founder, mod=mod, author=author, reader=reader, community=community,
                           post=post, parent=parent, child=child)


def _reply(post, user, body, parent=None):
    reply = make_post_reply(post, user, body=body)
    reply.body_html = f'<p>{body}</p>'
    db.session.commit()
    if parent is None:
        reply.path = [0, reply.id]
        reply.depth = 0
    else:
        reply.parent_id = parent.id
        reply.root_id = parent.id
        reply.path = [0, parent.id, reply.id]
        reply.depth = 1
        parent.child_count = 1
    db.session.commit()
    return reply


def _filter(user, keywords, hide_completely=False):
    db.session.add(Filter(title='a filter', user_id=user.id, keywords=keywords, filter_home=False,
                          filter_posts=False, filter_replies=True, hide_type=1 if hide_completely else 0))
    db.session.commit()


def _as(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


def _page(app, user, url):
    # The signed-in post page renders the reply form's csrf_token, which the test
    # config turns off; turned on here for this GET, which needs no token.
    with patch.dict(app.config, {'WTF_CSRF_ENABLED': True}):
        return _as(app, user).get(url).get_data(as_text=True)


class TestTheGlobal:
    def test_it_names_the_matched_keyword(self, app, env):
        _filter(env.reader, 'cats\nPolitics')

        with web_ctx(app, env.reader):
            assert reply_filter_keyword(env.parent) == 'politics'
            assert reply_filter_keyword(env.child) is None

    def test_a_filter_not_set_for_replies_does_nothing(self, app, env):
        db.session.add(Filter(title='posts only', user_id=env.reader.id, keywords='politics',
                              filter_posts=True, filter_replies=False))
        db.session.commit()

        with web_ctx(app, env.reader):
            assert reply_filter_keyword(env.parent) is None

    def test_your_own_reply_is_never_filtered(self, app, env):
        _filter(env.author, 'politics')

        with web_ctx(app, env.author):
            assert reply_filter_keyword(env.parent) is None


class TestThePostPage:
    @pytest.mark.parametrize('hide_completely', [False, True])
    def test_a_filtered_reply_is_a_collapsed_stub_and_its_replies_stay(self, app, env, hide_completely):
        _filter(env.reader, 'politics', hide_completely=hide_completely)

        html = _page(app, env.reader, f'/post/{env.post.id}')

        assert '<summary class="filtered_reply">Filtered: politics</summary>' in html
        assert html.index('<details') < html.index('politics again today') < html.index('</details>')
        assert html.index('</details>') < html.index('CHILDTEXT')

    def test_an_unfiltered_reader_sees_no_stub(self, app, env):
        html = _page(app, env.reader, f'/post/{env.post.id}')

        assert 'filtered_reply' not in html
        assert 'politics again today' in html


class TestTheViewsThatStayUnfiltered:
    def test_the_moderation_queue(self, app, env):
        _filter(env.mod, 'politics')

        html = _as(app, env.mod).get('/community/general/moderate/comments').get_data(as_text=True)

        assert 'politics again today' in html
        assert 'filtered_reply' not in html

    def test_the_admin_content_page(self, app, env):
        _filter(env.founder, 'politics')

        html = _as(app, env.founder).get('/admin/content?posts_replies=replies&show=spammy').get_data(as_text=True)

        assert 'politics again today' in html
        assert 'filtered_reply' not in html


class TestTheCommentApi:
    """The post API's `filtered` is True when a 'hide completely' filter matched;
    the comment view now carries the same flag."""

    @pytest.fixture(autouse=True)
    def api_enabled(self, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')

    @pytest.mark.parametrize('hide_completely, expected', [(True, True), (False, False)])
    def test_the_flag(self, app, env, hide_completely, expected):
        _filter(env.reader, 'politics', hide_completely=hide_completely)

        response = app.test_client().get('/api/alpha/comment', query_string={'id': env.parent.id},
                                         headers={'Authorization': bearer(env.reader)})

        assert response.status_code == 200
        assert response.get_json()['comment_view']['filtered'] is expected

    def test_anonymous_is_never_filtered(self, app, env):
        response = app.test_client().get('/api/alpha/comment', query_string={'id': env.parent.id})

        assert response.get_json()['comment_view']['filtered'] is False
