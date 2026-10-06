"""The helpers behind a community: tags, deletions, and federating them.

Sub-project 94 -- `app/community/util.py`, everything except the two image
functions and the backfill body, which are their own slices. The handle
lookup was sub-project 87 (`tests/test_community_search.py`) and the backfill
its slice B (`tests/test_community_backfill.py`).

Three defects, measured first:

* `tags_from_string` and `tags_from_string_old` read `tag[0]` on every
  comma-separated piece. `news,` -- one trailing comma, the most natural typo
  in a tags field -- and `,news` and `news,,sport` each produce an empty
  piece, and `tag[0]` on it was `IndexError: string index out of range`. The
  tags field on the post form is where this string comes from (D1279);
* `delete_post_from_community_task` asked `current_user.has_blocked_instance`
  while running in a Celery worker, where there is no request and
  `current_user` is None. Deleting a post from a LOCAL community with remote
  followers marked it deleted, committed, and then raised -- so the delete
  never federated and the remote copies stayed up. The sibling task for
  replies names the author instead, which is why only one of the two was
  broken (D1280);
* `send_to_remote_instance_task` guards `community` and then reads
  `instance.inbox` unguarded, though the instance row can be gone by the time
  a queued announce runs (D1281).
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.community.util import (actor_to_community, community_in_list,
                                community_theme_list,
                                delete_post_from_community,
                                delete_post_from_community_task,
                                delete_post_reply_from_community,
                                delete_post_reply_from_community_task,
                                end_poll_date, find_local_users,
                                find_potential_moderators, flair_from_form,
                                flairs_from_string,
                                get_community_theme_allowed,
                                hashtags_used_in_communities,
                                hashtags_used_in_community, is_bad_name,
                                normalize_font_size, remove_old_file,
                                send_to_remote_instance,
                                send_to_remote_instance_fast,
                                send_to_remote_instance_fast_task,
                                send_to_remote_instance_task,
                                search_for_community,
                                set_community_theme_allowed, tags_from_string,
                                tags_from_string_old)
from app.models import (Community, File, Instance, Post, PostReply, Site, Tag,
                        User)
from tests.factories import (make_community, make_community_flair,
                             make_community_member, make_file, make_instance,
                             make_post, make_post_reply, make_user)


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('probeland')
    author = api_baseline.user2
    db.session.commit()
    make_community_member(author, community)
    post = make_post(community, author, ap_id='https://test.piefed.local/v/1')
    db.session.commit()
    return SimpleNamespace(app=app, community=community, author=author,
                           post=post, baseline=api_baseline)


# --------------------------------------------------------------------------
# tags and flair
# --------------------------------------------------------------------------

class TestTagsTypedIntoAForm:
    """D1279. Every one of these is a string somebody can type."""

    def test_one_tag(self, env):
        assert tags_from_string('news') == [{'type': 'Hashtag',
                                             'name': 'news'}]

    def test_several(self, env):
        assert [t['name'] for t in tags_from_string('news,sport')] == \
            ['news', 'sport']

    def test_a_leading_hash_is_dropped(self, env):
        assert tags_from_string('#news')[0]['name'] == 'news'

    def test_space_around_them_is_ignored(self, env):
        assert [t['name'] for t in tags_from_string(' news , sport ')] == \
            ['news', 'sport']

    def test_nothing_at_all(self, env):
        assert tags_from_string('') == []
        assert tags_from_string('   ') == []

    @pytest.mark.parametrize('tags', ['news,', ',news', 'news,,sport', ',',
                                      ',,', ' , ', ',,,news,,,'])
    def test_an_empty_piece_between_commas(self, env, tags):
        """`tag[0]` on it was `IndexError: string index out of range`."""
        assert isinstance(tags_from_string(tags), list)

    def test_a_trailing_comma_still_yields_the_tag_before_it(self, env):
        assert [t['name'] for t in tags_from_string('news,')] == ['news']

    def test_a_hash_on_its_own_is_not_a_tag(self, env):
        assert tags_from_string('#') == []


class TestTheOlderTagParser:
    """The one the post form actually calls."""

    def test_one_tag(self, env):
        assert [t.name for t in tags_from_string_old('news')] == ['news']

    def test_nothing_at_all(self, env):
        assert tags_from_string_old('') == []
        assert tags_from_string_old(None) == []
        assert tags_from_string_old('   ') == []

    def test_a_tag_that_cannot_be_made_is_left_out(self, env):
        """find_hashtag_or_create answering nothing (an unusable name) adds nothing to the list."""
        with patch('app.community.util.find_hashtag_or_create', return_value=None):
            assert tags_from_string_old('news,sport') == []

    def test_the_same_tag_twice_is_kept_once(self, env):
        """D1282. The dedupe was `tag_to_append not in return_value`, which
        compares OBJECTS, and `find_hashtag_or_create` queries without
        flushing -- so the second `news` was a second pending Tag row. Nothing
        stops that being written: `Tag.name` carries an index, not a unique
        constraint."""
        assert len(tags_from_string_old('news,news')) == 1
        db.session.commit()
        assert Tag.query.filter_by(name='news').count() == 1

    def test_the_same_tag_in_two_cases(self, env):
        assert len(tags_from_string_old('News,news')) == 1

    def test_a_leading_hash_is_dropped(self, env):
        assert [t.name for t in tags_from_string_old('#news')] == ['news']

    @pytest.mark.parametrize('tags', [',news', 'news,,sport', ',', ',,',
                                      ' , ', ',,,news,,,'])
    def test_an_empty_piece_between_commas(self, env, tags):
        """D1279. One trailing comma is stripped by the line above; none of
        these is."""
        assert isinstance(tags_from_string_old(tags), list)

    def test_a_trailing_comma_still_yields_the_tag_before_it(self, env):
        assert [t.name for t in tags_from_string_old('news,')] == ['news']


class TestFlair:
    def test_flair_named_by_its_id(self, env):
        flair = make_community_flair(env.community, name='discussion')
        db.session.commit()
        assert flair_from_form([flair.id]) == [flair]

    def test_no_flair_named(self, env):
        assert flair_from_form(None) == []
        assert flair_from_form([]) == []

    def test_flair_named_in_a_string(self, env):
        make_community_flair(env.community, name='discussion')
        db.session.commit()
        found = flairs_from_string('discussion', env.community.id)
        assert [f.flair for f in found] == ['discussion']

    def test_nothing_at_all(self, env):
        assert flairs_from_string(None, env.community.id) == []
        assert flairs_from_string('', env.community.id) == []
        assert flairs_from_string('   ', env.community.id) == []

    def test_a_trailing_comma(self, env):
        make_community_flair(env.community, name='discussion')
        db.session.commit()
        found = flairs_from_string('discussion,', env.community.id)
        assert [f.flair for f in found] == ['discussion']

    def test_flair_this_community_does_not_have(self, env):
        assert flairs_from_string('nosuchflair', env.community.id) == []

    def test_the_same_flair_twice_is_kept_once(self, env):
        make_community_flair(env.community, name='discussion')
        db.session.commit()
        found = flairs_from_string('discussion,discussion', env.community.id)
        assert len(found) == 1


class TestWhenAPollEnds:
    @pytest.mark.parametrize('choice,minutes', [
        ('30m', 30), ('1h', 60), ('6h', 360), ('12h', 720),
        ('1d', 1440), ('3d', 4320), ('7d', 10080)])
    def test_each_choice_the_form_offers(self, env, choice, minutes):
        from app.utils import utcnow
        ends = end_poll_date(choice)
        assert abs((ends - utcnow()) - timedelta(minutes=minutes)) < \
            timedelta(seconds=5)

    @pytest.mark.parametrize('choice', ['forever', '', None, '1y'])
    def test_a_choice_nobody_offers(self, env, choice):
        with pytest.raises(ValueError, match='Invalid choice'):
            end_poll_date(choice)


# --------------------------------------------------------------------------
# deleting, and telling the rest of the fediverse
# --------------------------------------------------------------------------

class TestDeletingAPost:
    @pytest.fixture
    def follower(self, env):
        """A remote instance following this local community."""
        instance = env.baseline.instance_remote
        instance.inbox = 'https://remote.test/inbox'
        instance.dormant = False
        instance.gone_forever = False
        remote_user = make_user(instance, 'follower')
        remote_user.ap_id = 'follower@remote.test'
        db.session.commit()
        make_community_member(remote_user, env.community)
        db.session.commit()
        return instance

    def test_the_post_is_marked_deleted_and_by_whom(self, env):
        with patch('app.community.util.send_post_request'), \
                patch('app.community.util.send_to_remote_instance'):
            delete_post_from_community_task(env.post.id, env.author.id)
        db.session.expire_all()
        post = db.session.get(Post, env.post.id)
        assert post.deleted is True
        assert post.deleted_by == env.author.id

    def test_a_local_only_community_tells_nobody(self, env, follower):
        """With a remote follower in the community: the local-only flag is
        the only thing standing between the delete and the wire."""
        env.community.local_only = True
        db.session.commit()
        with patch('app.community.util.send_post_request') as direct, \
                patch('app.community.util.send_to_remote_instance') as announce:
            delete_post_from_community_task(env.post.id, env.author.id)
        assert direct.call_count == 0
        assert announce.call_count == 0

    def test_a_remote_community_is_told_directly(self, env):
        env.community.ap_id = 'probeland@remote.test'
        env.community.ap_inbox_url = 'https://remote.test/c/probeland/inbox'
        env.community.ap_profile_id = 'https://remote.test/c/probeland'
        env.community.instance_id = env.baseline.instance_remote.id
        db.session.commit()
        with patch('app.community.util.send_post_request') as send:
            delete_post_from_community_task(env.post.id, env.author.id)
        assert send.call_count == 1
        assert send.call_args.args[0] == \
            'https://remote.test/c/probeland/inbox'
        assert send.call_args.args[1]['type'] == 'Delete'

    def test_a_local_community_announces_it_to_its_followers(self, env,
                                                             follower):
        """D1280. This asked `current_user.has_blocked_instance` in a Celery
        worker, where `current_user` is None, so the post was marked deleted
        and committed and then the task raised -- the delete never left the
        instance."""
        with patch('app.community.util.send_to_remote_instance') as send:
            delete_post_from_community_task(env.post.id, env.author.id)
        assert send.call_count == 1
        assert send.call_args.args[2]['type'] == 'Announce'
        assert send.call_args.args[2]['object']['type'] == 'Delete'

    def test_an_instance_the_deleting_account_blocks_is_not_told(self, env,
                                                                 follower):
        from app.models import InstanceBlock
        db.session.add(InstanceBlock(user_id=env.author.id,
                                     instance_id=follower.id))
        db.session.commit()
        with patch('app.community.util.send_to_remote_instance') as send:
            delete_post_from_community_task(env.post.id, env.author.id)
        assert send.call_count == 0

    def test_nor_is_one_this_instance_has_banned(self, env, follower):
        from app.models import BannedInstances
        db.session.add(BannedInstances(domain=follower.domain))
        db.session.commit()
        with patch('app.community.util.send_to_remote_instance') as send:
            delete_post_from_community_task(env.post.id, env.author.id)
        assert send.call_count == 0

    def test_nor_is_one_with_no_inbox(self, env, follower):
        follower.inbox = None
        db.session.commit()
        with patch('app.community.util.send_to_remote_instance') as send:
            delete_post_from_community_task(env.post.id, env.author.id)
        assert send.call_count == 0

    def test_a_failure_is_rolled_back_and_raised(self, env):
        env.community.ap_id = 'probeland@remote.test'
        env.community.ap_inbox_url = 'https://remote.test/inbox'
        env.community.ap_profile_id = 'https://remote.test/c/probeland'
        db.session.commit()
        with patch('app.community.util.send_post_request',
                   side_effect=Exception('the remote is down')):
            with pytest.raises(Exception, match='the remote is down'):
                delete_post_from_community_task(env.post.id, env.author.id)


class TestDeletingAReply:
    @pytest.fixture
    def reply(self, env):
        reply = make_post_reply(env.post, env.author, body='a reply')
        db.session.commit()
        return reply

    def test_it_is_marked_deleted_and_by_whom(self, env, reply):
        with patch('app.community.util.send_post_request'), \
                patch('app.community.util.send_to_remote_instance'):
            delete_post_reply_from_community_task(reply.id, env.author.id)
        db.session.expire_all()
        stored = db.session.get(PostReply, reply.id)
        assert stored.deleted is True
        assert stored.deleted_by == env.author.id

    def test_a_local_only_community_tells_nobody(self, env, reply):
        instance = env.baseline.instance_remote
        instance.inbox = 'https://remote.test/inbox'
        instance.dormant = False
        instance.gone_forever = False
        remote_user = make_user(instance, 'follower')
        remote_user.ap_id = 'follower@remote.test'
        db.session.commit()
        make_community_member(remote_user, env.community)
        env.community.local_only = True
        db.session.commit()
        with patch('app.community.util.send_post_request') as direct, \
                patch('app.community.util.send_to_remote_instance') as announce:
            delete_post_reply_from_community_task(reply.id, env.author.id)
        assert direct.call_count == 0
        assert announce.call_count == 0

    def test_a_local_community_announces_it_to_its_followers(self, env, reply):
        instance = env.baseline.instance_remote
        instance.inbox = 'https://remote.test/inbox'
        instance.dormant = False
        instance.gone_forever = False
        remote_user = make_user(instance, 'follower')
        remote_user.ap_id = 'follower@remote.test'
        db.session.commit()
        make_community_member(remote_user, env.community)
        db.session.commit()
        with patch('app.community.util.send_to_remote_instance') as announce:
            delete_post_reply_from_community_task(reply.id, env.author.id)
        assert announce.call_count == 1
        assert announce.call_args.args[2]['object']['object'] == reply.ap_id

    def test_a_remote_community_is_told_directly(self, env, reply):
        env.community.ap_id = 'probeland@remote.test'
        env.community.ap_inbox_url = 'https://remote.test/c/probeland/inbox'
        env.community.ap_profile_id = 'https://remote.test/c/probeland'
        db.session.commit()
        with patch('app.community.util.send_post_request') as send:
            delete_post_reply_from_community_task(reply.id, env.author.id)
        assert send.call_count == 1
        assert send.call_args.args[1]['object'] == reply.ap_id

    def test_an_instance_that_is_banned_is_not_told(self, env, reply):
        """Of the instances following a local community, one this instance has banned is skipped."""
        instance = env.baseline.instance_remote
        instance.inbox = 'https://remote.test/inbox'
        instance.dormant = False
        instance.gone_forever = False
        remote_user = make_user(instance, 'follower')
        remote_user.ap_id = 'follower@remote.test'
        db.session.commit()
        make_community_member(remote_user, env.community)
        db.session.commit()
        with patch('app.community.util.instance_banned', return_value=True), \
                patch('app.community.util.send_to_remote_instance') as announce:
            delete_post_reply_from_community_task(reply.id, env.author.id)
        assert announce.call_count == 0

    def test_a_failure_is_rolled_back_and_raised(self, env, reply):
        env.community.ap_id = 'probeland@remote.test'
        env.community.ap_inbox_url = 'https://remote.test/inbox'
        env.community.ap_profile_id = 'https://remote.test/c/probeland'
        db.session.commit()
        with patch('app.community.util.send_post_request',
                   side_effect=Exception('the remote is down')):
            with pytest.raises(Exception, match='the remote is down'):
                delete_post_reply_from_community_task(reply.id, env.author.id)


class TestHowADeleteIsDispatched:
    def test_in_debug_it_runs_here_and_now(self, env, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', True)
        with patch('app.community.util.delete_post_reply_from_community_task') \
                as task:
            delete_post_reply_from_community(7, 3)
        task.assert_called_once_with(7, 3)

    def test_otherwise_it_is_queued(self, env, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', False)
        with patch('app.community.util.delete_post_reply_from_community_task') \
                as task:
            delete_post_reply_from_community(7, 3)
        task.delay.assert_called_once_with(7, 3)


class TestSendingAnAnnounceToOneInstance:
    @pytest.fixture
    def instance(self, env):
        instance = env.baseline.instance_remote
        instance.inbox = 'https://remote.test/inbox'
        instance.dormant = False
        instance.gone_forever = False
        db.session.commit()
        return instance

    def test_it_is_signed_as_the_community(self, env, instance):
        with patch('app.community.util.send_post_request') as send:
            send_to_remote_instance_task(instance.id, env.community.id,
                                         {'type': 'Announce'})
        assert send.call_count == 1
        assert send.call_args.args[0] == 'https://remote.test/inbox'
        assert send.call_args.args[3] == \
            env.community.ap_profile_id + '#main-key'

    def test_a_community_that_is_gone(self, env, instance):
        with patch('app.community.util.send_post_request') as send:
            send_to_remote_instance_task(instance.id, 999999, {'type': 'x'})
        assert send.call_count == 0

    def test_an_instance_that_is_gone(self, env):
        """D1281. `instance.inbox` on None was an AttributeError, and a
        queued announce can outlive the row it names."""
        with patch('app.community.util.send_post_request') as send:
            send_to_remote_instance_task(999999, env.community.id,
                                         {'type': 'x'})
        assert send.call_count == 0

    def test_an_instance_with_no_inbox(self, env, instance):
        instance.inbox = None
        db.session.commit()
        with patch('app.community.util.send_post_request') as send:
            send_to_remote_instance_task(instance.id, env.community.id,
                                         {'type': 'x'})
        assert send.call_count == 0

    def test_an_instance_that_is_not_answering(self, env, instance):
        instance.gone_forever = True
        db.session.commit()
        with patch('app.community.util.send_post_request') as send:
            send_to_remote_instance_task(instance.id, env.community.id,
                                         {'type': 'x'})
        assert send.call_count == 0

    def test_an_instance_this_one_has_banned(self, env, instance):
        from app.models import BannedInstances
        db.session.add(BannedInstances(domain=instance.domain))
        db.session.commit()
        with patch('app.community.util.send_post_request') as send:
            send_to_remote_instance_task(instance.id, env.community.id,
                                         {'type': 'x'})
        assert send.call_count == 0

    def test_a_failure_is_raised(self, env, instance):
        with patch('app.community.util.send_post_request',
                   side_effect=Exception('the remote is down')):
            with pytest.raises(Exception, match='the remote is down'):
                send_to_remote_instance_task(instance.id, env.community.id,
                                             {'type': 'x'})

    def test_in_debug_it_runs_here_and_now(self, env, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', True)
        with patch('app.community.util.send_to_remote_instance_task') as task:
            send_to_remote_instance(1, 2, {'type': 'x'})
        task.assert_called_once_with(1, 2, {'type': 'x'})

    def test_otherwise_it_is_queued(self, env, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', False)
        with patch('app.community.util.send_to_remote_instance_task') as task:
            send_to_remote_instance(1, 2, {'type': 'x'})
        task.delay.assert_called_once_with(1, 2, {'type': 'x'})


class TestSendingOneWithoutTouchingTheDatabase:
    def test_what_it_sends(self, env):
        with patch('app.community.util.send_post_request') as send:
            send_to_remote_instance_fast_task('https://remote.test/inbox',
                                              'a-private-key',
                                              'https://here/c/probeland',
                                              {'type': 'Announce'})
        assert send.call_args.args[0] == 'https://remote.test/inbox'
        assert send.call_args.args[3] == 'https://here/c/probeland#main-key'
        assert send.call_args.kwargs == {'timeout': 10, 'new_task': False}

    def test_in_debug_it_runs_here_and_now(self, env, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', True)
        with patch('app.community.util.send_to_remote_instance_fast_task') \
                as task:
            send_to_remote_instance_fast('inbox', 'key', 'id', {'type': 'x'})
        task.assert_called_once_with('inbox', 'key', 'id', {'type': 'x'})

    def test_otherwise_it_is_queued(self, env, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', False)
        with patch('app.community.util.send_to_remote_instance_fast_task') \
                as task:
            send_to_remote_instance_fast('inbox', 'key', 'id', {'type': 'x'})
        task.delay.assert_called_once_with('inbox', 'key', 'id',
                                           {'type': 'x'})


# --------------------------------------------------------------------------
# the rest
# --------------------------------------------------------------------------

class TestFindingAccounts:
    def test_a_local_account_by_part_of_its_name(self, env):
        found = find_local_users(env.author.user_name[:3])
        assert env.author in found

    def test_a_banned_account_is_not_offered(self, env):
        env.author.banned = True
        db.session.commit()
        assert env.author not in find_local_users(env.author.user_name)

    def test_a_deleted_one_is_not_either(self, env):
        env.author.deleted = True
        db.session.commit()
        assert env.author not in find_local_users(env.author.user_name)

    def test_nor_is_a_remote_one(self, env):
        remote = make_user(env.baseline.instance_remote, 'faraway')
        remote.ap_id = 'faraway@remote.test'
        db.session.commit()
        assert remote not in find_local_users('faraway')

    def test_a_moderator_can_be_looked_for_by_name(self, env):
        assert env.author in find_potential_moderators(
            env.author.user_name[:3])

    def test_or_by_a_full_handle(self, env):
        remote = make_user(env.baseline.instance_remote, 'faraway')
        remote.ap_id = 'faraway@remote.test'
        db.session.commit()
        assert find_potential_moderators('faraway@remote.test') == [remote]

    def test_a_handle_in_the_wrong_case(self, env):
        remote = make_user(env.baseline.instance_remote, 'faraway')
        remote.ap_id = 'faraway@remote.test'
        db.session.commit()
        assert find_potential_moderators('FarAway@Remote.Test') == [remote]

    def test_a_handle_nobody_holds(self, env):
        assert find_potential_moderators('nobody@nowhere.test') == []


class TestTheHashtagCloud:
    def a_tagged_post(self, env, tag_name, count=1):
        tag = Tag(name=tag_name, display_as=tag_name)
        db.session.add(tag)
        db.session.commit()
        for index in range(count):
            post = make_post(env.community, env.author,
                             ap_id=f'https://test.piefed.local/v/{tag_name}{index}')
            post.tags.append(tag)
        db.session.commit()
        return tag

    def test_a_community_with_no_tagged_posts(self, env):
        assert hashtags_used_in_community(env.community.id, None) == []

    def test_the_tags_used_in_one(self, env):
        self.a_tagged_post(env, 'news')
        cloud = hashtags_used_in_community(env.community.id, None)
        assert [t['name'] for t in cloud] == ['news']

    def test_a_tag_a_filter_blocks_is_left_out(self, env):
        self.a_tagged_post(env, 'spoilers')
        cloud = hashtags_used_in_community(env.community.id,
                                           {'my filter': ['spoil']})
        assert cloud == []

    def test_a_filter_that_matches_nothing(self, env):
        self.a_tagged_post(env, 'news')
        cloud = hashtags_used_in_community(env.community.id,
                                           {'my filter': ['nothing']})
        assert [t['name'] for t in cloud] == ['news']

    def test_a_banned_tag_is_left_out(self, env):
        tag = self.a_tagged_post(env, 'nasty')
        tag.banned = True
        db.session.commit()
        assert hashtags_used_in_community(env.community.id, None) == []

    def test_a_deleted_post_does_not_count(self, env):
        self.a_tagged_post(env, 'news')
        for post in Post.query.all():
            post.deleted = True
        db.session.commit()
        assert hashtags_used_in_community(env.community.id, None) == []

    def test_across_several_communities(self, env):
        self.a_tagged_post(env, 'news')
        cloud = hashtags_used_in_communities([env.community.id], None)
        assert [t['name'] for t in cloud] == ['news']

    def test_no_communities_named(self, env):
        assert hashtags_used_in_communities(None, None) is None
        assert hashtags_used_in_communities([], None) is None

    def test_a_filter_across_several_that_matches_nothing(self, env):
        """Every keyword of every filter is tried against the tag before it is kept."""
        self.a_tagged_post(env, 'news')
        cloud = hashtags_used_in_communities([env.community.id],
                                             {'first': ['nothing', 'else'], 'second': ['nope']})
        assert [t['name'] for t in cloud] == ['news']

    def test_a_filter_across_several(self, env):
        self.a_tagged_post(env, 'spoilers')
        assert hashtags_used_in_communities([env.community.id],
                                            {'f': ['spoil']}) == []


class TestSizingTheCloud:
    def test_nothing_to_size(self):
        assert normalize_font_size([]) == []

    def test_tags_used_equally_often_are_the_same_size(self):
        tags = [{'name': 'a', 'pc': 3}, {'name': 'b', 'pc': 3}]
        sized = normalize_font_size(tags)
        assert sized[0]['font_size'] == sized[1]['font_size'] == 18

    def test_the_most_used_tag_is_the_largest(self):
        tags = [{'name': 'a', 'pc': 1}, {'name': 'b', 'pc': 10}]
        sized = normalize_font_size(tags)
        assert sized[0]['font_size'] == 12
        assert sized[1]['font_size'] == 24

    def test_the_range_can_be_asked_for(self):
        tags = [{'name': 'a', 'pc': 1}, {'name': 'b', 'pc': 10}]
        sized = normalize_font_size(tags, min_size=10, max_size=20)
        assert sized[0]['font_size'] == 10
        assert sized[1]['font_size'] == 20


class TestNamesACommunityMayNotHave:
    @pytest.mark.parametrize('name', ['shit', 'Fuck', 'greentext', '4chan',
                                      'my4chanmemes', 'FAUXBAIT'])
    def test_a_name_nobody_wants(self, name):
        assert is_bad_name(name) is True

    @pytest.mark.parametrize('name', ['gardening', 'news', 'linux'])
    def test_an_ordinary_one(self, name):
        assert is_bad_name(name) is False


class TestTheCommunityTheme:
    def test_it_is_allowed_by_default(self, env):
        assert get_community_theme_allowed(env.community.id,
                                           env.author.id) is True

    def test_it_can_be_turned_off(self, env):
        set_community_theme_allowed(env.community.id, env.author.id, False)
        assert get_community_theme_allowed(env.community.id,
                                           env.author.id) is False

    def test_and_back_on_again(self, env):
        set_community_theme_allowed(env.community.id, env.author.id, False)
        set_community_theme_allowed(env.community.id, env.author.id, True)
        assert get_community_theme_allowed(env.community.id,
                                           env.author.id) is True

    def test_the_list_a_form_offers_begins_with_disabled(self, env):
        assert community_theme_list()[0] == ('disabled', 'Disabled')


class TestOddsAndEnds:
    def test_an_actor_string_becomes_a_community(self, env):
        found = actor_to_community(f'!probeland@{env.community.ap_domain}')
        assert found == env.community or found is None

    def test_whether_a_community_is_in_a_list_of_tuples(self):
        assert community_in_list(2, [(1, 'a'), (2, 'b')]) is True
        assert community_in_list(3, [(1, 'a'), (2, 'b')]) is False
        assert community_in_list(1, []) is False

    def test_an_old_file_is_removed(self, env):
        image = make_file(file_path='app/static/media/x.png')
        with patch.object(File, 'delete_from_disk') as delete:
            remove_old_file(image.id)
        delete.assert_called_once()

    def test_a_file_that_is_already_gone(self, env):
        """The row can be deleted between the caller reading the id and
        this running; `remove_file.delete_from_disk()` on None was an
        AttributeError."""
        remove_old_file(999999)


# --------------------------------------------------------------------------
# search_for_community's webfinger walk -- D1397
# --------------------------------------------------------------------------


class TestTheWebfingerWalk:
    """The twin of `search_for_feed`'s walk (tests/test_feed_util.py holds the
    other half). Both read a document fetched from a REMOTE host, at a hostname the
    caller supplied, and both assumed a mapping with a `links` list.

    Covered here for symmetry on purpose: repairing two twins and testing one is
    how a twin comes to diverge again.
    """

    def _seed(self):
        instance = make_instance('test.piefed.local', software='piefed')
        make_user(instance, 'founder', local=True)
        db.session.commit()
        return instance

    @pytest.mark.parametrize('document', [
        'not a document',   # `webfinger_json['links']` is a TypeError
        42,
        None,
        ['links'],          # a list: `.get` is an AttributeError
        {},                 # no `links` at all: a KeyError
        {'links': 'https://remote.example/c/books'},   # iterated its CHARACTERS
        {'links': 42},
        {'links': None},
    ])
    def test_a_webfinger_document_this_shape_cannot_walk(self, app, db_session,
                                                        document):
        from unittest.mock import MagicMock, patch
        self._seed()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = document

        with app.test_request_context('/'):
            with patch('app.community.util.get_request',
                       side_effect=[response]) as get:
                assert search_for_community('!books@remote.example') is None

        assert get.call_count == 1

    def test_a_links_entry_that_is_not_an_object_is_skipped(self, app,
                                                            db_session):
        """The element rather than the list: `'rel' in <a string>` is a substring
        test whose subscript raises, so one junk entry stopped the walk. A real
        entry behind it must still be reached."""
        from unittest.mock import MagicMock, patch
        self._seed()
        webfinger = MagicMock()
        webfinger.status_code = 200
        webfinger.json.return_value = {'links': [
            'https://remote.example/rel/1',
            42,
            {'rel': 'self', 'type': 'application/activity+json',
             'href': 'https://remote.example/c/books'}]}
        actor = MagicMock()
        actor.status_code = 200
        actor.json.return_value = {'type': 'Group', 'preferredUsername': 'books'}
        community = make_community('books')

        with app.test_request_context('/'):
            with patch('app.community.util.get_request',
                       side_effect=[webfinger, actor]), \
                    patch('app.community.util.actor_json_to_model',
                          return_value=community), \
                    patch('app.community.util.retrieve_mods_and_backfill'):
                found = search_for_community('!books@remote.example')

        assert found.id == community.id

    def test_a_self_link_with_no_href_is_skipped(self, app, db_session):
        """`'href' not in links` -- the guard `search_for_feed` already had and
        this twin did not, so a `rel: self` entry without an href was a KeyError
        rather than a reason to keep walking."""
        from unittest.mock import MagicMock, patch
        self._seed()
        webfinger = MagicMock()
        webfinger.status_code = 200
        webfinger.json.return_value = {'links': [
            {'rel': 'self', 'type': 'application/activity+json'}]}

        with app.test_request_context('/'):
            with patch('app.community.util.get_request',
                       side_effect=[webfinger]) as get:
                assert search_for_community('!books@remote.example') is None

        assert get.call_count == 1


class TestTheCommunityAWebfingerNames:
    """search_for_community's last step: the actor document becomes a community, and a new one is backfilled."""

    def _walk(self, app, named, debug, monkeypatch):
        from unittest.mock import MagicMock
        instance = make_instance('test.piefed.local', software='piefed')
        make_user(instance, 'founder', local=True)
        db.session.commit()
        model = make_community(named) if named else None
        webfinger = MagicMock()
        webfinger.status_code = 200
        webfinger.json.return_value = {'links': [{'rel': 'self', 'type': 'application/activity+json',
                                                  'href': 'https://remote.example/c/books'}]}
        actor = MagicMock()
        actor.status_code = 200
        actor.json.return_value = {'type': 'Group', 'preferredUsername': 'books'}
        with app.test_request_context('/'):
            monkeypatch.setattr(current_app, 'debug', debug)
            with patch('app.community.util.get_request', side_effect=[webfinger, actor]), \
                    patch('app.community.util.actor_json_to_model', return_value=model), \
                    patch('app.community.util.retrieve_mods_and_backfill') as backfill:
                found = search_for_community('!books@remote.example')
        return found, backfill, model

    def test_a_group_that_makes_no_community_is_not_found(self, app, db_session, monkeypatch):
        found, backfill, _ = self._walk(app, None, False, monkeypatch)
        assert found is None
        assert backfill.delay.call_count == 0 and backfill.call_count == 0

    def test_in_debug_the_backfill_runs_here_and_now(self, app, db_session, monkeypatch):
        found, backfill, community = self._walk(app, 'books', True, monkeypatch)
        assert found.id == community.id
        backfill.assert_called_once_with(community.id, 'remote.example', 'books',
                                         {'type': 'Group', 'preferredUsername': 'books'})
        assert backfill.delay.call_count == 0
