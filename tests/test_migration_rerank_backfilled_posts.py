"""Migration a3c9e5f1b7d4: stored backfilled posts are re-ranked by when they were published (interop D24).

The SQL is imported from the revision itself, so this tests the statement that ships."""
import importlib.util
import pathlib
from datetime import timedelta

import pytest
from sqlalchemy import text

from app import db
from app.models import utcnow
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')

_REVISION = pathlib.Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / \
    'a3c9e5f1b7d4_rerank_backfilled_posts.py'


def _rerank_sql():
    spec = importlib.util.spec_from_file_location('rerank_revision', _REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RERANK_SQL


@pytest.fixture
def posts(db_session):
    instance = make_instance('tube.example', software='peertube')
    make_user(make_instance('test.piefed.local', software='piefed'), 'founder', local=True)
    community = make_community('chan', host='tube.example')
    author = make_user(instance, 'author')
    now = utcnow()
    backfilled = make_post(community, author, 'https://tube.example/v/1', title='backfilled')
    backfilled.created_at, backfilled.posted_at, backfilled.score = now, now - timedelta(days=40), 5
    backfilled.ranking, backfilled.ranking_scaled = 9999.0, 10009.0   # stale: ranked by arrival
    live = make_post(community, author, 'https://tube.example/v/2', title='live')
    live.created_at, live.posted_at, live.score = now, now - timedelta(minutes=5), 1
    live.ranking, live.ranking_scaled = 1234.5, 1240.5
    db.session.commit()
    return backfilled, live


def test_a_backfilled_post_is_ranked_by_when_it_was_published(posts):
    backfilled, _live = posts
    expected = backfilled.post_ranking(backfilled.score, backfilled.posted_at)

    db.session.execute(text(_rerank_sql()))
    db.session.commit()
    db.session.refresh(backfilled)

    assert backfilled.ranking == pytest.approx(expected, abs=1e-6)
    assert backfilled.ranking_scaled == pytest.approx(10009.0 + (expected - 9999.0), abs=1e-6)


def test_a_live_post_is_left_alone(posts):
    _backfilled, live = posts

    db.session.execute(text(_rerank_sql()))
    db.session.commit()
    db.session.refresh(live)

    assert (live.ranking, live.ranking_scaled) == (1234.5, 1240.5)


@pytest.mark.parametrize('score', [0, -3, 120])
def test_the_sql_matches_post_ranking_for_any_score(posts, score):
    backfilled, _live = posts
    backfilled.score = score
    db.session.commit()

    db.session.execute(text(_rerank_sql()))
    db.session.commit()
    db.session.refresh(backfilled)

    assert backfilled.ranking == pytest.approx(backfilled.post_ranking(score, backfilled.posted_at), abs=1e-6)
