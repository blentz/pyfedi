"""The web forms fill their tags field without the body hashtags of a local microblogs post (spec 2026-10-14 §10
A12). Those are stored in post.tags on save, so filling them in would turn each into an explicit tag, and taking a
#word out of the body would no longer drop it. A tag that was both explicit and in the body becomes body-only."""
from unittest.mock import patch

from app import db
from app.constants import POST_TYPE_LINK
from app.models import Post, Tag
from app.post.util import form_tags_string, tags_to_string
from tests.factories import make_community, make_community_member
from tests.test_community_add_post import poster, url  # noqa: F401 (poster is a fixture)


def _tagged_post(community, author, body='about #Cats', names=('cats', 'news')):
    post = Post(user_id=author.id, community_id=community.id, title='A title', body=body,
                body_html=f'<p>{body}</p>', url='https://example.com/a', type=POST_TYPE_LINK,
                ap_id=f'https://test.piefed.local/post/{community.name}')
    for name in names:
        post.tags.append(Tag(name=name, display_as=name.capitalize()))
    db.session.add(post)
    db.session.commit()
    return post


def test_tags_to_string_leaves_out_excluded_names(app, poster):
    _, _, community, author, _ = poster
    post = _tagged_post(community, author)
    assert tags_to_string(post, exclude=['cats']) == 'News'
    assert tags_to_string(post, exclude=['cats', 'news']) is None


def test_a_microblogs_post_fills_only_its_non_body_tags(app, poster):
    _, _, _, author, _ = poster
    post = _tagged_post(make_community('microblogs'), author)
    assert form_tags_string(post) == 'News'


def test_another_communitys_post_fills_all_its_tags(app, poster):
    _, _, community, author, _ = poster
    # Post.tags has no order: the tags are compared as a set (the gate saw 'News, Cats' once in 24 000 tests)
    assert sorted(form_tags_string(_tagged_post(community, author)).split(', ')) == ['Cats', 'News']


def test_the_edit_form_leaves_out_body_tags(app, poster):
    client, _, _, author, _ = poster
    microblogs = make_community('microblogs')
    make_community_member(author, microblogs)
    post = _tagged_post(microblogs, author)

    with patch('app.post.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/post/{post.id}/edit')

    assert response.status_code == 200
    assert render.call_args.kwargs['form'].tags.data == 'News'


def test_a_crosspost_from_a_microblogs_post_leaves_out_its_body_tags(app, poster):
    client, _, community, author, _ = poster
    source = _tagged_post(make_community('microblogs'), author)

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get(url(app, 'community.add_post', actor=community.name, type='link') + f'?source={source.id}')

    assert render.call_args.kwargs['form'].tags.data == 'News'
