"""The dillo theme's teasers show a post's content warning in place of its body
preview, as the default theme's do (tests/test_content_warning.py). The listing is
/home/<sort>/<view_filter>, the one route that reaches dillo's macros (see
tests/test_dillo_video_teaser.py)."""
import pytest

from app import db
from app.constants import POST_TYPE_ARTICLE, POST_TYPE_IMAGE, POST_TYPE_LINK
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def dillo_listing(db_session):
    make_instance('test.piefed.local', software='piefed')
    author = make_user(make_instance('cw.example'), 'cwauthor')
    viewer = make_user(None, 'dillocwviewer', local=True)
    viewer.theme = 'dillo'
    community = make_community('dillocw')
    for number, post_type in enumerate((POST_TYPE_ARTICLE, POST_TYPE_LINK, POST_TYPE_IMAGE)):
        post = make_post(community, author, f'https://cw.example/notes/{number}', title=f'cw post {number}')
        post.type = post_type
        post.url = f'https://cw.example/{number}.html' if post_type == POST_TYPE_LINK else None
        post.body_html = f'<p>secret body {number}</p>'
        post.content_warning = f'warning {number}'
    db.session.commit()
    return viewer


def test_dillo_teasers_show_the_warning_instead_of_the_body(app, dillo_listing):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(dillo_listing.id)
        sess['_fresh'] = True
    body = client.get('/home/new/all').get_data(as_text=True)

    assert 'themes/dillo/styles.css' in body
    for number in range(3):
        assert f'warning {number}' in body
        assert f'secret body {number}' not in body
