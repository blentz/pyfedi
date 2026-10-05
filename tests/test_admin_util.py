"""The work an admin's buttons queue.

Sub-project 97 -- `app/admin/util.py`. Four jobs: deleting an account and
telling the fediverse, sending the newsletter, moving a community's images
onto this instance when the community moves here, and reading a remote
instance's public directory when an admin wants to follow its people.

One defect, found by reading rather than probing:

    if store_files_in_s3():
        extra_args = {'ContentType': content_type}

`content_type` is not assigned until inside the loop below. So
`move_community_images_to_here` was `NameError: name 'content_type' is not
defined` on every instance that stores its files in S3 -- which is the only
kind of instance the S3 branch exists for. Each upload builds its own
arguments now (D1288).

The directory and nodeinfo helpers are driven against respx rather than a
real instance; the S3 arm runs against moto.
"""
import io
import os
from unittest.mock import patch

import httpx
import pytest
from flask import current_app, g

from app import db
from app.admin.util import (directory_candidates, fetch_mastodon_directory,
                            move_community_images_to_here,
                            remote_instance_software, send_newsletter,
                            switch_to_silenced, switch_to_unsilenced,
                            topics_for_form,
                            unsubscribe_from_community,
                            unsubscribe_from_everything_then_delete,
                            unsubscribe_from_everything_then_delete_task)
from app.constants import POST_TYPE_IMAGE
from app.models import (Community, CommunityMember, File, Instance, Post,
                        Site, Topic, User)
from tests.factories import (make_community, make_community_member, make_file,
                             make_post, make_user)


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.name = 'Probeland'
    community = make_community('probeland')
    member = api_baseline.user2
    db.session.commit()
    make_community_member(member, community)
    for key in ('S3_ACCESS_KEY', 'S3_ACCESS_SECRET', 'S3_ENDPOINT'):
        monkeypatch.setitem(current_app.config, key, '')
    return SimpleNamespace(app=app, community=community, member=member,
                           baseline=api_baseline)


# --------------------------------------------------------------------------
# deleting an account
# --------------------------------------------------------------------------

class TestDeletingAnAccount:
    def test_it_ends_up_deleted_and_banned(self, env):
        user_id = env.member.id
        with patch('app.admin.util.send_post_request'), \
                patch('app.admin.util.unsubscribe_from_community'):
            unsubscribe_from_everything_then_delete_task(user_id)
        db.session.expire_all()
        user = db.session.get(User, user_id)
        assert user.deleted is True
        assert user.banned is True

    def test_every_community_is_unsubscribed_from(self, env):
        with patch('app.admin.util.send_post_request'), \
                patch('app.admin.util.unsubscribe_from_community') as unsub:
            unsubscribe_from_everything_then_delete_task(env.member.id)
        assert unsub.call_count >= 1

    def test_a_local_account_is_announced_as_deleted(self, env):
        remote = env.baseline.instance_remote
        remote.inbox = 'https://remote.test/inbox'
        remote.dormant = False
        remote.gone_forever = False
        db.session.commit()
        with patch('app.admin.util.send_post_request') as send, \
                patch('app.admin.util.unsubscribe_from_community'):
            unsubscribe_from_everything_then_delete_task(env.member.id)
        assert send.call_count == 1
        assert send.call_args.args[1]['type'] == 'Delete'

    def test_a_remote_account_is_not(self, env):
        remote = env.baseline.instance_remote
        remote.inbox = 'https://remote.test/inbox'
        remote.dormant = False
        remote.gone_forever = False
        remote_user = make_user(remote, 'faraway')
        remote_user.ap_id = 'faraway@remote.test'
        remote_user.ap_profile_id = 'https://remote.test/u/faraway'
        db.session.commit()
        with patch('app.admin.util.send_post_request') as send, \
                patch('app.admin.util.unsubscribe_from_community'):
            unsubscribe_from_everything_then_delete_task(remote_user.id)
        assert send.call_count == 0

    def test_an_instance_with_no_inbox_is_not_written_to(self, env):
        remote = env.baseline.instance_remote
        remote.inbox = None
        db.session.commit()
        with patch('app.admin.util.send_post_request') as send, \
                patch('app.admin.util.unsubscribe_from_community'):
            unsubscribe_from_everything_then_delete_task(env.member.id)
        assert send.call_count == 0

    def test_an_account_that_is_already_gone(self, env):
        """Two admins pressing Delete, or a retry: the task is queued with an
        id and the row may not be there when it runs."""
        unsubscribe_from_everything_then_delete_task(999999)

    def test_a_failure_is_rolled_back_and_raised(self, env):
        with patch('app.admin.util.unsubscribe_from_community',
                   side_effect=Exception('the remote is down')):
            with pytest.raises(Exception, match='the remote is down'):
                unsubscribe_from_everything_then_delete_task(env.member.id)

    def test_in_debug_it_runs_here_and_now(self, env, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', True)
        with patch('app.admin.util.unsubscribe_from_everything_then_delete_task') \
                as task:
            unsubscribe_from_everything_then_delete(7)
        task.assert_called_once_with(7)

    def test_otherwise_it_is_queued(self, env, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', False)
        with patch('app.admin.util.unsubscribe_from_everything_then_delete_task') \
                as task:
            unsubscribe_from_everything_then_delete(7)
        task.delay.assert_called_once_with(7)


class TestLeavingACommunityOnTheWayOut:
    @pytest.fixture
    def remote_community(self, env):
        community = make_community('faraway', host='remote.test')
        community.ap_id = 'faraway@remote.test'
        community.ap_inbox_url = 'https://remote.test/c/faraway/inbox'
        db.session.commit()
        return community

    def test_an_undo_follow_is_sent(self, env, remote_community):
        with patch('app.admin.util.send_post_request') as send:
            unsubscribe_from_community(remote_community, env.member)
        assert send.call_args.args[1]['type'] == 'Undo'
        assert send.call_args.args[1]['object']['type'] == 'Follow'

    def test_an_instance_that_is_gone_is_not_written_to(self, env,
                                                        remote_community):
        remote_community.instance.gone_forever = True
        db.session.commit()
        with patch('app.admin.util.send_post_request') as send:
            unsubscribe_from_community(remote_community, env.member)
        assert send.call_count == 0


# --------------------------------------------------------------------------
# the newsletter
# --------------------------------------------------------------------------

class _Field:
    def __init__(self, data):
        self.data = data


class _NewsletterForm:
    def __init__(self, subject='A newsletter', body_text='hello',
                 body_html='<p>hello</p>', test=False):
        self.subject = _Field(subject)
        self.body_text = _Field(body_text)
        self.body_html = _Field(body_html)
        self.test = _Field(test)


class TestTheNewsletter:
    @pytest.fixture(autouse=True)
    def nobody_subscribed(self, env):
        """The column defaults to True, so opting everybody out first is
        what makes "who gets it" observable at all."""
        for user in User.query.all():
            user.newsletter = False
        db.session.commit()

    @pytest.fixture
    def subscriber(self, env):
        subscriber = env.member
        subscriber.newsletter = True
        subscriber.email = 'reader@probeland.test'
        db.session.commit()
        return subscriber

    def test_it_goes_to_everybody_who_asked_for_it(self, env, subscriber):
        with patch('app.admin.util.send_email') as send:
            send_newsletter(_NewsletterForm())
        assert send.call_count == 1
        assert send.call_args.kwargs['recipients'] == \
            ['reader@probeland.test']

    def test_it_does_not_go_to_anybody_who_did_not(self, env, subscriber):
        subscriber.newsletter = False
        db.session.commit()
        with patch('app.admin.util.send_email') as send, \
                patch('app.admin.util.flash'):
            send_newsletter(_NewsletterForm())
        assert send.call_count == 0

    def test_nor_to_a_banned_account(self, env, subscriber):
        subscriber.banned = True
        db.session.commit()
        with patch('app.admin.util.send_email') as send, \
                patch('app.admin.util.flash'):
            send_newsletter(_NewsletterForm())
        assert send.call_count == 0

    def test_nor_to_a_remote_one(self, env, subscriber):
        subscriber.ap_id = 'reader@remote.test'
        db.session.commit()
        with patch('app.admin.util.send_email') as send, \
                patch('app.admin.util.flash'):
            send_newsletter(_NewsletterForm())
        assert send.call_count == 0

    def test_an_admin_is_told_when_there_is_nobody_to_send_to(self, env):
        with patch('app.admin.util.send_email'), \
                patch('app.admin.util.flash') as flash:
            send_newsletter(_NewsletterForm())
        assert flash.call_count == 1

    def test_a_test_send_goes_to_the_admin_and_stops_there(self, env,
                                                           subscriber):
        second = make_user(env.baseline.instance_local, 'another', local=True)
        second.newsletter = True
        second.email = 'another@probeland.test'
        db.session.commit()
        with env.app.test_request_context('/'), \
                patch('app.admin.util.send_email') as send:
            from flask_login import login_user
            login_user(env.baseline.user1)
            send_newsletter(_NewsletterForm(test=True))
        assert send.call_count == 1
        assert send.call_args.kwargs['recipients'] == \
            [env.baseline.user1.email]

    def test_what_the_body_carries(self, env, subscriber):
        with patch('app.admin.util.send_email') as send:
            send_newsletter(_NewsletterForm(body_text='the news',
                                            body_html='<p>the news</p>'))
        assert 'the news' in send.call_args.kwargs['text_body']
        assert 'the news' in send.call_args.kwargs['html_body']


# --------------------------------------------------------------------------
# the topic dropdown
# --------------------------------------------------------------------------

class TestTheTopicDropdown:
    @pytest.fixture
    def topics(self, env):
        parent = Topic(name='Things', machine_name='things',
                       num_communities=0)
        db.session.add(parent)
        db.session.commit()
        child = Topic(name='Small things', machine_name='small-things',
                      num_communities=0, parent_id=parent.id)
        db.session.add(child)
        db.session.commit()
        return parent, child

    def test_it_begins_with_none(self, env):
        assert topics_for_form(-1)[0][0] == -1

    def test_every_topic_is_offered(self, env, topics):
        parent, child = topics
        offered = dict(topics_for_form(-1))
        assert parent.id in offered
        assert child.id in offered

    def test_a_child_is_shown_indented(self, env, topics):
        parent, child = topics
        assert dict(topics_for_form(-1))[child.id].startswith('--')

    def test_the_topic_being_edited_is_not_offered_as_its_own_parent(
            self, env, topics):
        parent, child = topics
        assert parent.id not in dict(topics_for_form(parent.id))

    def test_nor_is_a_child_being_edited(self, env, topics):
        parent, child = topics
        assert child.id not in dict(topics_for_form(child.id))


# --------------------------------------------------------------------------
# silencing an instance
# --------------------------------------------------------------------------

class TestSilencingAnInstance:
    def test_its_communities_drop_out_of_the_listings(self, env):
        topic = Topic(name='Things', machine_name='things', num_communities=1)
        db.session.add(topic)
        db.session.commit()
        env.community.instance_id = env.baseline.instance_remote.id
        env.community.show_all = True
        env.community.show_popular = True
        env.community.topic_id = topic.id
        db.session.commit()
        switch_to_silenced(env.baseline.instance_remote.id)
        db.session.commit()
        db.session.expire_all()
        community = db.session.get(Community, env.community.id)
        assert community.show_all is False
        assert community.show_popular is False
        assert community.topic_id is None

    def test_unsilencing_puts_them_back(self, env):
        env.community.instance_id = env.baseline.instance_remote.id
        db.session.commit()
        switch_to_unsilenced(env.baseline.instance_remote.id, trusted=False)
        db.session.commit()
        db.session.expire_all()
        community = db.session.get(Community, env.community.id)
        assert community.show_all is True
        assert community.show_popular is False

    def test_a_trusted_instance_is_popular_again_too(self, env):
        env.community.instance_id = env.baseline.instance_remote.id
        db.session.commit()
        switch_to_unsilenced(env.baseline.instance_remote.id, trusted=True)
        db.session.commit()
        db.session.expire_all()
        assert db.session.get(Community,
                              env.community.id).show_popular is True


# --------------------------------------------------------------------------
# a remote instance's public directory
# --------------------------------------------------------------------------

def an_account(acct='someone', statuses=100, followers=50, bot=False):
    return {'acct': acct, 'statuses_count': statuses,
            'followers_count': followers, 'bot': bot}


class TestPickingPeopleOutOfADirectory:
    def test_a_bare_acct_is_qualified_with_the_domain(self, env):
        """Mastodon returns `acct` unqualified for its own accounts, and an
        unqualified handle is one `search_for_user` cannot resolve."""
        handles, stats = directory_candidates(
            [an_account()], 'remote.test', 0, 0, False, 10)
        assert handles == ['someone@remote.test']
        assert stats['candidates'] == 1

    def test_one_that_is_already_qualified_is_left_alone(self, env):
        handles, _ = directory_candidates(
            [an_account('someone@elsewhere.test')], 'remote.test', 0, 0,
            False, 10)
        assert handles == ['someone@elsewhere.test']

    def test_somebody_below_the_post_count(self, env):
        handles, stats = directory_candidates(
            [an_account(statuses=3)], 'remote.test', 10, 0, False, 10)
        assert handles == []
        assert stats['below_minimum_statuses'] == 1

    def test_somebody_below_the_follower_count(self, env):
        handles, stats = directory_candidates(
            [an_account(followers=3)], 'remote.test', 0, 10, False, 10)
        assert handles == []
        assert stats['below_minimum_followers'] == 1

    def test_a_bot_when_bots_are_excluded(self, env):
        handles, stats = directory_candidates(
            [an_account(bot=True)], 'remote.test', 0, 0, True, 10)
        assert handles == []
        assert stats['bots'] == 1

    def test_a_bot_when_they_are_not(self, env):
        handles, _ = directory_candidates(
            [an_account(bot=True)], 'remote.test', 0, 0, False, 10)
        assert handles == ['someone@remote.test']

    @pytest.mark.parametrize('entry', [{}, {'acct': ''}, {'acct': 42},
                                       'not an object', None])
    def test_an_entry_that_is_not_one(self, env, entry):
        handles, stats = directory_candidates(
            [entry], 'remote.test', 0, 0, False, 10)
        assert handles == []
        assert stats['malformed'] == 1

    def test_an_account_with_no_counts_at_all(self, env):
        handles, _ = directory_candidates(
            [{'acct': 'someone'}], 'remote.test', 0, 0, False, 10)
        assert handles == ['someone@remote.test']

    def test_no_more_than_the_limit_is_returned(self, env):
        entries = [an_account(f'user{index}') for index in range(10)]
        handles, stats = directory_candidates(entries, 'remote.test', 0, 0,
                                              False, 3)
        assert len(handles) == 3
        assert stats['seen'] == 10
        assert stats['candidates'] == 10

    def test_an_empty_directory(self, env):
        handles, stats = directory_candidates([], 'remote.test', 0, 0, False,
                                              10)
        assert handles == []
        assert stats['seen'] == 0


class TestReadingTheDirectory:
    URL = 'https://remote.test/api/v1/directory'

    def test_one_short_page(self, env, http_mock):
        http_mock.get(self.URL).mock(return_value=httpx.Response(
            200, json=[an_account('a'), an_account('b')]))
        assert len(fetch_mastodon_directory('https://remote.test')) == 2

    def test_a_directory_that_is_empty(self, env, http_mock):
        http_mock.get(self.URL).mock(return_value=httpx.Response(200,
                                                                 json=[]))
        assert fetch_mastodon_directory('https://remote.test') == []

    def test_a_directory_that_answers_with_something_else(self, env,
                                                          http_mock):
        http_mock.get(self.URL).mock(return_value=httpx.Response(
            200, json={'error': 'directory is disabled'}))
        assert fetch_mastodon_directory('https://remote.test') == []

    def test_several_pages_are_followed(self, env, http_mock):
        full = [an_account(f'user{index}') for index in range(80)]
        http_mock.get(self.URL).mock(side_effect=[
            httpx.Response(200, json=full),
            httpx.Response(200, json=[an_account('last')])])
        assert len(fetch_mastodon_directory('https://remote.test')) == 81

    def test_no_more_than_the_page_limit(self, env, http_mock):
        full = [an_account(f'user{index}') for index in range(80)]
        http_mock.get(self.URL).mock(
            return_value=httpx.Response(200, json=full))
        accounts = fetch_mastodon_directory('https://remote.test',
                                            max_pages=2)
        assert len(accounts) == 160


class TestAskingWhatSoftwareAnInstanceRuns:
    WELL_KNOWN = 'https://remote.test/.well-known/nodeinfo'
    INFO = 'https://remote.test/nodeinfo/2.0'
    NODEINFO2 = 'https://remote.test/.well-known/x-nodeinfo2'   # read when no 2.x link is usable (interop D24)

    def no_nodeinfo2(self, http_mock):
        http_mock.get(self.NODEINFO2).mock(return_value=httpx.Response(404, json=''))

    def nodeinfo(self, rel='http://nodeinfo.diaspora.software/ns/schema/2.0'):
        return {'links': [{'rel': rel, 'href': self.INFO}]}

    def test_what_it_says_it_runs(self, env, http_mock):
        http_mock.get(self.WELL_KNOWN).mock(
            return_value=httpx.Response(200, json=self.nodeinfo()))
        http_mock.get(self.INFO).mock(return_value=httpx.Response(
            200, json={'software': {'name': 'Mastodon', 'version': '4.2'}}))
        assert remote_instance_software('https://remote.test') == 'mastodon'

    def test_the_two_point_one_schema_is_accepted_too(self, env, http_mock):
        http_mock.get(self.WELL_KNOWN).mock(return_value=httpx.Response(
            200, json=self.nodeinfo(
                'http://nodeinfo.diaspora.software/ns/schema/2.1')))
        http_mock.get(self.INFO).mock(return_value=httpx.Response(
            200, json={'software': {'name': 'lemmy'}}))
        assert remote_instance_software('https://remote.test') == 'lemmy'

    def test_an_instance_that_advertises_no_schema_we_know(self, env,
                                                           http_mock):
        self.no_nodeinfo2(http_mock)
        http_mock.get(self.WELL_KNOWN).mock(return_value=httpx.Response(
            200, json=self.nodeinfo(
                'http://nodeinfo.diaspora.software/ns/schema/1.0')))
        with pytest.raises(Exception, match='no nodeinfo'):
            remote_instance_software('https://remote.test')

    def test_one_that_advertises_nothing_at_all(self, env, http_mock):
        self.no_nodeinfo2(http_mock)
        http_mock.get(self.WELL_KNOWN).mock(
            return_value=httpx.Response(200, json={}))
        with pytest.raises(Exception, match='no nodeinfo'):
            remote_instance_software('https://remote.test')

    def test_one_whose_links_are_not_objects(self, env, http_mock):
        self.no_nodeinfo2(http_mock)
        http_mock.get(self.WELL_KNOWN).mock(
            return_value=httpx.Response(200, json={'links': ['nonsense']}))
        with pytest.raises(Exception, match='no nodeinfo'):
            remote_instance_software('https://remote.test')

    def test_one_whose_nodeinfo_names_no_software(self, env, http_mock):
        http_mock.get(self.WELL_KNOWN).mock(
            return_value=httpx.Response(200, json=self.nodeinfo()))
        http_mock.get(self.INFO).mock(
            return_value=httpx.Response(200, json={'version': '1.0'}))
        with pytest.raises(Exception, match='does not name its software'):
            remote_instance_software('https://remote.test')

    def test_one_whose_software_block_is_empty(self, env, http_mock):
        http_mock.get(self.WELL_KNOWN).mock(
            return_value=httpx.Response(200, json=self.nodeinfo()))
        http_mock.get(self.INFO).mock(
            return_value=httpx.Response(200, json={'software': {}}))
        with pytest.raises(Exception, match='does not name its software'):
            remote_instance_software('https://remote.test')


# --------------------------------------------------------------------------
# moving a community's images onto this instance
# --------------------------------------------------------------------------

def a_png():
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (10, 10), (1, 2, 3)).save(buffer, format='PNG')
    return buffer.getvalue()


SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
       b'<rect width="10" height="10"/></svg>')
HOSTILE_SVG = (b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "'
               + b'x' * 64 + b'"><!ENTITY b "&a;&a;&a;">]>'
               b'<svg xmlns="http://www.w3.org/2000/svg">&b;&b;&b;&b;&b;&b;'
               b'&b;&b;&b;&b;</svg>')


@pytest.fixture
def image_post(env):
    """One image post in the community, with a File pointing somewhere."""
    post = make_post(env.community, env.member,
                     ap_id='https://remote.test/p/1')
    post.type = POST_TYPE_IMAGE
    post.instance_id = env.baseline.instance_remote.id
    image = make_file(source_url='https://remote.test/media/photo.png')
    post.image_id = image.id
    db.session.commit()
    return post


def written_files():
    import glob
    return set(glob.glob('app/static/media/posts/*/*/*'))


class TestMovingAPostOntoThisInstance:
    def test_the_posts_become_this_instance_s_own(self, env, image_post):
        with patch('app.admin.util.get_request',
                   return_value=httpx.Response(404)):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        post = db.session.get(Post, image_post.id)
        assert post.instance_id == 1
        assert post.ap_id.endswith(f'/post/{post.id}')
        assert post.ap_create_id is None
        assert post.ap_announce_id is None

    def test_a_reply_is_renamed_too(self, env, image_post):
        from tests.factories import make_post_reply
        reply = make_post_reply(image_post, env.member, body='a reply')
        db.session.commit()
        with patch('app.admin.util.get_request',
                   return_value=httpx.Response(404)):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        from app.models import PostReply
        stored = db.session.get(PostReply, reply.id)
        assert stored.ap_id.endswith(f'/comment/{stored.id}')
        assert stored.instance_id == 1

    def test_an_image_already_on_this_disk_is_only_renamed(self, env,
                                                           image_post,
                                                           tmp_path):
        directory = 'app/static/media/posts/ab/cd'
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, 'already-here.png')
        with open(path, 'wb') as handle:
            handle.write(a_png())
        image_post.image.source_url = path
        db.session.commit()
        try:
            move_community_images_to_here(env.community.id)
            db.session.expire_all()
            post = db.session.get(Post, image_post.id)
            assert post.image.source_url.startswith(
                current_app.config['SERVER_URL'])
            assert 'static/media/posts/ab/cd/already-here.png' in \
                post.image.source_url
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_a_remote_image_is_downloaded_and_re_hosted(self, env,
                                                        image_post):
        before = written_files()
        with patch('app.admin.util.get_request', return_value=httpx.Response(
                200, content=a_png(),
                headers={'content-type': 'image/png'})):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        post = db.session.get(Post, image_post.id)
        try:
            assert post.image.source_url.startswith(
                current_app.config['SERVER_URL'])
            assert post.image.source_url.endswith('.png')
        finally:
            for path in written_files() - before:
                os.unlink(path)

    def test_a_jpeg_is_given_the_short_extension(self, env, image_post):
        before = written_files()
        with patch('app.admin.util.get_request', return_value=httpx.Response(
                200, content=a_png(),
                headers={'content-type': 'image/jpeg; charset=binary'})):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        post = db.session.get(Post, image_post.id)
        try:
            assert post.image.source_url.endswith('.jpg')
        finally:
            for path in written_files() - before:
                os.unlink(path)

    def test_a_remote_svg_is_sanitised_before_it_is_re_hosted(self, env,
                                                              image_post):
        before = written_files()
        with patch('app.admin.util.get_request', return_value=httpx.Response(
                200, content=SVG,
                headers={'content-type': 'image/svg+xml'})):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        post = db.session.get(Post, image_post.id)
        try:
            assert post.image.source_url.startswith(
                current_app.config['SERVER_URL'])
        finally:
            for path in written_files() - before:
                os.unlink(path)

    def test_one_that_cannot_be_sanitised_is_left_where_it_is(self, env,
                                                              image_post):
        with patch('app.admin.util.get_request', return_value=httpx.Response(
                200, content=HOSTILE_SVG,
                headers={'content-type': 'image/svg+xml'})):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        post = db.session.get(Post, image_post.id)
        assert post.image.source_url == 'https://remote.test/media/photo.png'

    def test_something_that_is_not_an_image_is_left_alone(self, env,
                                                          image_post):
        with patch('app.admin.util.get_request', return_value=httpx.Response(
                200, content=b'<html>', headers={'content-type': 'text/html'})):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        post = db.session.get(Post, image_post.id)
        assert post.image.source_url == 'https://remote.test/media/photo.png'

    def test_a_download_that_fails_does_not_stop_the_task(self, env,
                                                          image_post):
        with patch('app.admin.util.get_request',
                   side_effect=httpx.ConnectError('no route')):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        assert db.session.get(Post, image_post.id).instance_id == 1

    def test_an_image_already_pointing_at_this_server(self, env, image_post):
        image_post.image.source_url = \
            f"{current_app.config['SERVER_URL']}/static/media/posts/x.png"
        db.session.commit()
        with patch('app.admin.util.get_request') as get:
            move_community_images_to_here(env.community.id)
        assert get.call_count == 0

    def test_a_post_with_no_image_row(self, env):
        post = make_post(env.community, env.member,
                         ap_id='https://remote.test/p/2')
        post.type = POST_TYPE_IMAGE
        db.session.commit()
        with patch('app.admin.util.get_request') as get:
            move_community_images_to_here(env.community.id)
        assert get.call_count == 0

    def test_a_community_with_nothing_in_it(self, env):
        move_community_images_to_here(env.community.id)


class TestMovingThemIntoS3:
    @pytest.fixture
    def s3(self, env, s3_bucket, monkeypatch):
        for key, value in (('S3_ACCESS_KEY', 'key'),
                           ('S3_ACCESS_SECRET', 'secret'),
                           ('S3_ENDPOINT', 'https://s3.us-east-1.amazonaws.com'),
                           ('S3_REGION', 'us-east-1'),
                           ('S3_BUCKET', s3_bucket),
                           ('S3_PUBLIC_URL', 'cdn.probeland.test'),
                           ('S3_STORAGE_CLASS', ''),
                           ('S3_PUBLIC_ACL', '')):
            monkeypatch.setitem(current_app.config, key, value)
        return s3_bucket

    def test_an_image_on_this_disk_is_pushed_to_the_bucket(self, env, s3,
                                                           image_post):
        """D1288. `extra_args = {'ContentType': content_type}` stood above
        every assignment to `content_type`, so this whole task was a
        NameError on exactly the instances it is for."""
        directory = 'app/static/media/posts/ef/gh'
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, 'to-move.png')
        with open(path, 'wb') as handle:
            handle.write(a_png())
        image_post.image.source_url = path
        db.session.commit()
        try:
            move_community_images_to_here(env.community.id)
            db.session.expire_all()
            post = db.session.get(Post, image_post.id)
            assert post.image.source_url.startswith(
                'https://cdn.probeland.test/')
            assert not os.path.exists(path)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_a_storage_class_and_an_acl_are_passed_on(self, env, s3,
                                                      image_post,
                                                      monkeypatch):
        monkeypatch.setitem(current_app.config, 'S3_STORAGE_CLASS',
                            'STANDARD')
        monkeypatch.setitem(current_app.config, 'S3_PUBLIC_ACL', True)
        directory = 'app/static/media/posts/ij/kl'
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, 'to-move.png')
        with open(path, 'wb') as handle:
            handle.write(a_png())
        image_post.image.source_url = path
        db.session.commit()
        try:
            move_community_images_to_here(env.community.id)
            db.session.expire_all()
            assert db.session.get(Post, image_post.id).image.source_url.\
                startswith('https://cdn.probeland.test/')
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_a_remote_image_is_downloaded_then_pushed(self, env, s3,
                                                      image_post):
        with patch('app.admin.util.get_request', return_value=httpx.Response(
                200, content=a_png(),
                headers={'content-type': 'image/png'})):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        post = db.session.get(Post, image_post.id)
        assert post.image.source_url.startswith('https://cdn.probeland.test/')

    def test_a_remote_svg_is_sanitised_first(self, env, s3, image_post):
        with patch('app.admin.util.get_request', return_value=httpx.Response(
                200, content=SVG, headers={'content-type': 'image/svg+xml'})):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        post = db.session.get(Post, image_post.id)
        assert post.image.source_url.startswith('https://cdn.probeland.test/')

    def test_one_that_cannot_be_sanitised_is_left_where_it_is(self, env, s3,
                                                              image_post):
        with patch('app.admin.util.get_request', return_value=httpx.Response(
                200, content=HOSTILE_SVG,
                headers={'content-type': 'image/svg+xml'})):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        post = db.session.get(Post, image_post.id)
        assert post.image.source_url == 'https://remote.test/media/photo.png'

    def test_a_download_that_fails_does_not_stop_the_task(self, env, s3,
                                                          image_post):
        with patch('app.admin.util.get_request',
                   side_effect=httpx.ConnectError('no route')):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        assert db.session.get(Post, image_post.id).instance_id == 1

    def test_something_that_is_not_an_image_is_left_alone(self, env, s3,
                                                          image_post):
        with patch('app.admin.util.get_request', return_value=httpx.Response(
                200, content=b'<html>',
                headers={'content-type': 'text/html'})):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        post = db.session.get(Post, image_post.id)
        assert post.image.source_url == 'https://remote.test/media/photo.png'

    def test_a_404_that_still_claims_to_be_an_image(self, env, s3,
                                                    image_post):
        """The status is what decides, not the content type the error page
        happens to carry."""
        with patch('app.admin.util.get_request', return_value=httpx.Response(
                404, content=b'not found',
                headers={'content-type': 'image/png'})):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        post = db.session.get(Post, image_post.id)
        assert post.image.source_url == 'https://remote.test/media/photo.png'

    def test_an_image_already_in_the_bucket(self, env, s3, image_post):
        image_post.image.source_url = \
            'https://cdn.probeland.test/posts/ab/cd/already.png'
        db.session.commit()
        with patch('app.admin.util.get_request') as get:
            move_community_images_to_here(env.community.id)
        assert get.call_count == 0


class TestMoreShapesOfTheMove:
    def test_a_grandchild_topic_is_indented_twice(self, env):
        parent = Topic(name='Things', machine_name='things',
                       num_communities=0)
        db.session.add(parent)
        db.session.commit()
        child = Topic(name='Small', machine_name='small', num_communities=0,
                      parent_id=parent.id)
        db.session.add(child)
        db.session.commit()
        grandchild = Topic(name='Tiny', machine_name='tiny',
                           num_communities=0, parent_id=child.id)
        db.session.add(grandchild)
        db.session.commit()
        assert dict(topics_for_form(-1))[grandchild.id].startswith('----')

    def test_a_body_that_fails_halfway_through(self, env, image_post):
        """`get_request` returns, so the handler has a response to close --
        the arm that leaked a pooled connection before it was fixed."""
        class _Truncated:
            status_code = 200
            headers = {'content-type': 'image/png'}

            def __init__(self):
                self.closed = False

            @property
            def content(self):
                raise httpx.ReadError('the connection went away')

            def close(self):
                self.closed = True

        truncated = _Truncated()
        with patch('app.admin.util.get_request', return_value=truncated):
            move_community_images_to_here(env.community.id)
        assert truncated.closed is True
        db.session.expire_all()
        post = db.session.get(Post, image_post.id)
        assert post.image.source_url == 'https://remote.test/media/photo.png'


class TestMoreShapesIntoS3:
    @pytest.fixture
    def s3(self, env, s3_bucket, monkeypatch):
        for key, value in (('S3_ACCESS_KEY', 'key'),
                           ('S3_ACCESS_SECRET', 'secret'),
                           ('S3_ENDPOINT', 'https://s3.us-east-1.amazonaws.com'),
                           ('S3_REGION', 'us-east-1'),
                           ('S3_BUCKET', s3_bucket),
                           ('S3_PUBLIC_URL', 'cdn.probeland.test'),
                           ('S3_STORAGE_CLASS', ''),
                           ('S3_PUBLIC_ACL', '')):
            monkeypatch.setitem(current_app.config, key, value)
        return s3_bucket

    def test_a_downloaded_jpeg_keeps_the_short_extension(self, env, s3,
                                                         image_post):
        with patch('app.admin.util.get_request', return_value=httpx.Response(
                200, content=a_png(),
                headers={'content-type': 'image/jpeg; charset=binary'})):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        post = db.session.get(Post, image_post.id)
        assert post.image.source_url.endswith('.jpg')

    def test_a_downloaded_image_with_a_storage_class_and_an_acl(self, env, s3,
                                                                image_post,
                                                                monkeypatch):
        monkeypatch.setitem(current_app.config, 'S3_STORAGE_CLASS',
                            'STANDARD')
        monkeypatch.setitem(current_app.config, 'S3_PUBLIC_ACL', True)
        with patch('app.admin.util.get_request', return_value=httpx.Response(
                200, content=a_png(),
                headers={'content-type': 'image/png'})):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        post = db.session.get(Post, image_post.id)
        assert post.image.source_url.startswith('https://cdn.probeland.test/')

    def test_a_body_that_fails_halfway_through(self, env, s3, image_post):
        class _Truncated:
            status_code = 200
            headers = {'content-type': 'image/png'}

            def __init__(self):
                self.closed = False

            @property
            def content(self):
                raise httpx.ReadError('the connection went away')

            def close(self):
                self.closed = True

        truncated = _Truncated()
        with patch('app.admin.util.get_request', return_value=truncated):
            move_community_images_to_here(env.community.id)
        assert truncated.closed is True


class TestSeveralPostsAtOnce:
    def two_image_posts(self, env):
        posts = []
        for index in (1, 2):
            post = make_post(env.community, env.member,
                             ap_id=f'https://remote.test/p/{index}')
            post.type = POST_TYPE_IMAGE
            image = make_file(source_url=f'https://remote.test/m/{index}.png')
            post.image_id = image.id
            posts.append(post)
        db.session.commit()
        return posts

    def test_the_loop_carries_on_to_the_second(self, env):
        before = written_files()
        self.two_image_posts(env)
        with patch('app.admin.util.get_request', return_value=httpx.Response(
                200, content=a_png(),
                headers={'content-type': 'image/png'})):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        try:
            moved = [post.image.source_url for post in
                     Post.query.filter_by(community_id=env.community.id).all()
                     if post.image_id]
            assert all(url.startswith(current_app.config['SERVER_URL'])
                       for url in moved)
            assert len(moved) == 2
        finally:
            for path in written_files() - before:
                os.unlink(path)

    def test_and_into_the_bucket_too(self, env, s3_bucket, monkeypatch):
        for key, value in (('S3_ACCESS_KEY', 'key'),
                           ('S3_ACCESS_SECRET', 'secret'),
                           ('S3_ENDPOINT', 'https://s3.us-east-1.amazonaws.com'),
                           ('S3_REGION', 'us-east-1'),
                           ('S3_BUCKET', s3_bucket),
                           ('S3_PUBLIC_URL', 'cdn.probeland.test'),
                           ('S3_STORAGE_CLASS', ''), ('S3_PUBLIC_ACL', '')):
            monkeypatch.setitem(current_app.config, key, value)
        self.two_image_posts(env)
        with patch('app.admin.util.get_request', return_value=httpx.Response(
                200, content=a_png(),
                headers={'content-type': 'image/png'})):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        moved = [post.image.source_url for post in
                 Post.query.filter_by(community_id=env.community.id).all()
                 if post.image_id]
        assert len(moved) == 2
        assert all(url.startswith('https://cdn.probeland.test/')
                   for url in moved)

    def test_a_second_topic_with_no_children_of_its_own(self, env):
        first = Topic(name='Things', machine_name='things', num_communities=0)
        second = Topic(name='Others', machine_name='others',
                       num_communities=0)
        db.session.add_all([first, second])
        db.session.commit()
        db.session.add(Topic(name='Small', machine_name='small',
                             num_communities=0, parent_id=first.id))
        db.session.commit()
        offered = dict(topics_for_form(-1))
        assert second.id in offered

    def test_a_failure_is_rolled_back_and_raised(self, env, image_post,
                                                 s3_bucket, monkeypatch):
        for key, value in (('S3_ACCESS_KEY', 'key'),
                           ('S3_ACCESS_SECRET', 'secret'),
                           ('S3_ENDPOINT', 'https://s3.us-east-1.amazonaws.com'),
                           ('S3_REGION', 'us-east-1'),
                           ('S3_BUCKET', s3_bucket),
                           ('S3_PUBLIC_URL', 'cdn.probeland.test'),
                           ('S3_STORAGE_CLASS', ''), ('S3_PUBLIC_ACL', '')):
            monkeypatch.setitem(current_app.config, key, value)
        directory = 'app/static/media/posts/mn/op'
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, 'to-move.png')
        with open(path, 'wb') as handle:
            handle.write(a_png())
        image_post.image.source_url = path
        db.session.commit()
        try:
            with patch('app.admin.util.guess_mime_type',
                       side_effect=Exception('cannot read the file')):
                with pytest.raises(Exception, match='cannot read the file'):
                    move_community_images_to_here(env.community.id)
        finally:
            if os.path.exists(path):
                os.unlink(path)


class TestPostsThatAreSkippedMidLoop:
    """Each of these is followed by a second post, so the loop has to carry
    on past the one it skipped."""

    @pytest.fixture
    def s3(self, env, s3_bucket, monkeypatch):
        for key, value in (('S3_ACCESS_KEY', 'key'),
                           ('S3_ACCESS_SECRET', 'secret'),
                           ('S3_ENDPOINT', 'https://s3.us-east-1.amazonaws.com'),
                           ('S3_REGION', 'us-east-1'),
                           ('S3_BUCKET', s3_bucket),
                           ('S3_PUBLIC_URL', 'cdn.probeland.test'),
                           ('S3_STORAGE_CLASS', ''), ('S3_PUBLIC_ACL', '')):
            monkeypatch.setitem(current_app.config, key, value)
        return s3_bucket

    def a_post_with(self, env, source_url, index):
        post = make_post(env.community, env.member,
                         ap_id=f'https://remote.test/p/{index}')
        post.type = POST_TYPE_IMAGE
        image = make_file(source_url=source_url)
        post.image_id = image.id
        db.session.commit()
        return post

    def test_a_local_path_whose_file_is_not_there(self, env, s3):
        skipped = self.a_post_with(env, 'app/static/media/posts/zz/zz/gone.png', 1)
        self.a_post_with(env, 'https://remote.test/m/2.png', 2)
        with patch('app.admin.util.get_request', return_value=httpx.Response(
                200, content=a_png(),
                headers={'content-type': 'image/png'})):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        assert db.session.get(Post, skipped.id).image.source_url == \
            'app/static/media/posts/zz/zz/gone.png'

    def test_a_download_that_answers_404(self, env, s3):
        skipped = self.a_post_with(env, 'https://remote.test/m/1.png', 1)
        with patch('app.admin.util.get_request',
                   return_value=httpx.Response(404)):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        assert db.session.get(Post, skipped.id).image.source_url == \
            'https://remote.test/m/1.png'

    def test_a_download_with_no_content_type_at_all(self, env, s3):
        skipped = self.a_post_with(env, 'https://remote.test/m/1.png', 1)
        with patch('app.admin.util.get_request',
                   return_value=httpx.Response(200, content=a_png())):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        assert db.session.get(Post, skipped.id).image.source_url == \
            'https://remote.test/m/1.png'

    def test_one_already_on_this_server_when_there_is_no_bucket(self, env):
        already = self.a_post_with(
            env, f"{current_app.config['SERVER_URL']}/static/media/x.png", 1)
        before = written_files()
        self.a_post_with(env, 'https://remote.test/m/2.png', 2)
        with patch('app.admin.util.get_request', return_value=httpx.Response(
                200, content=a_png(),
                headers={'content-type': 'image/png'})):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        try:
            assert db.session.get(Post, already.id).image.source_url == \
                f"{current_app.config['SERVER_URL']}/static/media/x.png"
        finally:
            for path in written_files() - before:
                os.unlink(path)

    def test_a_local_path_whose_file_is_not_there_without_a_bucket(self, env):
        skipped = self.a_post_with(
            env, 'app/static/media/posts/zz/zz/gone.png', 1)
        before = written_files()
        self.a_post_with(env, 'https://remote.test/m/2.png', 2)
        with patch('app.admin.util.get_request', return_value=httpx.Response(
                200, content=a_png(),
                headers={'content-type': 'image/png'})):
            move_community_images_to_here(env.community.id)
        db.session.expire_all()
        try:
            assert db.session.get(Post, skipped.id).image.source_url == \
                'app/static/media/posts/zz/zz/gone.png'
        finally:
            for path in written_files() - before:
                os.unlink(path)
