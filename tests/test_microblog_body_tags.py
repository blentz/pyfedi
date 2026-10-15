"""Body `#words` of a local microblogs post are stored as post tags on create and edit (spec 2026-10-14 §10 A12,
replacing A8). The rule is link_hashtags': a `#` at the start or after a non-alphanumeric, then a word holding a letter,
outside <a>, <code> and <pre>. Body tags are added to the explicit ones, each tag once. Other communities are not
scanned."""
import pytest

from app import db
from app.constants import POST_TYPE_ARTICLE, SRC_API
from app.models import Post, Tag
from app.shared.post import edit_post, make_post
from tests.test_shared_post_edit import _api_input
from tests.test_s7_followers_only_create import _api_seed


def _names(post):
    return sorted(tag.name for tag in db.session.get(Post, post.id).tags)


def _create(monkeypatch, body, community='microblogs', **extra):
    s = _api_seed(monkeypatch, community)
    _, post = make_post(_api_input(body=body, **extra), s.community, POST_TYPE_ARTICLE, SRC_API, auth='t')
    return s, post


def test_create_stores_the_body_hashtags(db_session, monkeypatch):
    _, post = _create(monkeypatch, 'hello #Cats and #dogs, not #1 or a#b or `#code`')
    assert _names(post) == ['cats', 'dogs']


def test_create_adds_body_hashtags_to_the_explicit_tags_once(db_session, monkeypatch):
    _, post = _create(monkeypatch, '#Cats and #birds', tags='cats, news')
    assert _names(post) == ['birds', 'cats', 'news']
    assert Tag.query.filter_by(name='cats').count() == 1


def test_a_body_hashtag_reuses_an_existing_tag(db_session, monkeypatch):
    existing = Tag(name='cats', display_as='Cats', post_count=1)
    db.session.add(existing)
    db.session.commit()
    _, post = _create(monkeypatch, '#cats')
    assert [tag.id for tag in db.session.get(Post, post.id).tags] == [existing.id]
    assert Tag.query.count() == 1


def test_another_community_does_not_store_body_hashtags(db_session, monkeypatch):
    _, post = _create(monkeypatch, 'hello #cats', community='general')
    assert _names(post) == []


def test_a_post_without_a_body_stores_only_its_explicit_tags(db_session, monkeypatch):
    _, post = _create(monkeypatch, '', tags='news')
    assert _names(post) == ['news']


@pytest.mark.parametrize('new_body,expected', [
    ('#cats and now #fish', ['cats', 'fish']),
    ('no tags left', []),
], ids=['adds', 'removes'])
def test_edit_stores_the_body_hashtags_again(db_session, monkeypatch, new_body, expected):
    s, post = _create(monkeypatch, 'about #cats')
    assert _names(post) == ['cats']

    edit_post(_api_input(body=new_body), post, POST_TYPE_ARTICLE, SRC_API, auth='t')

    assert _names(post) == expected


def test_an_edit_keeps_an_explicit_tag_the_body_no_longer_names(db_session, monkeypatch):
    s, post = _create(monkeypatch, 'about #cats', tags='cats')

    edit_post(_api_input(body='no tags', tags='cats'), post, POST_TYPE_ARTICLE, SRC_API, auth='t')

    assert _names(post) == ['cats']


def test_the_note_lists_the_stored_tags_and_no_other_body_word(db_session, monkeypatch):
    """The Note's tag list is post.tags (A12): a body #word that is not stored -- a post saved before A12, here one
    whose tag was removed afterwards -- is linked in content and absent from the list."""
    from app.activitypub.serializers import microblog_note
    _, post = _create(monkeypatch, 'hello #cats and #dogs', tags='news')
    post = db.session.get(Post, post.id)
    post.tags = [tag for tag in post.tags if tag.name != 'dogs']
    db.session.commit()

    note = microblog_note(post, [])

    assert sorted(entry['name'] for entry in note['tag'] if entry['type'] == 'Hashtag') == ['#cats', '#news']
    assert 'tag/dogs' in note['content']


def test_a_body_hashtag_longer_than_a_tag_name_is_not_stored(db_session, monkeypatch):
    """Tag.name and display_as are String(256); a longer #word failed the whole save."""
    longest, too_long = 'a' * 256, 'b' * 257
    _, post = _create(monkeypatch, f'#{longest} #{too_long} #cats')
    assert _names(post) == sorted([longest, 'cats'])


def test_a_new_body_tag_keeps_the_spelling_first_used(db_session, monkeypatch):
    """link_hashtags matches case-blind; the Tag it creates shows #SolarStorm as written, not as solarstorm."""
    _, post = _create(monkeypatch, 'a #SolarStorm, then #solarstorm')
    tags = db.session.get(Post, post.id).tags
    assert [(tag.name, tag.display_as) for tag in tags] == [('solarstorm', 'SolarStorm')]
