"""Interop D24: one predicate decides what is a video or podcast community."""
import pytest
from sqlalchemy import text

from app import db
from app.discovery.media import MEDIA_COMMUNITY_SQL, media_community_clause, media_post_clause, platform_community_clause, platform_of
from app.models import Community, Post
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture(autouse=True)
def founder(db_session):
    make_user(make_instance('test.piefed.local', software='piefed'), 'founder', local=True)


@pytest.fixture
def three(db_session):
    made = {}
    for software, host in (('PeerTube', 'tube.example'), ('castopod', 'pod.example'), ('lemmy', 'lemmy.example')):
        instance = make_instance(host, software=software)
        community = make_community(f'c_{software.lower()}', host=host)
        community.instance_id = instance.id
        author = make_user(instance, f'a_{software.lower()}')
        made[software.lower()] = (community, make_post(community, author, f'https://{host}/p/1'))
    db.session.commit()
    return made


def test_the_clause_matches_peertube_and_castopod_case_insensitively(three):
    assert {c.name for c in Community.query.filter(media_community_clause())} == {'c_peertube', 'c_castopod'}


def test_the_post_clause_matches_their_posts(three):
    assert {p.ap_id for p in Post.query.filter(media_post_clause())} == \
        {'https://tube.example/p/1', 'https://pod.example/p/1'}


def test_the_raw_fragment_selects_the_same_communities(three):
    rows = db.session.execute(text(f'SELECT c.name FROM community c WHERE {MEDIA_COMMUNITY_SQL}')).scalars()
    assert set(rows) == {'c_peertube', 'c_castopod'}


def test_platform_of(three):
    assert platform_of(three['peertube'][0]) == 'peertube'
    assert platform_of(three['castopod'][0]) == 'castopod'
    assert platform_of(three['lemmy'][0]) is None


def test_platform_of_a_community_with_no_instance_or_software(db_session):
    community = make_community('orphan', host='x.example')
    community.instance_id = None
    assert platform_of(community) is None
    instance = make_instance('blank.example', software=None)
    community.instance_id = instance.id
    db.session.commit()
    assert platform_of(community) is None


def test_the_platform_clause_matches_one_platform_case_insensitively(three):
    assert {c.name for c in Community.query.filter(platform_community_clause('peertube'))} == {'c_peertube'}
    assert {c.name for c in Community.query.filter(platform_community_clause('castopod'))} == {'c_castopod'}
