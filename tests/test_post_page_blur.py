"""A sensitive image post is blurred on the post page, as it is in teasers, for
everyone but a viewer who chose to see NSFW content as it is. Gallery images
blur with the primary one."""
import re

import pytest
import respx

from app import db
from app.models import Language, Site
from tests.factories import make_community, make_community_member, make_instance, make_site, make_user
from tests.test_content_warning import activity, ingest
from tests.test_gallery import a_jpeg

IMAGES = [{'type': 'Document', 'mediaType': 'image/jpeg', 'url': f'https://m.example/{n}.jpg', 'name': n}
          for n in 'ab']


@pytest.fixture
def author(db_session):
    return make_user(make_instance('m.example'), 'alice')


@pytest.fixture
def community(db_session, author):
    make_site()
    community = make_community()
    make_community_member(author, community)
    db.session.get(Site, 1).private_instance = False
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    return community


def album_post(community, author, **extra):
    with respx.mock(assert_all_called=False) as router:
        router.route(host='m.example').respond(200, headers={'Content-Type': 'image/jpeg'}, content=a_jpeg())
        post = ingest(community, author, activity(attachment=IMAGES, **extra))
    post.comments_enabled = False  # a signed-in page's reply form needs the CSRF token the test app turns off
    db.session.commit()
    return post


_viewers = iter(range(1000))


def viewer(hide_nsfw=None, hide_nsfl=None):
    user = make_user(make_instance(f'local{next(_viewers)}.example'), f'bob{next(_viewers)}', local=True)
    if hide_nsfw is not None:
        user.hide_nsfw = hide_nsfw
    if hide_nsfl is not None:
        user.hide_nsfl = hide_nsfl
    db.session.commit()
    return user


def page_images(app, post, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    html = client.get(f'/post/{post.id}', follow_redirects=True).get_data(as_text=True)
    block = html[html.index('<div class="post_image">'):]
    block = block[:block.index('</div>')]
    return re.findall(r'<img [^>]*>', block)


def blurred(tag):
    return 'blur' in re.search(r'class="([^"]*)"', tag).group(1) if 'class="' in tag else False


def test_nsfw_image_and_gallery_are_blurred_for_a_default_viewer(app, community, author):
    images = page_images(app, album_post(community, author, sensitive=True), viewer())
    assert len(images) == 2
    assert all(blurred(tag) for tag in images)


def test_nsfw_images_are_not_blurred_for_a_viewer_who_shows_nsfw(app, community, author):
    images = page_images(app, album_post(community, author, sensitive=True), viewer(hide_nsfw=0))
    assert len(images) == 2
    assert not any(blurred(tag) for tag in images)


def test_nsfl_images_are_blurred_for_a_default_viewer(app, community, author):
    images = page_images(app, album_post(community, author, nsfl=True), viewer())
    assert len(images) == 2
    assert all(blurred(tag) for tag in images)


def test_nsfl_images_are_not_blurred_for_a_viewer_who_shows_nsfl(app, community, author):
    images = page_images(app, album_post(community, author, nsfl=True), viewer(hide_nsfl=0))
    assert len(images) == 2
    assert not any(blurred(tag) for tag in images)


def test_an_ordinary_image_post_is_not_blurred(app, community, author):
    images = page_images(app, album_post(community, author), viewer())
    assert len(images) == 2
    assert not any(blurred(tag) for tag in images)


def test_a_spoiler_flair_blurs_the_images_as_it_blurs_the_teaser(app, community, author):
    from app.models import CommunityFlair
    post = album_post(community, author)
    flair = CommunityFlair(community_id=community.id, flair='spoiler', blur_images=True)
    db.session.add(flair)
    post.flair.append(flair)
    db.session.commit()

    images = page_images(app, post, viewer(hide_nsfw=0, hide_nsfl=0))
    assert len(images) == 2
    assert all(blurred(tag) for tag in images)
