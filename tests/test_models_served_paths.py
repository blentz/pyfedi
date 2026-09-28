"""How a stored image path becomes a URL: `served_path` in `app/models.py`.

Every image a template shows goes through one of ten methods -- `File.view_url`,
`medium_url`, `thumbnail_url`, `User.avatar_image`, `avatar_thumbnail`,
`cover_image`, and `icon_image`/`header_image` on both Community and Feed -- and each
of them has to turn a stored value into something a browser can fetch. Three kinds of
value reach them: a path under the media root that this instance wrote, an absolute
URL for a file kept in S3 or on a CDN, and `source_url`, which for anything federated
in is a string A PEER CHOSE.

D1363. There were two implementations. `File.view_url`, `medium_url` and
`thumbnail_url` anchored the rewrite -- `value[4:] if value.startswith('app/')` --
while seventeen places in the icon, header, avatar and cover methods used
`value.replace('app/', '/')`, which rewrites that prefix ANYWHERE in the string. So
one File could render two different URLs depending on which method the template
called, and `app/static/media/app/x.png` -- a value a peer can put in `source_url` --
was `/static/media//x.png` from one and `/static/media/app/x.png` from the other.

Each of those seventeen sites was an `if startswith(...) / else` pair returning the
rewritten value or the value itself, which is what `served_path` is; collapsing them
removed thirty-four lines that no test could reach separately, which is why this file
covers ten methods rather than one.
"""
import pytest
from flask import current_app, g

from app import db
from app.models import Community, Feed, File, Site, User, served_path


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    db.session.commit()
    return SimpleNamespace(baseline=api_baseline,
                           server=current_app.config['SERVER_URL'])


def a_file(**columns):
    file = File(**columns)
    db.session.add(file)
    db.session.commit()
    return file


class TestServedPath:
    def test_a_media_path_loses_its_root(self):
        assert served_path('app/static/media/posts/ab/cd/x.png') == \
            '/static/media/posts/ab/cd/x.png'

    def test_only_the_leading_occurrence_is_removed(self):
        """The whole defect: `replace` took every one.

        `app/` appearing again is not something this instance writes -- the shards
        are two characters and the filename is last -- but `source_url` is a peer's
        string and these methods hand it to the same rewrite.
        """
        assert served_path('app/static/media/app/x.png') == '/static/media/app/x.png'

    def test_a_value_that_merely_contains_the_root(self):
        assert served_path('https://peer.test/app/x.png') == \
            'https://peer.test/app/x.png'

    @pytest.mark.parametrize('value', [
        'https://cdn.example/posts/x.png',
        'http://peer.test/x.png',
        '/already/absolute.png',
        'static/media/x.png',
        '',
    ])
    def test_anything_else_comes_back_unchanged(self, value):
        assert served_path(value) == value

    @pytest.mark.parametrize('value', [None, 5, [], {}])
    def test_a_value_that_is_not_a_string_comes_back_unchanged(self, value):
        """These methods are all guarded by `is not None`, so this arm is
        defensive -- and it is the arm that keeps a column holding something
        unexpected from raising inside a template."""
        assert served_path(value) == value

    def test_the_root_on_its_own(self):
        assert served_path('app/') == '/'


class TestAFilesUrls:
    def test_view_url_prefers_the_source(self, env):
        """`source_url` first: the point of `view_url` is the original, wherever it
        lives."""
        file = a_file(source_url='https://peer.test/original.png',
                      file_path='app/static/media/posts/ab/cd/local.webp')

        assert file.view_url() == 'https://peer.test/original.png'

    def test_view_url_falls_back_to_the_local_file(self, env):
        file = a_file(file_path='app/static/media/posts/ab/cd/local.webp')

        assert file.view_url() == \
            f'{env.server}/static/media/posts/ab/cd/local.webp'

    def test_view_url_of_an_absolute_local_path(self, env):
        """A file already in S3: the stored value IS the url."""
        file = a_file(file_path='https://cdn.example/posts/ab/cd/local.webp')

        assert file.view_url() == 'https://cdn.example/posts/ab/cd/local.webp'

    def test_view_url_with_nothing_stored(self, env):
        assert a_file().view_url() == ''

    def test_view_url_resizes_a_pictrs_source(self, env):
        file = a_file(source_url='https://peer.test/pictrs/image/abc.png')

        assert file.view_url(resize=True) == \
            'https://peer.test/pictrs/image/abc.png?thumbnail=1024'

    def test_view_url_leaves_a_pictrs_source_that_already_has_a_query(self, env):
        file = a_file(source_url='https://peer.test/pictrs/image/abc.png?format=webp')

        assert file.view_url(resize=True) == \
            'https://peer.test/pictrs/image/abc.png?format=webp'

    def test_view_url_does_not_resize_a_non_pictrs_source(self, env):
        file = a_file(source_url='https://peer.test/media/abc.png')

        assert file.view_url(resize=True) == 'https://peer.test/media/abc.png'

    def test_medium_url_uses_the_file_path(self, env):
        file = a_file(file_path='app/static/media/posts/ab/cd/medium.webp',
                      thumbnail_path='app/static/media/posts/ab/cd/thumb.webp')

        assert file.medium_url() == \
            f'{env.server}/static/media/posts/ab/cd/medium.webp'

    def test_medium_url_falls_back_to_the_thumbnail(self, env):
        file = a_file(thumbnail_path='app/static/media/posts/ab/cd/thumb.webp')

        assert file.medium_url() == \
            f'{env.server}/static/media/posts/ab/cd/thumb.webp'

    def test_thumbnail_url_falls_back_to_the_source(self, env):
        file = a_file(source_url='https://peer.test/original.png')

        assert file.thumbnail_url() == 'https://peer.test/original.png'

    def test_thumbnail_url_with_nothing_stored(self, env):
        assert a_file().thumbnail_url() == ''

    def test_the_three_agree_about_a_media_path(self, env):
        """The defect was that they did not have to. One File, one stored value,
        one answer."""
        path = 'app/static/media/app/x.png'
        file = a_file(file_path=path, thumbnail_path=path)

        assert file.medium_url() == f'{env.server}/static/media/app/x.png'
        assert file.thumbnail_url() == f'{env.server}/static/media/app/x.png'
        assert file.view_url() == f'{env.server}/static/media/app/x.png'


class TestAUsersAvatarAndCover:
    def a_user(self, env, **files):
        user = env.baseline.user2
        for attribute, file in files.items():
            setattr(user, attribute, file.id)
        db.session.commit()
        return user

    def test_the_avatar_thumbnail(self, env):
        file = a_file(thumbnail_path='app/static/media/users/ab/cd/t.webp')
        user = self.a_user(env, avatar_id=file)

        assert user.avatar_thumbnail() == '/static/media/users/ab/cd/t.webp'

    def test_the_avatar_thumbnail_falls_back_to_the_full_image(self, env):
        file = a_file(file_path='app/static/media/users/ab/cd/a.webp')
        user = self.a_user(env, avatar_id=file)

        assert user.avatar_thumbnail() == '/static/media/users/ab/cd/a.webp'

    def test_no_avatar_at_all(self, env):
        assert env.baseline.user2.avatar_thumbnail() == ''
        assert env.baseline.user2.avatar_image() == ''

    def test_the_avatar_image_falls_back_to_the_source(self, env):
        file = a_file(source_url='https://peer.test/avatar.png')
        user = self.a_user(env, avatar_id=file)

        assert user.avatar_image() == 'https://peer.test/avatar.png'

    def test_an_avatar_row_with_nothing_in_it(self, env):
        user = self.a_user(env, avatar_id=a_file())

        assert user.avatar_image() == ''

    def test_the_cover_image(self, env):
        file = a_file(thumbnail_path='app/static/media/users/ab/cd/c.webp')
        user = self.a_user(env, cover_id=file)

        assert user.cover_image() == '/static/media/users/ab/cd/c.webp'

    def test_the_cover_image_falls_back_to_the_source(self, env):
        file = a_file(source_url='https://peer.test/cover.png')
        user = self.a_user(env, cover_id=file)

        assert user.cover_image() == 'https://peer.test/cover.png'

    def test_no_cover_at_all(self, env):
        assert env.baseline.user2.cover_image() == ''

    def test_a_peers_source_url_under_the_media_root(self, env):
        """`source_url` is a peer's string and it reaches the same rewrite. One
        answer, and the second `app/` left alone."""
        file = a_file(source_url='app/static/media/app/evil.png')
        user = self.a_user(env, avatar_id=file)

        assert user.avatar_image() == '/static/media/app/evil.png'


class TestACommunitysIconAndHeader:
    def a_community(self, env, **files):
        from tests.factories import make_community

        community = make_community('imaged')
        for attribute, file in files.items():
            setattr(community, attribute, file.id)
        db.session.commit()
        return community

    def test_the_default_icon(self, env):
        file = a_file(file_path='app/static/media/communities/ab/cd/i.webp')
        community = self.a_community(env, icon_id=file)

        assert community.icon_image() == '/static/media/communities/ab/cd/i.webp'

    def test_the_tiny_icon(self, env):
        file = a_file(thumbnail_path='app/static/media/communities/ab/cd/t.webp',
                      file_path='app/static/media/communities/ab/cd/i.webp')
        community = self.a_community(env, icon_id=file)

        assert community.icon_image('tiny') == \
            '/static/media/communities/ab/cd/t.webp'

    def test_the_tiny_icon_falls_back_to_the_source(self, env):
        file = a_file(source_url='https://peer.test/icon.png')
        community = self.a_community(env, icon_id=file)

        assert community.icon_image('tiny') == 'https://peer.test/icon.png'

    def test_the_default_icon_falls_back_to_the_source(self, env):
        file = a_file(source_url='https://peer.test/icon.png')
        community = self.a_community(env, icon_id=file)

        assert community.icon_image() == 'https://peer.test/icon.png'

    def test_a_size_nobody_asks_for_gets_the_placeholder(self, env):
        """Only 'default' and 'tiny' are handled, and the method ends at the
        placeholder rather than raising."""
        file = a_file(file_path='app/static/media/communities/ab/cd/i.webp')
        community = self.a_community(env, icon_id=file)

        assert community.icon_image('enormous') == '/static/images/1px.gif'

    def test_no_icon_at_all(self, env):
        assert self.a_community(env).icon_image() == '/static/images/1px.gif'

    def test_an_icon_row_with_nothing_in_it(self, env):
        community = self.a_community(env, icon_id=a_file())

        assert community.icon_image() == '/static/images/1px.gif'

    def test_the_header(self, env):
        file = a_file(file_path='app/static/media/communities/ab/cd/h.webp')
        community = self.a_community(env, image_id=file)

        assert community.header_image() == '/static/media/communities/ab/cd/h.webp'

    def test_the_header_falls_back_to_the_source(self, env):
        file = a_file(source_url='https://peer.test/header.png')
        community = self.a_community(env, image_id=file)

        assert community.header_image() == 'https://peer.test/header.png'

    def test_no_header_at_all(self, env):
        assert self.a_community(env).header_image() == ''


class TestAFeedsIconAndHeader:
    """The Feed copies, byte-identical to the Community ones before this round and
    still separate methods afterwards -- so each needs its own test or a change to
    one of them goes unnoticed."""

    def a_feed(self, env, **files):
        from tests.factories import make_feed

        feed = make_feed(env.baseline.instance_local, 'imagedfeed')
        for attribute, file in files.items():
            setattr(feed, attribute, file.id)
        db.session.commit()
        return feed

    def test_the_default_icon(self, env):
        file = a_file(file_path='app/static/media/feeds/ab/cd/i.webp')

        assert self.a_feed(env, icon_id=file).icon_image() == \
            '/static/media/feeds/ab/cd/i.webp'

    def test_the_tiny_icon(self, env):
        file = a_file(thumbnail_path='app/static/media/feeds/ab/cd/t.webp')

        assert self.a_feed(env, icon_id=file).icon_image('tiny') == \
            '/static/media/feeds/ab/cd/t.webp'

    def test_the_tiny_icon_falls_back_to_the_source(self, env):
        file = a_file(source_url='https://peer.test/icon.png')

        assert self.a_feed(env, icon_id=file).icon_image('tiny') == \
            'https://peer.test/icon.png'

    def test_the_default_icon_falls_back_to_the_source(self, env):
        file = a_file(source_url='https://peer.test/icon.png')

        assert self.a_feed(env, icon_id=file).icon_image() == \
            'https://peer.test/icon.png'

    def test_no_icon_at_all(self, env):
        assert self.a_feed(env).icon_image() == '/static/images/1px.gif'

    def test_the_header(self, env):
        file = a_file(file_path='app/static/media/feeds/ab/cd/h.webp')

        assert self.a_feed(env, image_id=file).header_image() == \
            '/static/media/feeds/ab/cd/h.webp'

    def test_the_header_falls_back_to_the_source(self, env):
        file = a_file(source_url='https://peer.test/header.png')

        assert self.a_feed(env, image_id=file).header_image() == \
            'https://peer.test/header.png'

    def test_no_header_at_all(self, env):
        assert self.a_feed(env).header_image() == ''
