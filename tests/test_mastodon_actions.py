"""Favourite, unfavourite, bookmark and unbookmark a status from a Mastodon app (plan W1.3; M5: a favourite is an
upvote). Each goes through the shared functions, is idempotent as Mastodon's are, and answers the status."""
from itertools import count

import pytest

import app.shared.tasks.boosts as boosts
from app import db
from app.constants import VISIBILITY_FOLLOWERS
from app.mastodon import ids
from app.models import PostBookmark, PostBoost, PostReplyBookmark, PostReplyVote, PostVote
from app.oauth_server import tokens
from tests.factories import make_community, make_post, make_post_reply, make_user

_SERIAL = count()


@pytest.fixture
def world(app, api_baseline):
    registered, _secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read write follow', None)
    me = make_user(None, 'actor', local=True)
    author = make_user(None, 'actedon', local=True)
    community = make_community('actions')
    post = make_post(community, author, ap_id=f'https://test.piefed.local/post/a{next(_SERIAL)}')
    reply = make_post_reply(post, author)
    access, _refresh = tokens.issue_token(registered, me, 'read write')
    return {'me': me, 'post': post, 'reply': reply, 'app': registered, 'token': access, 'client': app.test_client()}


def _do(world, row, action, token=None):
    return world['client'].post(f'/api/v1/statuses/{ids.status_id(row) if row is not None else "123"}/{action}',
                                headers={'Authorization': f"Bearer {token or world['token']}"})


def _votes(world, model, column, row):
    return db.session.query(model).filter(model.user_id == world['me'].id, column == row.id).all()


@pytest.mark.parametrize('which', ['post', 'reply'])
def test_favourite_is_an_upvote_and_twice_is_once(world, which):
    row = world[which]
    model, column = (PostVote, PostVote.post_id) if which == 'post' else (PostReplyVote, PostReplyVote.post_reply_id)
    first = _do(world, row, 'favourite')
    assert first.status_code == 200, first.get_data(as_text=True)
    assert first.get_json()['favourited'] is True and first.get_json()['favourites_count'] >= 1
    assert _do(world, row, 'favourite').get_json()['favourited'] is True   # not toggled off
    assert [vote.effect > 0 for vote in _votes(world, model, column, row)] == [True]
    undone = _do(world, row, 'unfavourite')
    assert undone.get_json()['favourited'] is False and _votes(world, model, column, row) == []
    assert _do(world, row, 'unfavourite').status_code == 200   # nothing to undo is no error


@pytest.mark.parametrize('which', ['post', 'reply'])
def test_bookmark_and_unbookmark_are_idempotent(world, which):
    row = world[which]
    model, column = (PostBookmark, PostBookmark.post_id) if which == 'post' else \
        (PostReplyBookmark, PostReplyBookmark.post_reply_id)
    assert _do(world, row, 'bookmark').get_json()['bookmarked'] is True
    assert _do(world, row, 'bookmark').status_code == 200
    assert len(_votes(world, model, column, row)) == 1
    assert _do(world, row, 'unbookmark').get_json()['bookmarked'] is False
    assert _do(world, row, 'unbookmark').status_code == 200 and _votes(world, model, column, row) == []


def test_a_status_the_viewer_may_not_read_is_not_found(world):
    world['post'].visibility = VISIBILITY_FOLLOWERS
    db.session.commit()
    assert _do(world, world['post'], 'favourite').status_code == 404
    assert _do(world, None, 'bookmark').status_code == 404


def test_each_needs_its_write_scope(world):
    reader, _refresh = tokens.issue_token(world['app'], world['me'], 'read')
    assert _do(world, world['post'], 'favourite', token=reader).status_code == 403
    narrow, _refresh = tokens.issue_token(world['app'], world['me'], 'write:bookmarks')
    assert _do(world, world['post'], 'bookmark', token=narrow).status_code == 200
    assert _do(world, world['post'], 'favourite', token=narrow).status_code == 403


def test_a_refusal_from_the_shared_write_is_mastodons_json(world, monkeypatch):
    monkeypatch.setitem(world['client'].application.config, 'VOTE_QUOTA', 0)
    response = _do(world, world['post'], 'favourite')
    assert response.status_code == 429 and 'error' in response.get_json()


def test_reblog_boosts_a_post_and_answers_mastodons_reblog_status(world, monkeypatch):
    queued = []
    monkeypatch.setattr(boosts, 'queue_boost', lambda *args, **kwargs: queued.append((args, kwargs)))
    response = _do(world, world['post'], 'reblog')
    assert response.status_code == 200, response.get_data(as_text=True)
    status = response.get_json()
    assert status['reblog']['id'] == ids.status_id(world['post']) and status['reblog']['reblogged'] is True
    assert status['account']['username'] == 'actor' and status['reblog']['reblogs_count'] == 1
    assert _do(world, world['post'], 'reblog').status_code == 200   # twice is once
    assert PostBoost.query.filter_by(user_id=world['me'].id).count() == 1
    undone = _do(world, world['post'], 'unreblog')
    assert undone.get_json()['reblogged'] is False and PostBoost.query.filter_by(user_id=world['me'].id).count() == 0
    assert len(queued) == 2


def test_a_comment_cannot_be_reblogged(world):
    response = _do(world, world['reply'], 'reblog')
    assert response.status_code == 422 and 'cannot be boosted' in response.get_json()['error']
    assert _do(world, world['reply'], 'unreblog').status_code == 422
