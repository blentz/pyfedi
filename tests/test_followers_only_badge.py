"""A followers-only post says so wherever its title or byline shows, and its edit form shows the fixed visibility."""
import pytest

from app import db
from app.constants import COMMUNITY_KIND_MICROBLOGS
from app.models import Site
from tests.factories import make_community, make_community_member, make_post
from tests.test_community_add_post import poster  # noqa: F401 (poster is a fixture)

pytestmark = pytest.mark.usefixtures('site')

BADGE = 'Followers only'


@pytest.fixture
def csrf_on(app, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the post page and edit form render form.csrf_token


def own_post(poster, visibility, microblog=False):
    _, _, community, author, _ = poster
    db.session.get(Site, 1).private_instance = False
    if microblog:
        community = make_community('microblogs', kind=COMMUNITY_KIND_MICROBLOGS)
        make_community_member(author, community)
    post = make_post(community, author, f'https://test.piefed.local/post/{visibility}-{microblog}',
                     title='' if microblog else 'my title', microblog=microblog)
    post.visibility = visibility
    post.body = post.body_html = 'body text here'
    db.session.commit()
    return post


@pytest.mark.parametrize('microblog', [False, True])
def test_the_post_page_marks_a_followers_only_post_to_its_author(csrf_on, poster, microblog):
    post = own_post(poster, 'followers', microblog)
    page = poster[0].get(f'/post/{post.id}', follow_redirects=True).get_data(as_text=True)
    assert BADGE in page


@pytest.mark.parametrize('microblog', [False, True])
def test_the_post_page_does_not_mark_a_public_post(csrf_on, poster, microblog):
    post = own_post(poster, 'public', microblog)
    page = poster[0].get(f'/post/{post.id}', follow_redirects=True).get_data(as_text=True)
    assert BADGE not in page


@pytest.mark.parametrize('microblog', [False, True])
def test_the_profile_teaser_marks_a_followers_only_post_to_its_author(csrf_on, poster, microblog):
    post = own_post(poster, 'followers', microblog)
    page = poster[0].get(f'/u/{poster[3].user_name}').get_data(as_text=True)
    assert 'post_' + str(post.id) in page and BADGE in page


@pytest.mark.parametrize('microblog', [False, True])
def test_the_profile_teaser_does_not_mark_a_public_post(csrf_on, poster, microblog):
    post = own_post(poster, 'public', microblog)
    page = poster[0].get(f'/u/{poster[3].user_name}').get_data(as_text=True)
    assert 'post_' + str(post.id) in page and BADGE not in page


def test_the_edit_form_shows_the_fixed_visibility_as_text_not_a_field(csrf_on, poster):
    post = own_post(poster, 'followers', True)
    page = poster[0].get(f'/post/{post.id}/edit').get_data(as_text=True)
    assert 'Visibility' in page and BADGE in page
    assert 'name="visibility"' not in page


def test_the_edit_form_of_a_public_microblog_post_says_public(csrf_on, poster):
    post = own_post(poster, 'public', True)
    page = poster[0].get(f'/post/{post.id}/edit').get_data(as_text=True)
    assert 'Visibility' in page and BADGE not in page and 'Public' in page
