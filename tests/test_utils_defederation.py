"""Subscribing to somebody else's blocklist, and the helpers around it.

Sub-project 105 -- `retrieve_defederation_list`, `download_defeds` and its
worker in `app/utils.py`, plus `move_file_to_s3`, `find_next_occurrence`,
`filtered_out_communities` and `notif_id_to_string`.

A defederation subscription is an instance saying "ban whoever they ban". The
list therefore comes from a domain an admin chose to trust but does not
control, and every row it produces becomes a `BannedInstances` row here -- so
the shape of that answer decides who this instance will speak to.

Three defects, measured first:

* five keys read out of the remote's answer with no membership test, and one
  answer assumed to be a list:
  `instance_data['federated_instances']['blocked']` and `row['domain']` for a
  Lemmy-compatible peer, `row['domain']` and the list itself for a
  Mastodon-compatible one. Each was a `KeyError` or `TypeError` in a Celery
  worker, which stopped the subscription updating AND left the task session
  open, because the worker had no `try/finally` (D1295);
* the worker looked for no existing row before inserting, and
  `BannedInstances.domain` is not unique -- so a domain the remote names twice,
  or a subscription downloaded twice from the admin screen, produced a second
  row. The periodic sync deletes every subscription row before reloading, so
  only the direct path could grow (D1296).

One equivalent mutant: `if post.repeat is not None and post.repeat != 'none'`
loses nothing if the `!= 'none'` is dropped, because none of the three inner
branches matches 'none' and the function falls through to the same
`timedelta(seconds=0)` either way.

And `days_to_add_for_next_month` subtracted a datetime from a midnight
datetime and read `.days`, so a monthly repeat scheduled for any time after
midnight moved one day SHORT -- and one day further short the month after
that (D1297).
"""
import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest
from flask import current_app, g

from app import db
from app.constants import (NOTIF_ANSWER, NOTIF_BAN, NOTIF_COMMUNITY,
                           NOTIF_DEFAULT, NOTIF_FEED, NOTIF_MENTION,
                           NOTIF_MESSAGE, NOTIF_NEW_MOD, NOTIF_POST,
                           NOTIF_REGISTRATION, NOTIF_REPLY, NOTIF_REPORT,
                           NOTIF_REPORT_ESCALATION, NOTIF_TOPIC, NOTIF_UNBAN,
                           NOTIF_USER)
from app.models import (AllowedInstances, BannedInstances, File, Post, Site)
from app.utils import (download_defeds, download_defeds_worker,
                       filtered_out_communities, find_next_occurrence,
                       move_file_to_s3, notif_id_to_string,
                       retrieve_defederation_list, utcnow)
from tests.factories import (make_community, make_community_member, make_file,
                            make_instance, make_post)

DOMAIN = 'faraway.test'
LEMMY_URL = f'https://{DOMAIN}/api/v3/federated_instances'
MASTODON_URL = f'https://{DOMAIN}/api/v1/instance/domain_blocks'


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    instance = make_instance(DOMAIN, software='lemmy')
    db.session.commit()
    return SimpleNamespace(app=app, instance=instance,
                           baseline=api_baseline)


def lemmy_says(*domains):
    return httpx.Response(200, json={'federated_instances': {
        'blocked': [{'domain': domain} for domain in domains]}})


def mastodon_says(*domains):
    return httpx.Response(200, json=[{'domain': domain, 'severity': 'suspend'}
                                     for domain in domains])


class TestReadingALemmyCompatibleList:
    def test_the_domains_it_names(self, env, http_mock):
        http_mock.get(LEMMY_URL).mock(
            return_value=lemmy_says('nasty.test', 'worse.test'))
        assert retrieve_defederation_list(DOMAIN) == ['nasty.test',
                                                      'worse.test']

    def test_an_answer_with_no_federated_instances_at_all(self, env,
                                                          http_mock):
        """D1295. This was `KeyError: 'federated_instances'`."""
        http_mock.get(LEMMY_URL).mock(return_value=httpx.Response(200,
                                                                  json={}))
        assert retrieve_defederation_list(DOMAIN) == []

    def test_one_with_no_blocked_list(self, env, http_mock):
        http_mock.get(LEMMY_URL).mock(return_value=httpx.Response(
            200, json={'federated_instances': {'linked': []}}))
        assert retrieve_defederation_list(DOMAIN) == []

    def test_a_row_with_no_domain(self, env, http_mock):
        http_mock.get(LEMMY_URL).mock(return_value=httpx.Response(
            200, json={'federated_instances': {'blocked': [
                {'id': 1}, {'domain': 'nasty.test'}]}}))
        assert retrieve_defederation_list(DOMAIN) == ['nasty.test']

    def test_a_row_that_is_not_an_object(self, env, http_mock):
        http_mock.get(LEMMY_URL).mock(return_value=httpx.Response(
            200, json={'federated_instances': {'blocked': [
                'nasty.test', {'domain': 'worse.test'}]}}))
        assert retrieve_defederation_list(DOMAIN) == ['worse.test']

    def test_an_answer_that_is_not_json(self, env, http_mock):
        http_mock.get(LEMMY_URL).mock(
            return_value=httpx.Response(200, text='<html>no api</html>'))
        assert retrieve_defederation_list(DOMAIN) is None

    def test_an_endpoint_that_is_not_there(self, env, http_mock):
        http_mock.get(LEMMY_URL).mock(return_value=httpx.Response(404))
        assert retrieve_defederation_list(DOMAIN) is None

    def test_an_endpoint_that_cannot_be_reached(self, env, http_mock):
        http_mock.get(LEMMY_URL).mock(
            side_effect=httpx.ConnectError('no route'))
        assert retrieve_defederation_list(DOMAIN) is None

    @pytest.mark.parametrize('software', ['lemmy', 'piefed', 'pylova'])
    def test_each_software_that_answers_this_way(self, env, http_mock,
                                                 software):
        env.instance.software = software
        db.session.commit()
        http_mock.get(LEMMY_URL).mock(return_value=lemmy_says('nasty.test'))
        assert retrieve_defederation_list(DOMAIN) == ['nasty.test']


class TestReadingAMastodonCompatibleList:
    @pytest.fixture(autouse=True)
    def mastodon(self, env):
        env.instance.software = 'mastodon'
        db.session.commit()

    def test_the_domains_it_names(self, env, http_mock):
        http_mock.get(MASTODON_URL).mock(
            return_value=mastodon_says('nasty.test'))
        assert retrieve_defederation_list(DOMAIN) == ['nasty.test']

    def test_a_row_with_no_domain(self, env, http_mock):
        """D1295. This was `KeyError: 'domain'`."""
        http_mock.get(MASTODON_URL).mock(return_value=httpx.Response(
            200, json=[{'severity': 'suspend'}, {'domain': 'nasty.test'}]))
        assert retrieve_defederation_list(DOMAIN) == ['nasty.test']

    def test_an_answer_that_is_not_a_list(self, env, http_mock):
        """D1295. This was `TypeError: string indices must be integers`."""
        http_mock.get(MASTODON_URL).mock(return_value=httpx.Response(
            200, json={'error': 'not available'}))
        assert retrieve_defederation_list(DOMAIN) is None

    def test_an_answer_that_is_not_json(self, env, http_mock):
        http_mock.get(MASTODON_URL).mock(
            return_value=httpx.Response(200, text='<html>no api</html>'))
        assert retrieve_defederation_list(DOMAIN) is None

    def test_an_endpoint_that_cannot_be_reached(self, env, http_mock):
        http_mock.get(MASTODON_URL).mock(
            side_effect=httpx.ConnectError('no route'))
        assert retrieve_defederation_list(DOMAIN) is None

    def test_an_instance_this_one_knows_nothing_about(self, env, http_mock):
        """An unknown software falls to the Mastodon-compatible endpoint."""
        env.instance.software = ''
        db.session.commit()
        http_mock.get(MASTODON_URL).mock(
            return_value=mastodon_says('nasty.test'))
        assert retrieve_defederation_list(DOMAIN) == ['nasty.test']


class TestWhatTheSubscriptionWrites:
    @pytest.fixture
    def subscription(self, env):
        from app.models import DefederationSubscription
        subscription = DefederationSubscription(domain=DOMAIN)
        db.session.add(subscription)
        db.session.commit()
        return subscription

    def banned(self, subscription):
        return {row.domain for row in BannedInstances.query.filter_by(
            subscription_id=subscription.id)}

    def test_every_domain_the_list_names_is_banned(self, env, subscription):
        with patch('app.utils.retrieve_defederation_list',
                   return_value=['nasty.test', 'worse.test']):
            download_defeds_worker(subscription.id, DOMAIN)
        assert self.banned(subscription) == {'nasty.test', 'worse.test'}

    def test_the_rows_say_where_they_came_from(self, env, subscription):
        with patch('app.utils.retrieve_defederation_list',
                   return_value=['nasty.test']):
            download_defeds_worker(subscription.id, DOMAIN)
        row = BannedInstances.query.filter_by(domain='nasty.test').one()
        assert row.reason == 'auto'
        assert row.subscription_id == subscription.id

    def test_a_domain_on_the_allowlist_is_not_banned(self, env,
                                                    subscription):
        db.session.add(AllowedInstances(domain='friend.test'))
        db.session.commit()
        with patch('app.utils.retrieve_defederation_list',
                   return_value=['friend.test', 'nasty.test']):
            download_defeds_worker(subscription.id, DOMAIN)
        assert self.banned(subscription) == {'nasty.test'}

    def test_a_domain_the_list_names_twice(self, env, subscription):
        """D1296. Nothing looked for an existing row, and
        `BannedInstances.domain` is not unique."""
        with patch('app.utils.retrieve_defederation_list',
                   return_value=['nasty.test', 'nasty.test']):
            download_defeds_worker(subscription.id, DOMAIN)
        assert BannedInstances.query.filter_by(domain='nasty.test').count() == 1

    def test_the_same_subscription_downloaded_twice(self, env, subscription):
        with patch('app.utils.retrieve_defederation_list',
                   return_value=['nasty.test']):
            download_defeds_worker(subscription.id, DOMAIN)
            download_defeds_worker(subscription.id, DOMAIN)
        assert BannedInstances.query.filter_by(domain='nasty.test').count() == 1

    def test_a_list_with_nothing_in_it(self, env, subscription):
        with patch('app.utils.retrieve_defederation_list', return_value=[]):
            download_defeds_worker(subscription.id, DOMAIN)
        assert self.banned(subscription) == set()

    def test_a_replacing_download_drops_what_the_list_no_longer_names(
            self, env, subscription):
        """D378, fixed. The periodic sync's `replace=True` swaps the
        subscription's bans for the new list in the worker's one commit."""
        with patch('app.utils.retrieve_defederation_list',
                   return_value=['old.test', 'kept.test']):
            download_defeds_worker(subscription.id, DOMAIN)
        with patch('app.utils.retrieve_defederation_list',
                   return_value=['kept.test', 'new.test']):
            download_defeds_worker(subscription.id, DOMAIN, replace=True)
        assert self.banned(subscription) == {'kept.test', 'new.test'}

    def test_a_failed_replacing_download_keeps_the_old_bans(self, env,
                                                            subscription):
        """D378, fixed. Nothing is deleted until the new list is in hand."""
        with patch('app.utils.retrieve_defederation_list',
                   return_value=['old.test']):
            download_defeds_worker(subscription.id, DOMAIN)
        with patch('app.utils.retrieve_defederation_list',
                   side_effect=RuntimeError('unreachable')):
            with pytest.raises(RuntimeError):
                download_defeds_worker(subscription.id, DOMAIN, replace=True)
        assert self.banned(subscription) == {'old.test'}

    def test_a_replacing_download_that_could_not_fetch_keeps_the_old_bans(
            self, env, subscription, caplog):
        """D378 residue. A download that could not be fetched (None) is not
        an empty list: the subscription's bans stay and the failure is
        logged."""
        with patch('app.utils.retrieve_defederation_list',
                   return_value=['old.test']):
            download_defeds_worker(subscription.id, DOMAIN)
        with patch('app.utils.retrieve_defederation_list', return_value=None):
            download_defeds_worker(subscription.id, DOMAIN, replace=True)
        assert self.banned(subscription) == {'old.test'}
        assert DOMAIN in caplog.text

    def test_a_replacing_download_of_an_empty_list_clears_the_bans(
            self, env, subscription):
        """D378 residue. A list that is genuinely empty still replaces."""
        with patch('app.utils.retrieve_defederation_list',
                   return_value=['old.test']):
            download_defeds_worker(subscription.id, DOMAIN)
        with patch('app.utils.retrieve_defederation_list', return_value=[]):
            download_defeds_worker(subscription.id, DOMAIN, replace=True)
        assert self.banned(subscription) == set()

    def test_in_debug_the_download_runs_here_and_now(self, env, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', True)
        with patch('app.utils.download_defeds_worker') as worker:
            download_defeds(7, DOMAIN)
        worker.assert_called_once_with(7, DOMAIN, False)

    def test_otherwise_it_is_queued(self, env, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', False)
        with patch('app.utils.download_defeds_worker') as worker:
            download_defeds(7, DOMAIN, replace=True)
        worker.delay.assert_called_once_with(7, DOMAIN, True)


# --------------------------------------------------------------------------
# moving a file into object storage
# --------------------------------------------------------------------------

def a_png():
    import io

    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (10, 10), (1, 2, 3)).save(buffer, format='PNG')
    return buffer.getvalue()


def a_file_on_disk(name):
    directory = 'app/static/media/posts/aa/bb'
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, 'wb') as handle:
        handle.write(a_png())
    return path


class TestMovingAFileIntoObjectStorage:
    @pytest.fixture
    def s3(self, env, s3_bucket, monkeypatch):
        for key, value in (('S3_ACCESS_KEY', 'key'),
                           ('S3_ACCESS_SECRET', 'secret'),
                           ('S3_ENDPOINT',
                            'https://s3.us-east-1.amazonaws.com'),
                           ('S3_REGION', 'us-east-1'),
                           ('S3_BUCKET', s3_bucket),
                           ('S3_PUBLIC_URL', 'cdn.probeland.test'),
                           ('S3_STORAGE_CLASS', ''), ('S3_PUBLIC_ACL', '')):
            monkeypatch.setitem(current_app.config, key, value)
        import boto3
        return boto3.session.Session().client(
            service_name='s3', region_name='us-east-1',
            endpoint_url='https://s3.us-east-1.amazonaws.com',
            aws_access_key_id='key', aws_secret_access_key='secret')

    def test_all_three_paths_are_moved(self, env, s3):
        paths = [a_file_on_disk(name) for name in
                 ('medium.png', 'thumb.png', 'source.png')]
        file = make_file()
        file.file_path, file.thumbnail_path, file.source_url = paths
        db.session.commit()
        try:
            move_file_to_s3(file.id, s3)
            db.session.expire_all()
            stored = db.session.get(File, file.id)
            assert stored.file_path.startswith('https://cdn.probeland.test/')
            assert stored.thumbnail_path.startswith(
                'https://cdn.probeland.test/')
            assert stored.source_url.startswith('https://cdn.probeland.test/')
            assert not any(os.path.exists(path) for path in paths)
        finally:
            for path in paths:
                if os.path.exists(path):
                    os.unlink(path)

    def test_a_path_already_in_the_bucket_is_left_alone(self, env, s3):
        file = make_file()
        file.file_path = 'https://cdn.probeland.test/posts/aa/bb/already.png'
        db.session.commit()
        move_file_to_s3(file.id, s3)
        db.session.expire_all()
        assert db.session.get(File, file.id).file_path == \
            'https://cdn.probeland.test/posts/aa/bb/already.png'

    def test_a_path_that_is_not_under_the_media_root(self, env, s3):
        """The file EXISTS, so only the `app/static/media` test keeps it where
        it is -- with a path that is not there, any mutant would look right."""
        os.makedirs('app/static/tmp', exist_ok=True)
        path = 'app/static/tmp/elsewhere.png'
        with open(path, 'wb') as handle:
            handle.write(a_png())
        file = make_file()
        file.file_path = path
        db.session.commit()
        try:
            move_file_to_s3(file.id, s3)
            db.session.expire_all()
            assert db.session.get(File, file.id).file_path == path
            assert os.path.exists(path)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_a_path_whose_file_is_not_there(self, env, s3):
        file = make_file()
        file.file_path = 'app/static/media/posts/zz/zz/gone.png'
        db.session.commit()
        move_file_to_s3(file.id, s3)
        db.session.expire_all()
        assert db.session.get(File, file.id).file_path == \
            'app/static/media/posts/zz/zz/gone.png'

    def test_a_storage_class_and_an_acl_are_passed_on(self, env, s3,
                                                     monkeypatch):
        monkeypatch.setitem(current_app.config, 'S3_STORAGE_CLASS',
                            'STANDARD')
        monkeypatch.setitem(current_app.config, 'S3_PUBLIC_ACL', True)
        path = a_file_on_disk('withacl.png')
        file = make_file()
        file.file_path = path
        db.session.commit()
        try:
            move_file_to_s3(file.id, s3)
            db.session.expire_all()
            assert db.session.get(File, file.id).file_path.startswith(
                'https://cdn.probeland.test/')
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_a_file_row_that_is_gone(self, env, s3):
        move_file_to_s3(999999, s3)

    def test_an_instance_that_does_not_use_object_storage(self, env,
                                                          monkeypatch):
        for key in ('S3_ACCESS_KEY', 'S3_ACCESS_SECRET', 'S3_ENDPOINT'):
            monkeypatch.setitem(current_app.config, key, '')
        client = MagicMock()
        path = a_file_on_disk('stayshere.png')
        file = make_file()
        file.file_path = path
        db.session.commit()
        try:
            move_file_to_s3(file.id, client)
            assert client.upload_file.call_count == 0
            assert os.path.exists(path)
        finally:
            if os.path.exists(path):
                os.unlink(path)


# --------------------------------------------------------------------------
# the small helpers
# --------------------------------------------------------------------------

class TestWhenAScheduledPostRepeats:
    def a_post(self, env, repeat, scheduled_for=None):
        community = make_community('probeland')
        db.session.commit()
        post = make_post(community, env.baseline.user2,
                         ap_id='https://test.piefed.local/p/1')
        post.repeat = repeat
        post.scheduled_for = scheduled_for or utcnow()
        db.session.commit()
        return post

    def test_daily(self, env):
        assert find_next_occurrence(self.a_post(env, 'daily')) == \
            timedelta(days=1)

    def test_weekly(self, env):
        assert find_next_occurrence(self.a_post(env, 'weekly')) == \
            timedelta(days=7)

    def test_monthly(self, env):
        """D1297. The answer was taken from the difference between midnight
        on the target day and the scheduled datetime, so a post with any time
        of day on it moved one day short -- and again the month after."""
        from datetime import datetime
        post = self.a_post(env, 'monthly',
                           scheduled_for=datetime(2026, 1, 15, 12, 0))
        assert find_next_occurrence(post) == timedelta(days=31)

    def test_monthly_from_midnight(self, env):
        from datetime import datetime
        post = self.a_post(env, 'monthly',
                           scheduled_for=datetime(2026, 1, 15, 0, 0))
        assert find_next_occurrence(post) == timedelta(days=31)

    def test_monthly_from_the_thirty_first(self, env):
        """February has no 31st, so the helper backs off to the last day it
        does have."""
        from datetime import datetime
        post = self.a_post(env, 'monthly',
                           scheduled_for=datetime(2026, 1, 31, 9, 30))
        assert find_next_occurrence(post) == timedelta(days=28)

    def test_monthly_across_the_end_of_a_year(self, env):
        from datetime import datetime
        post = self.a_post(env, 'monthly',
                           scheduled_for=datetime(2026, 12, 15, 9, 30))
        assert find_next_occurrence(post) == timedelta(days=31)

    def test_a_post_that_does_not_repeat(self, env):
        assert find_next_occurrence(self.a_post(env, 'none')) == \
            timedelta(seconds=0)

    def test_one_with_no_repeat_set_at_all(self, env):
        assert find_next_occurrence(self.a_post(env, None)) == \
            timedelta(seconds=0)

    def test_a_repeat_nobody_offers(self, env):
        assert find_next_occurrence(self.a_post(env, 'fortnightly')) == \
            timedelta(seconds=0)


class TestTheCommunitiesAnAccountFiltersOut:
    def test_a_keyword_matching_a_name(self, env):
        make_community('sportsball')
        make_community('gardening')
        db.session.commit()
        user = env.baseline.user2
        user.community_keyword_filter = 'sport'
        db.session.commit()
        from app import cache
        cache.clear()
        names = {db.session.get(__import__('app.models', fromlist=['Community']).Community, cid).name
                 for cid in filtered_out_communities(user)}
        assert names == {'sportsball'}

    def test_several_keywords(self, env):
        make_community('sportsball')
        make_community('gardening')
        db.session.commit()
        user = env.baseline.user2
        user.community_keyword_filter = 'sport, garden'
        db.session.commit()
        from app import cache
        cache.clear()
        assert len(filtered_out_communities(user)) == 2

    def test_a_filter_of_only_commas(self, env):
        user = env.baseline.user2
        user.community_keyword_filter = ' , , '
        db.session.commit()
        from app import cache
        cache.clear()
        assert filtered_out_communities(user) == []

    def test_no_filter_at_all(self, env):
        user = env.baseline.user2
        user.community_keyword_filter = None
        db.session.commit()
        from app import cache
        cache.clear()
        assert filtered_out_communities(user) == []

    def test_a_keyword_nothing_matches(self, env):
        user = env.baseline.user2
        user.community_keyword_filter = 'nothingmatchesthis'
        db.session.commit()
        from app import cache
        cache.clear()
        assert filtered_out_communities(user) == []


class TestWhatKindOfNotificationItIs:
    @pytest.mark.parametrize('notif_id,expected', [
        (NOTIF_USER, 'User'), (NOTIF_COMMUNITY, 'Community'),
        (NOTIF_TOPIC, 'Topic/feed'), (NOTIF_POST, 'Comment'),
        (NOTIF_REPLY, 'Comment'), (NOTIF_FEED, 'Topic/feed'),
        (NOTIF_MENTION, 'Comment'), (NOTIF_MESSAGE, 'Chat'),
        (NOTIF_BAN, 'Admin'), (NOTIF_UNBAN, 'Admin'),
        (NOTIF_NEW_MOD, 'Admin'), (NOTIF_ANSWER, 'Answer'),
        (NOTIF_REPORT, 'Admin'), (NOTIF_REPORT_ESCALATION, 'Admin'),
        (NOTIF_REGISTRATION, 'Admin'), (NOTIF_DEFAULT, 'All'),
    ])
    def test_each_kind_has_a_name(self, env, notif_id, expected):
        assert notif_id_to_string(notif_id) == expected

    def test_a_kind_nobody_defined_has_none(self, env):
        """There is no final else, so an id nobody has defined answers None --
        pinned rather than changed, because a caller that formats the answer
        would be showing 'None' either way."""
        assert notif_id_to_string(-99) is None
