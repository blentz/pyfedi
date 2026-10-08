"""Relayed posts nobody here engaged with expire (spec: Expiry)."""
import itertools
from datetime import timedelta

import pytest

import app.cli as cli
from app import db
from app.constants import POST_STATUS_REVIEWING
from app.models import (Post, PostBookmark, PostVote, Relay, Report, Settings, UserFollower, utcnow)
from app.relays import RELAY_ACCEPTED, STYLE_MASTODON
from app.relays import expiry
from tests.factories import (make_community, make_community_member, make_instance, make_post, make_post_reply,
                             make_user)

pytestmark = pytest.mark.usefixtures('site')

_serial = itertools.count(1)


@pytest.fixture
def world():
    instance = make_instance('remote.example')   # instance 1 and user 1 are what make_user and make_community name
    local = make_user(None, 'localuser', local=True)
    relay = Relay(url='https://relay.example/inbox', style=STYLE_MASTODON, inbox_url='https://relay.example/inbox',
                  follow_activity_id='https://test.piefed.local/activities/relay-follow/x', state=RELAY_ACCEPTED)
    db.session.add(relay)
    db.session.commit()
    return {'relay': relay, 'community': make_community('microblogs'),
            'author': make_user(instance, 'author'), 'local': local,
            'remote': make_user(instance, 'otherremote'), 'instance': instance}


def old_relayed(world, days=8, community=None, relayed=True, **kw):
    n = next(_serial)
    post = make_post(community or world['community'], world['author'], f'https://remote.example/notes/{n}',
                     microblog=True)
    post.created_at = utcnow() - timedelta(days=days)   # arrival, not the author's date
    post.relay_id = world['relay'].id if relayed else None
    for name, value in kw.items():
        setattr(post, name, value)
    db.session.commit()
    return post.id


def present(post_id):
    db.session.expire_all()
    return db.session.query(Post).filter(Post.id == post_id).count() == 1


class TestDeletion:

    def test_an_old_unengaged_relayed_post_is_purged(self, app, world):
        post_id = old_relayed(world)
        assert expiry.expire_relayed_posts() == {'deleted': 1, 'kept': 0, 'failed': 0}
        assert not present(post_id)

    def test_a_vote_from_a_remote_user_does_not_keep_it(self, app, world):
        post_id = old_relayed(world)
        db.session.add(PostVote(user_id=world['remote'].id, author_id=world['author'].id, post_id=post_id, effect=1))
        db.session.commit()
        assert expiry.expire_relayed_posts() == {'deleted': 1, 'kept': 0, 'failed': 0}
        assert not present(post_id)

    def test_a_follow_of_another_author_does_not_keep_it(self, app, world):
        post_id = old_relayed(world)
        db.session.add(UserFollower(local_user_id=world['local'].id, remote_user_id=world['remote'].id,
                                    is_accepted=True, is_inward=False))
        db.session.commit()
        assert expiry.expire_relayed_posts() == {'deleted': 1, 'kept': 0, 'failed': 0}
        assert not present(post_id)

    def test_a_remote_member_of_another_community_does_not_keep_it(self, app, world):
        other = make_community('elsewhere')
        make_community_member(world['remote'], other)
        post_id = old_relayed(world, community=other)
        assert expiry.expire_relayed_posts() == {'deleted': 1, 'kept': 0, 'failed': 0}
        assert not present(post_id)

    def test_a_local_member_of_the_microblogs_community_does_not_keep_it(self, app, world):
        make_community_member(world['local'], world['community'])
        post_id = old_relayed(world)
        assert expiry.expire_relayed_posts() == {'deleted': 1, 'kept': 0, 'failed': 0}
        assert not present(post_id)


class TestKept:

    def test_a_local_users_vote_keeps_it(self, app, world):
        post_id = old_relayed(world)
        db.session.add(PostVote(user_id=world['local'].id, author_id=world['author'].id, post_id=post_id, effect=1))
        db.session.commit()
        assert expiry.expire_relayed_posts() == {'deleted': 0, 'kept': 1, 'failed': 0}
        assert present(post_id)

    def test_a_local_reply_keeps_it(self, app, world):
        post_id = old_relayed(world)
        make_post_reply(db.session.get(Post, post_id), world['local'])
        assert expiry.expire_relayed_posts() == {'deleted': 0, 'kept': 1, 'failed': 0}
        assert present(post_id)

    def test_a_bookmark_keeps_it(self, app, world):
        post_id = old_relayed(world)
        db.session.add(PostBookmark(user_id=world['local'].id, post_id=post_id))
        db.session.commit()
        assert expiry.expire_relayed_posts() == {'deleted': 0, 'kept': 1, 'failed': 0}
        assert present(post_id)

    def test_a_report_keeps_it(self, app, world):
        post_id = old_relayed(world)
        db.session.add(Report(reasons='spam', type=1, reporter_id=world['local'].id, suspect_post_id=post_id))
        db.session.commit()
        assert expiry.expire_relayed_posts() == {'deleted': 0, 'kept': 1, 'failed': 0}
        assert present(post_id)

    def test_an_author_a_local_user_follows_keeps_it(self, app, world):
        post_id = old_relayed(world)
        db.session.add(UserFollower(local_user_id=world['local'].id, remote_user_id=world['author'].id,
                                    is_accepted=True, is_inward=False))
        db.session.commit()
        assert expiry.expire_relayed_posts() == {'deleted': 0, 'kept': 1, 'failed': 0}
        assert present(post_id)

    def test_a_post_in_a_community_with_a_local_member_keeps_it(self, app, world):
        other = make_community('cats')
        make_community_member(world['local'], other)
        post_id = old_relayed(world, community=other)
        assert expiry.expire_relayed_posts() == {'deleted': 0, 'kept': 1, 'failed': 0}
        assert present(post_id)

    def test_a_sticky_post_is_kept(self, app, world):
        post_id = old_relayed(world, sticky=True)
        assert expiry.expire_relayed_posts() == {'deleted': 0, 'kept': 1, 'failed': 0}
        assert present(post_id)

    def test_a_post_under_review_is_kept(self, app, world):
        post_id = old_relayed(world, status=POST_STATUS_REVIEWING)
        assert expiry.expire_relayed_posts() == {'deleted': 0, 'kept': 1, 'failed': 0}
        assert present(post_id)


class TestNotSelected:

    def test_a_post_younger_than_the_retention_period_is_left(self, app, world):
        post_id = old_relayed(world, days=6)
        assert expiry.expire_relayed_posts() == {'deleted': 0, 'kept': 0, 'failed': 0}
        assert present(post_id)

    def test_a_non_relayed_old_post_is_left(self, app, world):
        post_id = old_relayed(world, relayed=False)
        assert expiry.expire_relayed_posts() == {'deleted': 0, 'kept': 0, 'failed': 0}
        assert present(post_id)

    def test_a_post_whose_relay_was_deleted_is_left(self, app, world):
        post_id = old_relayed(world)
        db.session.delete(world['relay'])
        db.session.commit()
        assert db.session.get(Post, post_id).relay_id is None
        assert expiry.expire_relayed_posts() == {'deleted': 0, 'kept': 0, 'failed': 0}
        assert present(post_id)


class TestSettingAndCap:

    def test_the_retention_default_is_seven_days(self, app, world):
        assert expiry.relay_retention_days() == 7

    def test_the_retention_setting_is_read(self, app, world):
        db.session.add(Settings(name='relay_retention_days', value='3'))
        db.session.commit()
        post_id = old_relayed(world, days=4)
        assert expiry.relay_retention_days() == 3
        assert expiry.expire_relayed_posts() == {'deleted': 1, 'kept': 0, 'failed': 0}
        assert not present(post_id)

    def test_zero_retention_deletes_nothing(self, app, world):
        db.session.add(Settings(name='relay_retention_days', value='0'))
        db.session.commit()
        post_id = old_relayed(world)
        assert expiry.expire_relayed_posts() == {'deleted': 0, 'kept': 0, 'failed': 0}
        assert present(post_id)

    def test_one_run_deletes_every_batch_until_none_are_left(self, app, world, monkeypatch):
        monkeypatch.setattr(expiry, 'RELAY_EXPIRY_BATCH', 2)
        ids = [old_relayed(world) for _ in range(5)]
        assert expiry.expire_relayed_posts() == {'deleted': 5, 'kept': 0, 'failed': 0}
        assert not any(present(i) for i in ids)

    def test_the_time_budget_stops_the_loop(self, app, world, monkeypatch):
        monkeypatch.setattr(expiry, 'RELAY_EXPIRY_BATCH', 2)
        clock = {'now': 0}
        monkeypatch.setattr(expiry.time, 'monotonic', lambda: clock['now'])
        real = Post.delete_dependencies

        def slow(self, *a, **k):
            clock['now'] += 400   # two posts take 800s, past RELAY_EXPIRY_SECONDS
            return real(self, *a, **k)
        monkeypatch.setattr(Post, 'delete_dependencies', slow)
        ids = [old_relayed(world) for _ in range(5)]
        assert expiry.expire_relayed_posts()['deleted'] == 2
        assert sum(present(i) for i in ids) == 3

    def test_the_time_budget_is_ten_minutes(self):
        assert expiry.RELAY_EXPIRY_SECONDS == 600

    def test_the_cdn_is_flushed_once_with_every_url(self, app, world, monkeypatch):
        monkeypatch.setattr(expiry, 'RELAY_EXPIRY_BATCH', 2)
        flushed = []
        monkeypatch.setattr(expiry, 'flush_cdn_cache', lambda urls: flushed.append(list(urls)))
        real = Post.delete_dependencies

        def collecting(self, *a, cache_urls=None, **k):
            cache_urls.append(f'https://cdn.example/{self.id}')
            return real(self, *a, cache_urls=cache_urls, **k)
        monkeypatch.setattr(Post, 'delete_dependencies', collecting)
        ids = [old_relayed(world) for _ in range(5)]
        expiry.expire_relayed_posts()
        assert flushed == [[f'https://cdn.example/{i}' for i in ids]]

    def test_no_flush_when_there_are_no_urls(self, app, world, monkeypatch):
        flushed = []
        monkeypatch.setattr(expiry, 'flush_cdn_cache', lambda urls: flushed.append(urls))
        old_relayed(world)
        expiry.expire_relayed_posts()
        assert flushed == []

    def test_a_post_posted_long_ago_but_arrived_recently_is_left(self, app, world):
        post_id = old_relayed(world, days=1)
        db.session.get(Post, post_id).posted_at = utcnow() - timedelta(days=30)
        db.session.commit()
        assert expiry.expire_relayed_posts() == {'deleted': 0, 'kept': 0, 'failed': 0}
        assert present(post_id)


class TestFailure:

    def test_one_failing_post_does_not_stop_the_others(self, app, world, monkeypatch):
        ids = [old_relayed(world) for _ in range(3)]
        real = Post.delete_dependencies

        def selective(self, *a, **k):
            if self.id == ids[1]:
                raise RuntimeError('boom')
            return real(self, *a, **k)

        monkeypatch.setattr(Post, 'delete_dependencies', selective)
        assert expiry.expire_relayed_posts() == {'deleted': 2, 'kept': 0, 'failed': 1}
        assert [present(i) for i in ids] == [False, True, False]

    def test_a_failing_post_is_attempted_once_per_run(self, app, world, monkeypatch):
        monkeypatch.setattr(expiry, 'RELAY_EXPIRY_BATCH', 2)
        ids = [old_relayed(world) for _ in range(5)]
        attempts = []
        real = Post.delete_dependencies

        def selective(self, *a, **k):
            attempts.append(self.id)
            if self.id == ids[0]:
                raise RuntimeError('boom')
            return real(self, *a, **k)

        monkeypatch.setattr(Post, 'delete_dependencies', selective)
        assert expiry.expire_relayed_posts() == {'deleted': 4, 'kept': 0, 'failed': 1}
        assert attempts.count(ids[0]) == 1

    def test_a_failure_outside_a_single_post_is_raised(self, app, world, monkeypatch):
        monkeypatch.setattr(expiry, '_kept_clause', lambda: 1 / 0)
        with pytest.raises(ZeroDivisionError):
            expiry.expire_relayed_posts()

    def test_a_non_integer_retention_setting_means_seven_days(self, app, world):
        db.session.add(Settings(name='relay_retention_days', value='"soon"'))
        db.session.commit()
        assert expiry.relay_retention_days() == 7


class TestDailyMaintenance:

    def test_the_daily_command_runs_the_expiry(self, app, monkeypatch):
        calls = []
        monkeypatch.setattr(cli, 'expire_relayed_posts', lambda: calls.append(1))
        # Every other step is stubbed: this test is about the call, not the maintenance.
        for name in ('cleanup_old_notifications', 'cleanup_old_read_posts', 'cleanup_send_queue',
                     'process_expired_bans', 'remove_old_community_content', 'remove_old_bot_content',
                     'update_hashtag_counts', 'update_community_stats', 'cleanup_old_voting_data',
                     'unban_expired_users', 'sync_defederation_subscriptions', 'recalculate_user_attitudes',
                     'calculate_community_activity_stats', 'cleanup_old_activitypub_logs', 'clean_up_tmp',
                     'delete_old_soft_deleted_content', 'archive_old_posts', 'archive_old_users', 'pwn_bots',
                     'log_cron_task_to_db'):
            monkeypatch.setattr(cli, name, lambda *a, **k: None)
        for name in ('check_instance_health', 'monitor_healthy_instances'):
            monkeypatch.setattr(cli, name, type('T', (), {'delay': staticmethod(lambda *a, **k: None)}))
        monkeypatch.setattr(cli, 'sleep', lambda *_: None)
        monkeypatch.setattr(cli, 'get_setting', lambda *a, **k: False)
        cli.register(app)   # pyfedi.py registers the commands; the test app has none
        result = app.test_cli_runner().invoke(args=['daily-maintenance'])
        assert result.exception is None, result.output
        assert calls == [1]


    def test_the_celery_variant_queues_the_expiry(self, app, monkeypatch):
        queued = []
        monkeypatch.setattr(cli, 'expire_relayed_posts', type('T', (), {'delay': staticmethod(lambda: queued.append(1))}))
        for name in ('cleanup_old_notifications', 'cleanup_old_read_posts', 'cleanup_send_queue',
                     'process_expired_bans', 'remove_old_community_content', 'update_hashtag_counts',
                     'delete_old_soft_deleted_content', 'update_community_stats', 'cleanup_old_voting_data',
                     'unban_expired_users', 'sync_defederation_subscriptions', 'check_instance_health',
                     'monitor_healthy_instances', 'recalculate_user_attitudes', 'calculate_community_activity_stats',
                     'cleanup_old_activitypub_logs', 'archive_old_posts', 'archive_old_users', 'clean_up_tmp'):
            monkeypatch.setattr(cli, name, type('T', (), {'delay': staticmethod(lambda *a, **k: None)}))
        monkeypatch.setattr(cli, 'log_cron_task_to_db', lambda *a, **k: None)
        monkeypatch.setattr(cli.plugins, 'fire_hook', lambda *a, **k: None)
        monkeypatch.setattr(cli, 'sleep', lambda *_: None)
        monkeypatch.setattr(cli, 'get_setting', lambda *a, **k: False)
        monkeypatch.setitem(app.config, 'DEBUG', False)
        app.debug = False
        cli.register(app)
        result = app.test_cli_runner().invoke(args=['daily-maintenance-celery'])
        assert result.exception is None, result.output
        assert queued == [1]
