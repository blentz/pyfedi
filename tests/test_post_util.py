"""Reading a post's replies, live or out of the archive.

Sub-project 92 -- `app/post/util.py`. Two jobs: assembling a post's replies
into a tree for the page, and reading them back out of a gzipped archive once
a post is old enough to have been rolled up.

One thing recorded and deliberately NOT repaired, at both of the two sites it
appears:

    comments.filter(PostReply.score > -20)

The result is discarded -- every other filter in both functions is
`comments = comments.filter(...)`. So the line that hides heavily downvoted
replies from anonymous visitors has never run. Repairing it is a product
decision rather than a defect fix: it would hide whole subtrees under a
downvoted parent from logged-out readers, and on a permalink to such a reply
it would answer with nothing at all. The tests below pin what the code does
TODAY, and say so, so that changing it is a decision somebody makes rather
than a side effect (D1277).
"""
import gzip
import json
from unittest.mock import patch

import httpx
import orjson
import pytest
from flask import g

from app import db
from app.constants import (POST_TYPE_ARTICLE, POST_TYPE_IMAGE, POST_TYPE_LINK,
                           POST_TYPE_POLL, POST_TYPE_VIDEO)
from app.models import Language, PostReply, Site
from app.post.util import (body_has_no_archive_link,
                           convert_archived_replies_to_tree,
                           find_comment_branch_in_archived,
                           flair_to_string, generate_archive_link,
                           get_comment_branch, get_post_reply_count,
                           post_replies, post_type_to_form_url_type,
                           retrieve_archived_post, tags_to_string)
from tests.factories import (make_community, make_community_member, make_post,
                             make_post_flair, make_post_reply, make_user)


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('probeland')
    author = api_baseline.user2
    reader = api_baseline.user3
    db.session.commit()
    make_community_member(author, community)
    post = make_post(community, author, ap_id='https://test.piefed.local/v/1')
    db.session.commit()
    return SimpleNamespace(app=app, community=community, author=author,
                           reader=reader, post=post, baseline=api_baseline)


def a_reply(env, body='a reply', parent=None, **columns):
    reply = make_post_reply(env.post, env.author, body=body)
    if parent is not None:
        reply.parent_id = parent.id
        reply.depth = parent.depth + 1
    for key, value in columns.items():
        setattr(reply, key, value)
    db.session.commit()
    return reply


def bodies(tree):
    return {node['comment'].body for node in tree}


# --------------------------------------------------------------------------
# the archive
# --------------------------------------------------------------------------

ARCHIVED = {'replies': [{'id': 900, 'body': 'an archived reply',
                         'body_html': '<p>an archived reply</p>',
                         'author_name': 'Somebody', 'replies': []}]}


class TestReadingAnArchive:
    def test_a_url_that_is_not_there(self, env):
        assert retrieve_archived_post(None) is None

    def test_an_empty_url(self, env):
        assert retrieve_archived_post('') is None

    def test_a_gzipped_archive_over_http(self, env, http_mock):
        http_mock.get('https://archive.test/a.json.gz').mock(
            return_value=httpx.Response(
                200, content=gzip.compress(orjson.dumps(ARCHIVED))))
        assert retrieve_archived_post(
            'https://archive.test/a.json.gz') == ARCHIVED

    def test_one_a_proxy_has_already_decompressed(self, env, http_mock):
        """Cloudflare unzips it in transit, so the bytes arrive as plain
        JSON and `gzip.decompress` raises `BadGzipFile`."""
        http_mock.get('https://archive.test/b.json.gz').mock(
            return_value=httpx.Response(200, content=orjson.dumps(ARCHIVED)))
        assert retrieve_archived_post(
            'https://archive.test/b.json.gz') == ARCHIVED

    def test_an_archive_that_answers_404(self, env, http_mock):
        http_mock.get('https://archive.test/c.json.gz').mock(
            return_value=httpx.Response(404))
        assert retrieve_archived_post('https://archive.test/c.json.gz') is None

    def test_an_archive_that_is_not_json(self, env, http_mock):
        http_mock.get('https://archive.test/d.json.gz').mock(
            return_value=httpx.Response(200, content=b'not json'))
        assert retrieve_archived_post('https://archive.test/d.json.gz') is None

    def test_an_archive_on_disk(self, env, tmp_path):
        path = tmp_path / 'archive.json.gz'
        with gzip.open(path, 'wb') as handle:
            handle.write(orjson.dumps(ARCHIVED))
        assert retrieve_archived_post(str(path)) == ARCHIVED

    def test_a_path_that_is_not_there(self, env, tmp_path):
        assert retrieve_archived_post(str(tmp_path / 'missing.gz')) is None

    def test_a_file_that_is_not_gzipped(self, env, tmp_path):
        path = tmp_path / 'plain.json.gz'
        path.write_bytes(b'{"replies": []}')
        assert retrieve_archived_post(str(path)) is None


class TestTurningAnArchiveBackIntoReplies:
    def test_nothing_archived(self, env):
        assert convert_archived_replies_to_tree([], env.post) == []
        assert convert_archived_replies_to_tree(None, env.post) == []

    def test_a_single_reply(self, env):
        tree = convert_archived_replies_to_tree(ARCHIVED['replies'], env.post)
        assert len(tree) == 1
        reply = tree[0]['comment']
        assert reply.body == 'an archived reply'
        assert reply.post is env.post
        assert reply.replies_enabled is False

    def test_the_author_is_rebuilt_from_what_was_stored(self, env):
        tree = convert_archived_replies_to_tree([{
            'id': 1, 'author_id': 42, 'author_name': 'Someone',
            'author_user_name': 'someone', 'author_ap_domain': 'remote.test',
            'author_bot': True, 'author_banned': True, 'replies': []}],
            env.post)
        author = tree[0]['comment'].author
        assert author.id == 42
        assert author.title == 'Someone'
        assert author.bot is True
        assert author.banned is True

    def test_an_author_the_archive_did_not_name(self, env):
        tree = convert_archived_replies_to_tree([{'id': 1, 'replies': []}],
                                                env.post)
        author = tree[0]['comment'].author
        assert author.title == 'Unknown'
        assert author.created is not None

    def test_the_times_are_parsed_back(self, env):
        tree = convert_archived_replies_to_tree([{
            'id': 1, 'posted_at': '2020-01-01T00:00:00',
            'edited_at': '2020-01-02T00:00:00',
            'author_created': '2019-01-01T00:00:00', 'replies': []}],
            env.post)
        reply = tree[0]['comment']
        assert reply.posted_at.year == 2020
        assert reply.edited_at.day == 2
        assert reply.author.created.year == 2019

    def test_a_language_that_is_named_is_looked_up(self, env):
        language = Language(code='en', name='English')
        db.session.add(language)
        db.session.commit()
        tree = convert_archived_replies_to_tree(
            [{'id': 1, 'language_id': language.id, 'replies': []}], env.post)
        assert tree[0]['comment'].language.code == 'en'

    def test_replies_to_replies_are_nested(self, env):
        tree = convert_archived_replies_to_tree([{
            'id': 1, 'body': 'first', 'replies': [
                {'id': 2, 'body': 'second', 'replies': [
                    {'id': 3, 'body': 'third', 'replies': []}]}]}], env.post)
        assert tree[0]['comment'].body == 'first'
        assert tree[0]['replies'][0]['comment'].body == 'second'
        assert tree[0]['replies'][0]['replies'][0]['comment'].body == 'third'


class TestFindingOneReplyInAnArchive:
    NESTED = [{'id': 1, 'body': 'first', 'replies': [
        {'id': 2, 'body': 'second', 'replies': [
            {'id': 3, 'body': 'third', 'replies': []}]}]},
        {'id': 4, 'body': 'another top-level one', 'replies': []}]

    def test_one_at_the_top(self, env):
        found = find_comment_branch_in_archived(self.NESTED, 1)
        assert found[0]['body'] == 'first'

    def test_one_further_down(self, env):
        found = find_comment_branch_in_archived(self.NESTED, 3)
        assert found[0]['body'] == 'third'

    def test_one_in_the_second_tree(self, env):
        found = find_comment_branch_in_archived(self.NESTED, 4)
        assert found[0]['body'] == 'another top-level one'

    def test_one_that_is_not_there(self, env):
        assert find_comment_branch_in_archived(self.NESTED, 999) == []

    def test_an_archive_with_nothing_in_it(self, env):
        assert find_comment_branch_in_archived([], 1) == []


class TestAPostThatHasBeenArchived:
    @pytest.fixture
    def archived(self, env, tmp_path):
        path = tmp_path / 'post.json.gz'
        with gzip.open(path, 'wb') as handle:
            handle.write(orjson.dumps(ARCHIVED))
        env.post.archived = str(path)
        db.session.commit()
        a_reply(env, body='a live reply')
        return env.post

    def test_its_replies_come_from_the_archive(self, env, archived):
        tree = post_replies(archived, 'new', viewer=None)
        assert bodies(tree) == {'an archived reply'}

    def test_unless_the_caller_asks_for_the_database(self, env, archived):
        tree = post_replies(archived, 'new', viewer=None, db_only=True)
        assert bodies(tree) == {'a live reply'}

    def test_an_archive_that_cannot_be_read_falls_back_to_the_database(
            self, env, archived):
        env.post.archived = '/nowhere/at/all.gz'
        db.session.commit()
        tree = post_replies(env.post, 'new', viewer=None)
        assert bodies(tree) == {'a live reply'}

    def test_an_archive_that_says_nothing_about_replies(self, env, tmp_path):
        """The membership test matters: `archived_data['replies']` on an
        archive that holds only the post would be `KeyError: 'replies'`."""
        path = tmp_path / 'post-only.json.gz'
        with gzip.open(path, 'wb') as handle:
            handle.write(orjson.dumps({'post': {'id': 1}}))
        env.post.archived = str(path)
        db.session.commit()
        a_reply(env, body='a live reply')
        tree = post_replies(env.post, 'new', viewer=None)
        assert bodies(tree) == {'a live reply'}

    def test_one_branch_of_an_archived_post(self, env, archived):
        tree = get_comment_branch(archived, 900, 'new', viewer=None)
        assert bodies(tree) == {'an archived reply'}

    def test_a_branch_the_archive_does_not_hold(self, env, archived):
        assert get_comment_branch(archived, 999, 'new', viewer=None) == []


# --------------------------------------------------------------------------
# the live tree
# --------------------------------------------------------------------------

class TestAssemblingTheTree:
    def test_replies_are_nested_under_their_parents(self, env):
        first = a_reply(env, body='first')
        second = a_reply(env, body='second', parent=first)
        a_reply(env, body='third', parent=second)
        tree = post_replies(env.post, 'new', viewer=None)
        assert bodies(tree) == {'first'}
        assert tree[0]['replies'][0]['comment'].body == 'second'

    def test_a_reply_whose_parent_is_not_in_the_list(self, env):
        """The parent can be filtered out from under it, and an orphan is
        dropped rather than promoted to the top."""
        orphan = a_reply(env, body='orphan')
        orphan.parent_id = 999999
        db.session.commit()
        assert bodies(post_replies(env.post, 'new', viewer=None)) == set()

    @pytest.mark.parametrize('sort_by', ['hot', 'top', 'new', 'old'])
    def test_every_sort_is_accepted(self, env, sort_by):
        a_reply(env, body='only one')
        assert bodies(post_replies(env.post, sort_by, viewer=None)) == \
            {'only one'}

    def test_a_sort_nobody_offers_is_not_an_error(self, env):
        a_reply(env, body='only one')
        assert bodies(post_replies(env.post, 'sideways', viewer=None)) == \
            {'only one'}


class TestWhatAReaderIsShown:
    def test_a_reply_from_somebody_they_block(self, env):
        from app.models import UserBlock
        blocked = make_user(env.baseline.instance_local, 'nuisance',
                            local=True)
        db.session.commit()
        reply = make_post_reply(env.post, blocked, body='from a blocked user')
        db.session.add(UserBlock(blocker_id=env.reader.id,
                                 blocked_id=blocked.id))
        a_reply(env, body='from somebody else')
        db.session.commit()
        tree = post_replies(env.post, 'new', viewer=env.reader)
        assert bodies(tree) == {'from somebody else'}

    def test_a_reply_from_an_instance_they_block(self, env):
        from app.models import InstanceBlock
        remote = env.baseline.instance_remote
        a_reply(env, body='from a blocked instance', instance_id=remote.id)
        a_reply(env, body='from here')
        db.session.add(InstanceBlock(user_id=env.reader.id,
                                     instance_id=remote.id))
        db.session.commit()
        tree = post_replies(env.post, 'new', viewer=env.reader)
        assert bodies(tree) == {'from here'}

    def test_a_reply_from_a_bot_when_they_ignore_bots(self, env):
        a_reply(env, body='from a bot', from_bot=True)
        a_reply(env, body='from a person')
        env.reader.ignore_bots = 1
        db.session.commit()
        tree = post_replies(env.post, 'new', viewer=env.reader)
        assert bodies(tree) == {'from a person'}

    def test_a_downvoted_reply_when_they_have_set_a_threshold(self, env):
        a_reply(env, body='well received', score=10)
        a_reply(env, body='badly received', score=-10, collapsible=True)
        env.reader.reply_hide_threshold = -5
        db.session.commit()
        tree = post_replies(env.post, 'new', viewer=env.reader)
        assert bodies(tree) == {'well received'}

    def test_a_moderator_is_shown_it_anyway(self, env):
        a_reply(env, body='badly received', score=-10, collapsible=True)
        env.reader.reply_hide_threshold = -5
        db.session.commit()
        make_community_member(env.reader, env.community, is_moderator=True)
        db.session.commit()
        tree = post_replies(env.post, 'new', viewer=env.reader)
        assert bodies(tree) == {'badly received'}

    def test_and_so_is_an_admin(self, env):
        a_reply(env, body='badly received', score=-10, collapsible=True)
        admin = env.baseline.user1
        admin.reply_hide_threshold = -5
        db.session.commit()
        g.admin_ids = [admin.id]
        tree = post_replies(env.post, 'new', viewer=admin)
        assert bodies(tree) == {'badly received'}

    def test_a_reply_in_a_language_they_do_not_read(self, env):
        english = Language(code='en', name='English')
        french = Language(code='fr', name='French')
        db.session.add_all([english, french])
        db.session.commit()
        a_reply(env, body='in english', language_id=english.id)
        a_reply(env, body='in french', language_id=french.id)
        a_reply(env, body='in no language at all', language_id=None)
        env.reader.read_language_ids = [english.id]
        db.session.commit()
        tree = post_replies(env.post, 'new', viewer=env.reader)
        assert bodies(tree) == {'in english', 'in no language at all'}

    def test_a_reader_who_reads_everything(self, env):
        a_reply(env, body='anything')
        env.reader.read_language_ids = []
        db.session.commit()
        assert bodies(post_replies(env.post, 'new',
                                   viewer=env.reader)) == {'anything'}


class TestWhatAnAnonymousVisitorIsShown:
    def test_a_reply_from_a_silenced_instance_is_hidden(self, env):
        from app.models import Instance
        remote = env.baseline.instance_remote
        remote.posting_warning = None
        remote.dormant = False
        db.session.commit()
        a_reply(env, body='from a silenced instance', instance_id=remote.id)
        a_reply(env, body='from here')
        db.session.commit()
        with patch('app.post.util.silenced_instances', return_value=[remote.id]):
            tree = post_replies(env.post, 'new', viewer=None)
        assert bodies(tree) == {'from here'}

    def test_a_heavily_downvoted_reply_is_shown_anyway(self, env):
        """D1277, pinned as it stands. `comments.filter(PostReply.score >
        -20)` discards its result, so the line has never run. Changing that
        is a product decision, not a defect fix, and this test is here to
        make the change visible when somebody makes it."""
        a_reply(env, body='deeply unpopular', score=-500)
        assert bodies(post_replies(env.post, 'new', viewer=None)) == \
            {'deeply unpopular'}


class TestOneBranchOfTheTree:
    def test_the_reply_asked_for_and_what_hangs_off_it(self, env):
        first = a_reply(env, body='first')
        second = a_reply(env, body='second', parent=first)
        a_reply(env, body='unrelated')
        db.session.commit()
        tree = get_comment_branch(env.post, first.id, 'new', viewer=None)
        assert bodies(tree) == {'first'}
        assert tree[0]['replies'][0]['comment'].body == 'second'

    def test_a_reply_nobody_wrote(self, env):
        assert get_comment_branch(env.post, 999999, 'new', viewer=None) == []

    @pytest.mark.parametrize('sort_by', ['hot', 'top', 'new', 'old'])
    def test_every_sort_is_accepted(self, env, sort_by):
        only = a_reply(env, body='only one')
        assert bodies(get_comment_branch(env.post, only.id, sort_by,
                                         viewer=None)) == {'only one'}

    def test_a_reader_who_blocks_the_author_of_a_child(self, env):
        from app.models import UserBlock
        first = a_reply(env, body='first')
        blocked = make_user(env.baseline.instance_local, 'nuisance',
                            local=True)
        db.session.commit()
        child = make_post_reply(env.post, blocked, body='from a blocked user')
        child.parent_id = first.id
        child.depth = 1
        db.session.add(UserBlock(blocker_id=env.reader.id,
                                 blocked_id=blocked.id))
        db.session.commit()
        tree = get_comment_branch(env.post, first.id, 'new',
                                 viewer=env.reader)
        assert tree[0]['replies'] == []

    def test_a_reader_with_a_hide_threshold(self, env):
        first = a_reply(env, body='first', score=10)
        a_reply(env, body='badly received', score=-10, parent=first)
        env.reader.reply_hide_threshold = -5
        db.session.commit()
        tree = get_comment_branch(env.post, first.id, 'new',
                                 viewer=env.reader)
        assert tree[0]['replies'] == []

    def test_a_reader_who_reads_one_language(self, env):
        english = Language(code='en', name='English')
        french = Language(code='fr', name='French')
        db.session.add_all([english, french])
        db.session.commit()
        first = a_reply(env, body='first', language_id=english.id)
        a_reply(env, body='in french', language_id=french.id, parent=first)
        env.reader.read_language_ids = [english.id]
        db.session.commit()
        tree = get_comment_branch(env.post, first.id, 'new',
                                 viewer=env.reader)
        assert tree[0]['replies'] == []

    def test_a_reader_who_blocks_an_instance(self, env):
        from app.models import InstanceBlock
        remote = env.baseline.instance_remote
        first = a_reply(env, body='first')
        a_reply(env, body='from a blocked instance', parent=first,
                instance_id=remote.id)
        db.session.add(InstanceBlock(user_id=env.reader.id,
                                     instance_id=remote.id))
        db.session.commit()
        tree = get_comment_branch(env.post, first.id, 'new',
                                 viewer=env.reader)
        assert tree[0]['replies'] == []

    def test_a_reader_who_ignores_bots(self, env):
        first = a_reply(env, body='first')
        a_reply(env, body='from a bot', parent=first, from_bot=True)
        env.reader.ignore_bots = 1
        db.session.commit()
        tree = get_comment_branch(env.post, first.id, 'new',
                                 viewer=env.reader)
        assert tree[0]['replies'] == []

    def test_an_anonymous_visitor_sees_a_downvoted_branch(self, env):
        """D1277's second site, pinned the same way as the first."""
        only = a_reply(env, body='deeply unpopular', score=-500)
        assert bodies(get_comment_branch(env.post, only.id, 'new',
                                         viewer=None)) == {'deeply unpopular'}


# --------------------------------------------------------------------------
# the small helpers
# --------------------------------------------------------------------------

class TestCountingReplies:
    def test_a_post_with_none(self, env):
        assert get_post_reply_count(env.post.id) == 0

    def test_a_post_with_some(self, env):
        a_reply(env, body='one')
        a_reply(env, body='two')
        assert get_post_reply_count(env.post.id) == 2

    def test_deleted_replies_are_not_counted(self, env):
        a_reply(env, body='one')
        a_reply(env, body='two', deleted=True)
        assert get_post_reply_count(env.post.id) == 1

    def test_a_post_nobody_wrote(self, env):
        assert get_post_reply_count(999999) == 0


class TestDescribingAPost:
    def test_the_tags_on_a_post(self, env):
        from app.models import Tag
        tag = Tag(name='news', display_as='News')
        db.session.add(tag)
        env.post.tags.append(tag)
        db.session.commit()
        assert tags_to_string(env.post) == 'News'

    def test_a_post_with_no_tags(self, env):
        assert tags_to_string(env.post) is None

    def test_the_flair_on_a_post(self, env):
        make_post_flair(env.post, name='Discussion')
        db.session.commit()
        assert flair_to_string(env.post) == 'Discussion'

    def test_a_post_with_no_flair(self, env):
        assert flair_to_string(env.post) is None


class TestArchiveLinks:
    @pytest.mark.parametrize('body', [
        'see https://archive.ph/abc',
        'see https://12ft.io/proxy?q=x',
        'see https://removepaywalls.com/x',
    ])
    def test_a_body_that_already_links_to_one(self, body):
        assert body_has_no_archive_link(body) is False

    def test_a_body_that_does_not(self):
        assert body_has_no_archive_link('just some words') is True

    def test_a_body_with_nothing_in_it(self):
        assert body_has_no_archive_link('') is True
        assert body_has_no_archive_link(None) is True

    def test_the_link_that_is_offered(self):
        assert generate_archive_link('https://example.test/x') == \
            'https://www.removepaywall.com/search?url=https://example.test/x'


class TestWhichFormAPostTypeNeeds:
    @pytest.mark.parametrize('post_type,url,expected', [
        (POST_TYPE_LINK, 'https://example.test/x', 'link'),
        (POST_TYPE_IMAGE, 'https://example.test/x.png', 'image'),
        (POST_TYPE_VIDEO, 'https://example.test/x.mp4', 'video'),
        (POST_TYPE_POLL, None, 'poll'),
        (POST_TYPE_ARTICLE, None, ''),
    ])
    def test_each_type(self, post_type, url, expected):
        assert post_type_to_form_url_type(post_type, url) == expected

    def test_an_article_pointing_at_a_video_host_is_a_link(self):
        assert post_type_to_form_url_type(
            POST_TYPE_ARTICLE, 'https://www.youtube.com/watch?v=x') == 'link'


class TestTheEdgesOfOneBranch:
    def test_an_archive_that_holds_no_replies_key(self, env, tmp_path):
        """The post is archived but the archive says nothing about replies,
        so the database answers instead."""
        path = tmp_path / 'no-replies.json.gz'
        with gzip.open(path, 'wb') as handle:
            handle.write(orjson.dumps({'post': {'id': 1}}))
        env.post.archived = str(path)
        db.session.commit()
        only = a_reply(env, body='a live reply')
        assert bodies(get_comment_branch(env.post, only.id, 'new',
                                         viewer=None)) == {'a live reply'}

    def test_a_moderator_reading_a_branch_keeps_the_downvoted_replies(
            self, env):
        first = a_reply(env, body='first', score=10)
        a_reply(env, body='badly received', score=-10, parent=first)
        env.reader.reply_hide_threshold = -5
        db.session.commit()
        make_community_member(env.reader, env.community, is_moderator=True)
        db.session.commit()
        tree = get_comment_branch(env.post, first.id, 'new',
                                 viewer=env.reader)
        assert tree[0]['replies'][0]['comment'].body == 'badly received'

    def test_a_sort_nobody_offers(self, env):
        only = a_reply(env, body='only one')
        assert bodies(get_comment_branch(env.post, only.id, 'sideways',
                                         viewer=None)) == {'only one'}

    def test_a_reply_whose_parent_is_not_in_the_branch(self, env):
        wanted = a_reply(env, body='the one asked for')
        orphan = a_reply(env, body='orphan')
        orphan.parent_id = 999999
        db.session.commit()
        tree = get_comment_branch(env.post, wanted.id, 'new', viewer=None)
        assert bodies(tree) == {'the one asked for'}
