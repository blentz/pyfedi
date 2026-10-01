"""Filling a new community with what the remote instance already has.

Sub-project 96 -- the body of `retrieve_mods_and_backfill` in
`app/community/util.py`. Its outer guards were slice B of sub-project 87
(`tests/test_community_backfill.py`); this is what happens once they pass:
moderators are made, an outbox is read, and every entry in it becomes a post
and, where the remote offers them, its replies.

Everything this task reads is JSON another instance sent, and it runs in a
Celery worker, so anything it raises is a traceback in a log and a community
that stays empty.

Six defects, measured first:

* `if not activity: return` -- one entry the task could make nothing of threw
  away every entry after it. The peertube branch beside it was given a
  `continue` in D1260 and this one was left (D1286);
* `replies['type']`, `reply_data['id']`, `reply_data['attributedTo']`,
  `reply_data['language']['identifier']` and `item['id']` in the featured
  collection were each read with no membership test, and each one killed the
  task (D1285);
* `community.post_count > 0` then `.first().posted_at` -- `post_count` is a
  counter, not a count, so it can be positive with no rows to read a date off
  (D1285);
* and a reply written in a language this instance had never seen kept no
  language at all: `find_language_or_create` adds the row without flushing,
  so `language.id` was None by the time it was read (D1287).

One equivalent mutant: removing the `filter_by(ap_id=...)` skip above each
reply survives, because `PostReply.ap_id` is unique, so the second copy is
refused by the database, the IntegrityError is caught by the `except` around
`PostReply.new`, and the loop carries on. The guard's value is avoiding that
round trip and the rollback it costs, which no assertion can see.
"""
from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.community.util import retrieve_mods_and_backfill
from app.models import (Community, CommunityMember, Language, Post, PostReply,
                        Site, User)
from tests.factories import make_community, make_user

MODS = 'https://remote.test/c/faraway/moderators'
OUTBOX = 'https://remote.test/c/faraway/outbox'
FEATURED = 'https://remote.test/c/faraway/featured'
REPLIES = 'https://remote.test/p/1/replies'
AUTHOR = 'https://remote.test/u/someone'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'

EMPTY_MODS = {'type': 'OrderedCollection', 'orderedItems': []}


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('faraway', host='remote.test')
    community.ap_id = 'faraway@remote.test'
    community.ap_moderators_url = MODS
    community.ap_outbox_url = OUTBOX
    community.ap_profile_id = 'https://remote.test/c/faraway'
    author = make_user(api_baseline.instance_remote, 'someone')
    author.ap_id = 'someone@remote.test'
    author.ap_domain = 'remote.test'  # an inbound author's host, which the Create gate reads (PERM-3)
    author.ap_profile_id = AUTHOR
    author.ap_public_url = AUTHOR
    db.session.commit()
    return SimpleNamespace(community=community, author=author,
                           baseline=api_baseline)


def backfill(community, answers, server='remote.test', name='faraway',
             community_json=None):
    """Run the task with every remote fetch answered from `answers`."""
    def fake(url, *args, **kwargs):
        return answers.get(url)

    with patch('app.community.util.remote_object_to_json', side_effect=fake), \
            patch('app.community.util.sleep', lambda seconds: None):
        retrieve_mods_and_backfill(community.id, server, name,
                                   community_json=community_json)


def outbox(*items, **extra):
    collection = {'type': 'OrderedCollection', 'orderedItems': list(items)}
    collection.update(extra)
    return collection


def a_post(post_id='https://remote.test/p/1', **overrides):
    post = {'id': post_id, 'type': 'Page', 'name': 'a post',
            'attributedTo': AUTHOR, 'to': [PUBLIC],
            'published': '2026-01-01T00:00:00Z'}
    post.update(overrides)
    return post


def an_announce(post=None, post_id='https://remote.test/p/1'):
    post = post if post is not None else a_post(post_id)
    return {'id': f"{post['id']}/announce", 'type': 'Announce',
            'object': {'id': post['id'], 'type': 'Create', 'object': post}}


def a_reply(reply_id='https://remote.test/r/1', **overrides):
    reply = {'id': reply_id, 'type': 'Note', 'attributedTo': AUTHOR,
             'to': [PUBLIC], 'inReplyTo': 'https://remote.test/p/1',
             'content': 'a reply'}
    reply.update(overrides)
    return reply


def replies(*items):
    return {'type': 'OrderedCollection', 'orderedItems': list(items)}


def titles(community):
    return {post.title for post in
            Post.query.filter_by(community_id=community.id).all()}


def bodies(community):
    return {reply.body for reply in
            PostReply.query.filter_by(community_id=community.id).all()}


class TestTheOutbox:
    def test_one_post_is_created(self, env):
        backfill(env.community, {MODS: EMPTY_MODS,
                                 OUTBOX: outbox(an_announce())})
        assert titles(env.community) == {'a post'}

    def test_its_author_is_the_account_the_remote_names(self, env):
        backfill(env.community, {MODS: EMPTY_MODS,
                                 OUTBOX: outbox(an_announce())})
        post = Post.query.filter_by(community_id=env.community.id).one()
        assert post.user_id == env.author.id

    def test_the_date_the_remote_gave_it(self, env):
        backfill(env.community, {MODS: EMPTY_MODS,
                                 OUTBOX: outbox(an_announce())})
        post = Post.query.filter_by(community_id=env.community.id).one()
        assert post.posted_at.year == 2026

    def test_several_posts(self, env):
        announces = [an_announce(a_post(f'https://remote.test/p/{index}',
                                        name=f'post {index}'))
                     for index in range(1, 4)]
        backfill(env.community, {MODS: EMPTY_MODS, OUTBOX: outbox(*announces)})
        assert titles(env.community) == {'post 1', 'post 2', 'post 3'}

    def test_an_entry_the_task_can_make_nothing_of_is_skipped(self, env):
        """D1286. This was `return`, so one malformed entry threw away every
        entry after it -- and an outbox is in whatever order the remote sent
        it, so which posts survived was luck."""
        good = an_announce(a_post('https://remote.test/p/2', name='a good one'))
        backfill(env.community,
                 {MODS: EMPTY_MODS,
                  OUTBOX: outbox({'id': 'x', 'type': 'Announce'}, good)})
        assert 'a good one' in titles(env.community)

    def test_an_entry_naming_no_author(self, env):
        good = an_announce(a_post('https://remote.test/p/2', name='a good one'))
        bad = an_announce(a_post('https://remote.test/p/3', name='no author',
                                 attributedTo=None))
        del bad['object']['object']['attributedTo']
        backfill(env.community, {MODS: EMPTY_MODS, OUTBOX: outbox(bad, good)})
        assert titles(env.community) == {'a good one'}

    def test_an_entry_whose_author_is_a_list(self, env):
        """`attributedTo` is only followed when it is a string."""
        bad = an_announce(a_post('https://remote.test/p/3', name='a list',
                                 attributedTo=[AUTHOR]))
        backfill(env.community, {MODS: EMPTY_MODS, OUTBOX: outbox(bad)})
        assert titles(env.community) == set()

    def test_a_post_attributed_to_somebody_here(self, env):
        """A local account's post is already on this instance; taking it back
        off the remote's outbox would duplicate it."""
        local = env.baseline.user2
        local.ap_profile_id = f"https://{current_app.config['SERVER_NAME']}/u/{local.user_name}"
        db.session.commit()
        announce = an_announce(a_post(attributedTo=local.ap_profile_id))
        backfill(env.community, {MODS: EMPTY_MODS, OUTBOX: outbox(announce)})
        assert titles(env.community) == set()

    def test_an_outbox_that_says_it_holds_nothing(self, env):
        backfill(env.community, {MODS: EMPTY_MODS,
                                 OUTBOX: outbox(an_announce(), totalItems=0)})
        assert titles(env.community) == set()

    def test_a_paginated_outbox_is_followed_to_its_first_page(self, env):
        page = 'https://remote.test/c/faraway/outbox?page=1'
        backfill(env.community,
                 {MODS: EMPTY_MODS,
                  OUTBOX: {'type': 'OrderedCollection', 'first': page},
                  page: {'type': 'OrderedCollectionPage',
                         'orderedItems': [an_announce()]}})
        assert titles(env.community) == {'a post'}

    def test_a_collection_of_a_type_nobody_knows(self, env):
        backfill(env.community,
                 {MODS: EMPTY_MODS,
                  OUTBOX: {'type': 'Collection',
                           'orderedItems': [an_announce()]}})
        assert titles(env.community) == set()

    def test_no_more_than_the_cap_is_taken(self, env, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', True)   # the cap is 2 there
        announces = [an_announce(a_post(f'https://remote.test/p/{index}',
                                        name=f'post {index}'))
                     for index in range(1, 6)]
        backfill(env.community, {MODS: EMPTY_MODS, OUTBOX: outbox(*announces)})
        assert len(titles(env.community)) == 2

    def test_a_post_the_instance_refuses_does_not_stop_the_rest(self, env):
        """`create_post` raising is caught per entry."""
        good = an_announce(a_post('https://remote.test/p/2', name='a good one'))
        with patch('app.community.util.create_post',
                   side_effect=[Exception('refused'), None]):
            backfill(env.community,
                     {MODS: EMPTY_MODS,
                      OUTBOX: outbox(an_announce(), good)})


class TestWhatTheRemoteSaysAboutReplies:
    def announce_with_replies(self):
        return an_announce(a_post(replies=REPLIES))

    def test_one_reply_is_created(self, env):
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(self.announce_with_replies()),
                  REPLIES: replies(a_reply())})
        assert bodies(env.community) == {'a reply'}

    def test_a_collection_with_no_type(self, env):
        """D1285. `replies['type']` was a KeyError, one line before a
        membership test on the very next key."""
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(self.announce_with_replies()),
                  REPLIES: {'orderedItems': [a_reply()]}})
        assert titles(env.community) == {'a post'}

    def test_a_reply_with_no_id(self, env):
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(self.announce_with_replies()),
                  REPLIES: replies({'content': 'no id'}, a_reply())})
        assert bodies(env.community) == {'a reply'}

    def test_a_reply_that_is_not_an_object(self, env):
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(self.announce_with_replies()),
                  REPLIES: replies('a string', a_reply())})
        assert bodies(env.community) == {'a reply'}

    def test_a_reply_with_no_author(self, env):
        no_author = a_reply('https://remote.test/r/2')
        del no_author['attributedTo']
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(self.announce_with_replies()),
                  REPLIES: replies(no_author, a_reply())})
        assert bodies(env.community) == {'a reply'}

    def test_a_reply_that_is_not_public(self, env):
        private = a_reply('https://remote.test/r/2', to=[AUTHOR],
                          content='for one person')
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(self.announce_with_replies()),
                  REPLIES: replies(private, a_reply())})
        assert 'for one person' not in bodies(env.community)

    def test_a_reply_this_instance_already_holds(self, env):
        answers = {MODS: EMPTY_MODS,
                   OUTBOX: outbox(self.announce_with_replies()),
                   REPLIES: replies(a_reply())}
        backfill(env.community, answers)
        backfill(env.community, answers)
        assert PostReply.query.filter_by(
            ap_id='https://remote.test/r/1').count() == 1

    def test_one_whose_text_has_changed_since_is_still_not_taken_twice(
            self, env):
        """The skip is on the reply's id, not on its text: a remote that
        edits a reply and re-serves the collection must not produce a second
        row carrying the same `ap_id`."""
        first = {MODS: EMPTY_MODS,
                 OUTBOX: outbox(self.announce_with_replies()),
                 REPLIES: replies(a_reply())}
        second = dict(first, **{REPLIES: replies(a_reply(content='edited'))})
        backfill(env.community, first)
        backfill(env.community, second)
        assert PostReply.query.filter_by(
            ap_id='https://remote.test/r/1').count() == 1
        assert bodies(env.community) == {'a reply'}

    def test_a_reply_written_in_markdown(self, env):
        markdown = a_reply(content='<p>rendered</p>',
                           source={'mediaType': 'text/markdown',
                                   'content': '**the source**'})
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(self.announce_with_replies()),
                  REPLIES: replies(markdown)})
        assert bodies(env.community) == {'**the source**'}

    def test_content_that_is_not_wrapped_in_a_paragraph(self, env):
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(self.announce_with_replies()),
                  REPLIES: replies(a_reply(content='bare text'))})
        reply = PostReply.query.filter_by(
            community_id=env.community.id).one()
        assert reply.body_html.startswith('<p>')

    def test_content_that_is_already_a_blockquote(self, env):
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(self.announce_with_replies()),
                  REPLIES: replies(a_reply(
                      content='<blockquote>quoted</blockquote>'))})
        assert PostReply.query.filter_by(
            community_id=env.community.id).count() == 1

    def test_a_reply_to_another_reply(self, env):
        first = a_reply()
        second = a_reply('https://remote.test/r/2', content='the second',
                         inReplyTo='https://remote.test/r/1')
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(self.announce_with_replies()),
                  REPLIES: replies(first, second)})
        child = PostReply.query.filter_by(
            ap_id='https://remote.test/r/2').one()
        parent = PostReply.query.filter_by(
            ap_id='https://remote.test/r/1').one()
        assert child.parent_id == parent.id

    def test_a_reply_to_something_this_instance_has_never_seen(self, env):
        orphan = a_reply(inReplyTo='https://remote.test/r/999')
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(self.announce_with_replies()),
                  REPLIES: replies(orphan)})
        reply = PostReply.query.filter_by(
            community_id=env.community.id).one()
        assert reply.parent_id is None

    def test_a_reply_with_no_in_reply_to_at_all(self, env):
        loose = a_reply()
        del loose['inReplyTo']
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(self.announce_with_replies()),
                  REPLIES: replies(loose)})
        assert bodies(env.community) == {'a reply'}

    def test_a_reply_in_a_named_language(self, env):
        """D1287. A language this instance has never seen is added to the
        session and not flushed, so `language.id` was None and the reply was
        stored with no language at all."""
        tagged = a_reply(language={'identifier': 'fr', 'name': 'French'})
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(self.announce_with_replies()),
                  REPLIES: replies(tagged)})
        reply = PostReply.query.filter_by(
            community_id=env.community.id).one()
        assert reply.language.code == 'fr'

    def test_a_language_the_remote_only_half_described(self, env):
        """D1285. `reply_data['language']['identifier']` was a KeyError."""
        tagged = a_reply(language={'name': 'French'})
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(self.announce_with_replies()),
                  REPLIES: replies(tagged)})
        assert bodies(env.community) == {'a reply'}

    def test_a_language_that_is_not_an_object(self, env):
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(self.announce_with_replies()),
                  REPLIES: replies(a_reply(language='fr'))})
        assert bodies(env.community) == {'a reply'}

    def test_a_distinguished_reply(self, env):
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(self.announce_with_replies()),
                  REPLIES: replies(a_reply(distinguished=True))})
        reply = PostReply.query.filter_by(
            community_id=env.community.id).one()
        assert reply.distinguished is True

    def test_a_replies_url_that_answers_nothing(self, env):
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(self.announce_with_replies()),
                  REPLIES: None})
        assert titles(env.community) == {'a post'}

    def test_replies_given_as_a_collection_rather_than_a_url(self, env):
        """Only a string is followed; an inline collection is left alone."""
        inline = an_announce(a_post(replies={'type': 'OrderedCollection',
                                             'orderedItems': [a_reply()]}))
        backfill(env.community, {MODS: EMPTY_MODS, OUTBOX: outbox(inline)})
        assert bodies(env.community) == set()

    def test_a_post_with_no_published_date_gets_no_replies(self, env):
        """The whole reply block sits under `if 'published' in activity`."""
        undated = a_post(replies=REPLIES)
        del undated['published']
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(an_announce(undated)),
                  REPLIES: replies(a_reply())})
        assert bodies(env.community) == set()

    def test_a_reply_the_instance_refuses_does_not_stop_the_rest(self, env):
        with patch.object(PostReply, 'new',
                          side_effect=Exception('refused')):
            backfill(env.community,
                     {MODS: EMPTY_MODS,
                      OUTBOX: outbox(self.announce_with_replies()),
                      REPLIES: replies(a_reply())})
        assert titles(env.community) == {'a post'}


class TestTheFeaturedCollection:
    def test_a_featured_post_is_pinned(self, env):
        env.community.ap_featured_url = FEATURED
        db.session.commit()
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(an_announce()),
                  FEATURED: {'type': 'OrderedCollection',
                             'orderedItems': [{'id': 'https://remote.test/p/1'}]}})
        post = Post.query.filter_by(ap_id='https://remote.test/p/1').one()
        assert post.sticky is True

    def test_an_item_with_no_id(self, env):
        """D1285. `item['id']` was a KeyError."""
        env.community.ap_featured_url = FEATURED
        db.session.commit()
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(an_announce()),
                  FEATURED: {'type': 'OrderedCollection',
                             'orderedItems': [{'name': 'no id'}]}})
        assert titles(env.community) == {'a post'}

    def test_an_item_naming_a_post_nobody_has(self, env):
        env.community.ap_featured_url = FEATURED
        db.session.commit()
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(an_announce()),
                  FEATURED: {'type': 'OrderedCollection',
                             'orderedItems': [{'id': 'https://remote.test/p/9'}]}})
        assert titles(env.community) == {'a post'}

    def test_a_featured_url_that_answers_nothing(self, env):
        env.community.ap_featured_url = FEATURED
        db.session.commit()
        backfill(env.community, {MODS: EMPTY_MODS,
                                 OUTBOX: outbox(an_announce()),
                                 FEATURED: None})
        assert titles(env.community) == {'a post'}

    def test_a_featured_collection_of_another_type(self, env):
        env.community.ap_featured_url = FEATURED
        db.session.commit()
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(an_announce()),
                  FEATURED: {'type': 'Collection', 'orderedItems': []}})
        assert titles(env.community) == {'a post'}


class TestTheModerators:
    def test_one_named_in_the_collection_becomes_a_moderator(self, env):
        backfill(env.community,
                 {MODS: {'type': 'OrderedCollection',
                         'orderedItems': [AUTHOR]},
                  OUTBOX: None})
        membership = CommunityMember.query.filter_by(
            community_id=env.community.id, user_id=env.author.id).one()
        assert membership.is_moderator is True

    def test_one_who_is_already_a_member_is_promoted(self, env):
        from tests.factories import make_community_member
        make_community_member(env.author, env.community)
        db.session.commit()
        backfill(env.community,
                 {MODS: {'type': 'OrderedCollection',
                         'orderedItems': [AUTHOR]},
                  OUTBOX: None})
        membership = CommunityMember.query.filter_by(
            community_id=env.community.id, user_id=env.author.id).one()
        assert membership.is_moderator is True

    def test_one_this_instance_cannot_resolve_is_skipped(self, env):
        with patch('app.community.util.find_actor_or_create',
                   return_value=None):
            backfill(env.community,
                     {MODS: {'type': 'OrderedCollection',
                             'orderedItems': ['https://remote.test/u/ghost']},
                      OUTBOX: None})
        assert CommunityMember.query.filter_by(
            community_id=env.community.id).count() == 0

    def test_moderators_named_in_the_actor_document_instead(self, env):
        env.community.ap_moderators_url = None
        db.session.commit()
        backfill(env.community, {OUTBOX: None},
                 community_json={'attributedTo': [
                     {'type': 'Person', 'id': AUTHOR}]})
        membership = CommunityMember.query.filter_by(
            community_id=env.community.id, user_id=env.author.id).one()
        assert membership.is_moderator is True

    def test_an_entry_in_that_document_that_is_not_a_person(self, env):
        env.community.ap_moderators_url = None
        db.session.commit()
        backfill(env.community, {OUTBOX: None},
                 community_json={'attributedTo': [
                     {'type': 'Service', 'id': AUTHOR}]})
        assert CommunityMember.query.filter_by(
            community_id=env.community.id).count() == 0

    def test_an_attributed_to_that_is_not_a_list(self, env):
        env.community.ap_moderators_url = None
        db.session.commit()
        backfill(env.community, {OUTBOX: None},
                 community_json={'attributedTo': AUTHOR})
        assert CommunityMember.query.filter_by(
            community_id=env.community.id).count() == 0


class TestCommunitiesThisInstanceWillNotFill:
    def test_an_nsfw_one_when_the_site_does_not_allow_them(self, env):
        env.community.nsfw = True
        db.session.get(Site, 1).enable_nsfw = False
        db.session.commit()
        backfill(env.community, {MODS: EMPTY_MODS,
                                 OUTBOX: outbox(an_announce())})
        assert titles(env.community) == set()

    def test_an_nsfl_one_when_the_site_does_not_allow_them(self, env):
        env.community.nsfl = True
        db.session.get(Site, 1).enable_nsfl = False
        db.session.commit()
        backfill(env.community, {MODS: EMPTY_MODS,
                                 OUTBOX: outbox(an_announce())})
        assert titles(env.community) == set()

    def test_an_nsfw_one_when_the_site_does(self, env):
        env.community.nsfw = True
        db.session.get(Site, 1).enable_nsfw = True
        db.session.commit()
        backfill(env.community, {MODS: EMPTY_MODS,
                                 OUTBOX: outbox(an_announce())})
        assert titles(env.community) == {'a post'}


class TestTheShapesOtherSoftwareSends:
    def test_a_peertube_channel_is_restricted_to_its_moderators(self, env):
        env.community.ap_profile_id = 'https://remote.test/video-channels/faraway'
        db.session.commit()
        backfill(env.community, {MODS: EMPTY_MODS, OUTBOX: None})
        db.session.expire_all()
        assert db.session.get(Community, env.community.id).restricted_to_mods \
            is True

    def test_a_peertube_entry_is_fetched_by_its_object_url(self, env):
        env.community.ap_profile_id = 'https://remote.test/video-channels/faraway'
        db.session.commit()
        video = 'https://remote.test/videos/watch/1'
        backfill(env.community,
                 {MODS: {'type': 'OrderedCollection', 'orderedItems': [AUTHOR]},
                  OUTBOX: outbox({'id': 'a', 'type': 'Announce',
                                  'object': video}),
                  video: a_post(video, name='a video')})
        assert 'a video' in titles(env.community)

    def test_a_guppe_group(self, env):
        env.community.ap_profile_id = 'https://ovo.st/club/faraway'
        db.session.commit()
        item = 'https://remote.test/p/5'
        backfill(env.community,
                 {MODS: EMPTY_MODS,
                  OUTBOX: outbox({'id': 'a', 'type': 'Announce',
                                  'object': item}),
                  item: a_post(item, name='from guppe')})
        assert 'from guppe' in titles(env.community)

    def test_a_wordpress_create(self, env):
        create = {'id': 'https://remote.test/c/1', 'type': 'Create',
                  'object': a_post('https://remote.test/p/7',
                                   name='from wordpress')}
        backfill(env.community, {MODS: EMPTY_MODS, OUTBOX: outbox(create)})
        assert 'from wordpress' in titles(env.community)


class TestWhatIsCountedAtTheEnd:
    def test_the_reply_count_on_each_post_is_recomputed(self, env):
        announce = an_announce(a_post(replies=REPLIES))
        backfill(env.community,
                 {MODS: EMPTY_MODS, OUTBOX: outbox(announce),
                  REPLIES: replies(a_reply(), a_reply('https://remote.test/r/2',
                                                      content='another'))})
        post = Post.query.filter_by(ap_id='https://remote.test/p/1').one()
        assert post.reply_count == 2

    def test_a_community_whose_counter_is_ahead_of_its_rows(self, env):
        """D1285. `post_count` is a counter kept by hand, so it can be
        positive with no rows behind it, and `.first().posted_at` on that was
        an AttributeError."""
        env.community.post_count = 5
        db.session.commit()
        backfill(env.community, {MODS: EMPTY_MODS, OUTBOX: outbox()})
        assert titles(env.community) == set()
