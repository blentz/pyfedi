"""`flask publish-scheduled-posts` (app/cli.py), which publishes a scheduled post
when its time comes -- once, or once per occurrence of a repeating schedule.

F7 (docs/superpowers/specs/2026-08-25-permission-callsite-audit.md), fixed by
owner ruling: `can_create_post` was checked when the post was scheduled and never
again, so an author banned from the community in between went on publishing --
indefinitely, for a repeating post. Each occurrence now re-checks it; a refused
occurrence is skipped and logged, and a repeating schedule carries on.
"""
from datetime import timedelta

import pytest

from app import cli, db
from app.constants import POST_STATUS_PUBLISHED, POST_STATUS_SCHEDULED
from app.models import CommunityMember, ModLog, Post, utcnow
from tests.factories import (make_community, make_community_ban, make_community_member,
                             make_instance, make_user)


@pytest.fixture
def scheduled(app, db_session, monkeypatch):
    """A verified local author and a post of theirs whose scheduled time has passed."""
    from types import SimpleNamespace

    instance = make_instance('test.piefed.local', software='piefed')
    make_user(instance, 'founder', local=True)  # takes the id-1 admin seat
    author = make_user(instance, 'author', local=True, with_keys=True)
    author.verified = True
    community = make_community('general')
    db.session.commit()
    make_community_member(author, community)
    post = Post(user_id=author.id, community_id=community.id, instance_id=instance.id,
                title='scheduled', body='', type=1, status=POST_STATUS_SCHEDULED,
                timezone='UTC', scheduled_for=utcnow() - timedelta(hours=1),
                posted_at=utcnow(), created_at=utcnow())
    db.session.add(post)
    db.session.commit()
    monkeypatch.setattr('app.cli.task_selector', lambda *a, **k: None)
    monkeypatch.setattr('app.cli.notify_about_post', lambda *a, **k: None)
    cli.register(app)   # pyfedi.py registers the commands; the test app has none
    return SimpleNamespace(author=author, community=community, post=post)


def _publish(app):
    result = app.test_cli_runner().invoke(args=['publish-scheduled-posts'])
    assert result.exception is None, result.exception


def test_a_one_shot_post_by_an_author_who_may_post_is_published(app, scheduled):
    """The control: with nothing changed since scheduling, the post goes out."""
    _publish(app)

    assert db.session.get(Post, scheduled.post.id).status == POST_STATUS_PUBLISHED


def test_a_one_shot_post_by_an_author_banned_since_is_not_published(app, scheduled):
    """F7, fixed (owner ruling): the author was banned from the community after
    scheduling; this occurrence is skipped, and the post stays scheduled."""
    make_community_ban(scheduled.author, scheduled.community)

    _publish(app)

    assert db.session.get(Post, scheduled.post.id).status == POST_STATUS_SCHEDULED


def test_a_repeating_post_skips_a_refused_occurrence_and_keeps_its_schedule(app, scheduled):
    """F7, fixed (owner ruling): no copy is published for a refused occurrence,
    and the schedule still moves on to the next one."""
    scheduled.post.repeat = 'daily'
    db.session.commit()
    due = scheduled.post.scheduled_for
    make_community_ban(scheduled.author, scheduled.community)

    _publish(app)

    assert Post.query.count() == 1
    assert db.session.get(Post, scheduled.post.id).scheduled_for == due + timedelta(days=1)


def test_a_repeating_post_by_an_author_who_may_post_publishes_a_copy(app, scheduled):
    """The repeating control."""
    scheduled.post.repeat = 'daily'
    db.session.commit()

    _publish(app)

    assert Post.query.filter_by(status=POST_STATUS_PUBLISHED).count() == 1


def test_a_repeating_media_post_is_published_once_and_never_copied(app, scheduled, caplog):
    """Final review #1: a copy took every column, the media record (same uuid), url and image_id with them, so
    purging any copy removed the file and thumbnail every other copy showed. A media post never repeats: it is
    published once, as a one-shot post, and the skipped repeat is logged."""
    post = scheduled.post
    post.repeat = 'daily'
    post.url = 'https://test.piefed.local/static/media/posts/ab/cd/abcdefghijklmno.mp4'
    post.extensions = {'media': {'status': 'ready', 'uuid': '5b0c1a7e-0000-4000-8000-000000000000',
                                 'mime': 'video/mp4', 'bytes': 10}}
    db.session.commit()

    with caplog.at_level('INFO'):
        _publish(app)

    assert Post.query.count() == 1
    published = db.session.get(Post, post.id)
    assert published.status == POST_STATUS_PUBLISHED and published.scheduled_for is None
    assert f'Scheduled post {post.id} has uploaded media: published once, not repeated' in caplog.text


def test_a_scheduled_sticky_post_is_featured_when_it_is_published(app, scheduled, monkeypatch):
    """add_post leaves an unpublished post's sticky to the publish: the scheduled publisher features it then."""
    sent = []
    monkeypatch.setattr('app.shared.post.task_selector', lambda key, **kw: sent.append((key, kw)))
    CommunityMember.query.filter_by(user_id=scheduled.author.id).one().is_moderator = True
    scheduled.post.sticky = True
    db.session.commit()

    _publish(app)

    assert db.session.get(Post, scheduled.post.id).status == POST_STATUS_PUBLISHED
    assert sent == [('sticky_post', {'user_id': scheduled.author.id, 'post_id': scheduled.post.id})]
    assert ModLog.query.filter_by(action='featured_post').count() == 1
