"""v1.8.10's scheduled posts in the API: a post given `scheduled_for` is held (POST_STATUS_SCHEDULED) and answered
as a scheduled post view; /post/scheduled and /post/scheduled/list show only its author their own.
"""
from datetime import timedelta

import pytest

from app import db
from app.api.alpha.utils.post import (get_scheduled_post, get_scheduled_post_list, post_post, post_post_delete,
                                      put_post)
from app.constants import POST_STATUS_SCHEDULED
from app.models import Post, utcnow
from tests.factories import make_community, make_community_member
from tests.test_api_post_actions import env, token  # noqa: F401 -- env is a fixture


def later(hours):
    return (utcnow() + timedelta(hours=hours)).isoformat(timespec='seconds') + 'Z'


def schedule(env, title, hours, community=None, **data):  # noqa: F811
    answer = post_post(token(env.author), {'title': title, 'community_id': (community or env.community).id,
                                           'scheduled_for': later(hours), **data})
    return answer['scheduled_post_view']['scheduled_post']['id']


def titles(answer):
    return [view['scheduled_post']['title'] for view in answer['scheduled_posts']]


def test_a_scheduled_post_is_held_and_answered_as_one(env):  # noqa: F811
    post_id = schedule(env, 'later on', 5, repeat='weekly')

    post = db.session.get(Post, post_id)
    assert post.status == POST_STATUS_SCHEDULED and post.repeat == 'weekly'
    view = get_scheduled_post(token(env.author), {'id': post_id})['scheduled_post_view']['scheduled_post']
    assert view['repeat'] == 'Weekly' and view['next_scheduled_for'].startswith(post.scheduled_for.isoformat()[:16])


def test_only_its_author_may_read_it(env):  # noqa: F811
    post_id = schedule(env, 'later on', 5)

    with pytest.raises(Exception, match='Not authorized to view'):
        get_scheduled_post(token(env.stranger), {'id': post_id})
    with pytest.raises(Exception, match='incorrect login'):
        get_scheduled_post(None, {'id': post_id})


def test_the_list_is_the_authors_soonest_first(env):  # noqa: F811
    schedule(env, 'in five hours', 5)
    schedule(env, 'in one hour', 1)

    assert titles(get_scheduled_post_list(token(env.author), {})) == ['in one hour', 'in five hours']
    assert titles(get_scheduled_post_list(token(env.author), {'sort': 'Old'})) == ['in five hours', 'in one hour']
    assert titles(get_scheduled_post_list(token(env.author), {'sort': 'Relevance'})) == ['in one hour',
                                                                                        'in five hours']
    assert titles(get_scheduled_post_list(token(env.stranger), {})) == []
    with pytest.raises(Exception, match='incorrect login'):
        get_scheduled_post_list(None, {})


def test_the_list_pages(env):  # noqa: F811
    for hours in (1, 2, 3):
        schedule(env, f'in {hours}', hours)

    first = get_scheduled_post_list(token(env.author), {'limit': 2})
    second = get_scheduled_post_list(token(env.author), {'limit': 2, 'page_cursor': first['next_page']})
    third = get_scheduled_post_list(token(env.author), {'limit': 2, 'page': 2})

    assert titles(first) == ['in 1', 'in 2'] and titles(second) == ['in 3'] and second['next_page'] is None
    assert titles(third) == ['in 3']


def test_the_list_narrows_to_one_community(env):  # noqa: F811
    other = make_community('elsewhere')
    make_community_member(env.author, other)
    schedule(env, 'here', 1)
    schedule(env, 'there', 2, community=other)

    assert titles(get_scheduled_post_list(token(env.author), {'community_id': other.id})) == ['there']
    assert titles(get_scheduled_post_list(token(env.author), {'community_name': 'elsewhere'})) == ['there']
    with pytest.raises(Exception, match='Community not found$'):
        get_scheduled_post_list(token(env.author), {'community_id': 999999})
    with pytest.raises(Exception, match='case-sensitive'):
        get_scheduled_post_list(token(env.author), {'community_name': 'nowhere'})


def test_the_list_can_be_searched(env):  # noqa: F811
    schedule(env, 'about otters', 1)
    schedule(env, 'about badgers', 2)

    assert titles(get_scheduled_post_list(token(env.author), {'q': 'otters'})) == ['about otters']


def test_a_deleted_one_is_listed_only_when_asked(env):  # noqa: F811
    post_id = schedule(env, 'withdrawn', 1)

    answer = post_post_delete(token(env.author), {'post_id': post_id, 'deleted': True})

    assert answer['scheduled_post_view']['scheduled_post']['id'] == post_id
    assert titles(get_scheduled_post_list(token(env.author), {})) == []
    assert titles(get_scheduled_post_list(token(env.author), {'include_deleted': True})) == ['withdrawn']


def test_an_edit_keeps_it_scheduled_unless_told_otherwise(env):  # noqa: F811
    post_id = schedule(env, 'draft', 3)

    kept = put_post(token(env.author), {'post_id': post_id, 'title': 'redrafted'})
    moved = put_post(token(env.author), {'post_id': post_id, 'scheduled_for': later(9), 'repeat': 'daily'})

    assert kept['scheduled_post_view']['scheduled_post']['title'] == 'redrafted'
    post = db.session.get(Post, post_id)
    assert post.status == POST_STATUS_SCHEDULED and post.repeat == 'daily'
    assert moved['scheduled_post_view']['scheduled_post']['repeat'] == 'Daily'


def test_a_time_already_past_is_refused(env):  # noqa: F811
    with pytest.raises(Exception, match='must be scheduled for the future'):
        schedule(env, 'too late', -1)
    assert Post.query.filter_by(title='too late').count() == 0
