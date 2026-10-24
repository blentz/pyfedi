"""Deleting a File, and the path it is allowed to delete.

Sub-project 116 -- `File.delete_from_disk` and `File.local_path_for_url` in
`app/models.py`. A File row carries up to three locations: `file_path` and
`thumbnail_path`, which this instance wrote, and `source_url`, which is WHERE THE
IMAGE CAME FROM -- and for anything federated in, that is a string a peer chose.

`source_url` is set from `request_json['object']['image']['url']` and
`['icon'][-1]['url']` when a Create is processed, and from a remote actor's icon
and image when a profile is refreshed. Deleting the File turned that string into
a local path by replacement and unlinked it, so a remote instance could name any
file the application user is able to delete (D1324).
"""
import os

import pytest
from flask import current_app, g
from sqlalchemy import text

from app import db
from app.models import File, Site, s3_key_from_url
from app.shared.upload import process_file_delete
from tests.factories import make_community, make_instance, make_post, make_post_reply, make_user


@pytest.fixture
def env(app, api_baseline, tmp_path):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    return SimpleNamespace(app=app, tmp_path=tmp_path,
                           server_url=current_app.config['SERVER_URL'],
                           server_name=current_app.config['SERVER_NAME'],
                           baseline=api_baseline)


def a_file(**columns):
    file = File(**columns)
    db.session.add(file)
    db.session.commit()
    return file


def gone(file, **kwargs):
    """A File deleted as its callers delete one: its files, then its row."""
    file.delete_from_disk(purge_cdn=False, **kwargs)
    db.session.delete(file)
    db.session.commit()


def an_upload(name, **columns):
    """The record our upload code makes of a file it wrote (`process_upload`, `edit_post`): named by its URL, with
    the name it gave the file."""
    return a_file(source_url=f"{current_app.config['SERVER_URL']}/static/media/{name}", file_name=name, **columns)


def a_media_file(name='probe.png', directory='app/static/media'):
    """A real file where this instance keeps its media, cleaned up by the caller."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, 'wb') as handle:
        handle.write(b'not really a png')
    return path


class TestWhatAUrlIsAllowedToName:
    """`local_path_for_url` is the whole of the defence, so it is asserted
    directly as well as through the delete."""

    def test_a_url_on_this_server(self, env):
        path = File().local_path_for_url(
            f'{env.server_url}/static/media/probe.png')
        assert path == os.path.realpath('app/static/media/probe.png')

    def test_a_url_on_another_server(self, env):
        assert File().local_path_for_url(
            'https://peer.test/static/media/probe.png') is None

    def test_a_host_that_merely_contains_this_one(self, env):
        """The old test was `SERVER_NAME in url`, which this passes."""
        assert File().local_path_for_url(
            f'https://evil.test/{env.server_name}/probe.png') is None

    def test_this_host_as_a_subdomain_of_another(self, env):
        assert File().local_path_for_url(
            f'https://{env.server_name}.evil.test/probe.png') is None

    def test_this_host_in_the_query_string(self, env):
        assert File().local_path_for_url(
            f'https://peer.test/probe.png?from={env.server_name}') is None

    def test_this_host_in_the_userinfo(self, env):
        """`https://user@host/` -- the host is what follows the @."""
        assert File().local_path_for_url(
            f'https://{env.server_name}@evil.test/probe.png') is None

    @pytest.mark.parametrize('path', [
        '/../../tmp/target',
        '/static/media/../../../tmp/target',
        '/static/../../etc/passwd',
        '/..%2f..%2ftmp%2ftarget',
        '/%2e%2e/%2e%2e/tmp/target',
        '/static/media/%2e%2e%2f%2e%2e%2f%2e%2e%2ftmp%2ftarget',
    ])
    def test_a_path_that_climbs_out_of_app(self, env, path):
        """Unquoted before it is resolved, so the encoded spellings are the same
        traversal."""
        assert File().local_path_for_url(f'{env.server_url}{path}') is None

    def test_a_sibling_directory_whose_name_starts_with_app(self, env):
        """`/appendix/x` is not inside `/app/`, and a containment check written
        as `startswith(root)` rather than `startswith(root + os.sep)` would take
        it -- which is the classic way this check is got wrong."""
        assert File().local_path_for_url(
            f'{env.server_url}/../appendix/probe.png') is None
        assert File().local_path_for_url(
            f'{env.server_url}/../app.evil/probe.png') is None

    def test_the_app_directory_itself_is_not_a_file(self, env):
        """The root resolves to `app` exactly, which names a directory, and is
        outside the media folders: not offered."""
        assert File().local_path_for_url(f'{env.server_url}/../app') is None

    @pytest.mark.parametrize('path', [
        '/models.py',
        '/static/../models.py',
        '/static/media/../../models.py',
        '/static/media/%2e%2e/%2e%2e/models.py',
        '/static/media/..%2f..%2fmodels.py',
        '/static/js/piefed.js',
        '/static/media',
        '/static/mediaevil/probe.png',
    ])
    def test_a_path_inside_app_but_outside_the_media_folders(self, env, path):
        """A peer names a post image `https://<our host>/models.py`: when the File
        is deleted, nothing but our own media may be unlinked."""
        assert File().local_path_for_url(f'{env.server_url}{path}') is None

    def test_a_path_that_stays_inside_app(self, env):
        assert File().local_path_for_url(
            f'{env.server_url}/static/media/ab/cd/probe.png') == \
            os.path.realpath('app/static/media/ab/cd/probe.png')

    def test_a_path_that_climbs_and_comes_back(self, env):
        """`/static/../static/media/x.png` resolves inside app, so it is allowed
        -- the test is where it LANDS, not whether it contains a `..`."""
        assert File().local_path_for_url(
            f'{env.server_url}/static/../static/media/probe.png') == \
            os.path.realpath('app/static/media/probe.png')

    def test_no_path_at_all(self, env):
        assert File().local_path_for_url(env.server_url) is None
        assert File().local_path_for_url(f'{env.server_url}/') is None

    def test_something_that_is_not_a_url(self, env):
        assert File().local_path_for_url('probe.png') is None
        assert File().local_path_for_url('') is None

    def test_the_server_name_with_its_scheme_missing(self, env):
        """`urlparse('test.piefed.local/x')` has no netloc, so this names
        nothing."""
        assert File().local_path_for_url(f'{env.server_name}/probe.png') is None


class TestDeletingTheFileItself:
    def test_a_local_file_path_is_unlinked(self, env):
        path = a_media_file('probe_local.png')
        file = a_file(file_path=path, file_name='probe_local.png')   # our upload's record (save_icon_file)
        file.delete_from_disk(purge_cdn=False)
        assert not os.path.exists(path)

    def test_a_thumbnail_is_unlinked_too(self, env):
        path = a_media_file('probe_thumb.png')
        file = a_file(thumbnail_path=path, file_name='probe_thumb.png')
        file.delete_from_disk(purge_cdn=False)
        assert not os.path.exists(path)

    def test_both_at_once(self, env):
        first = a_media_file('probe_both_1.png')
        second = a_media_file('probe_both_2.png')
        file = a_file(file_path=first, thumbnail_path=second, file_name='probe_both_1.png')
        file.delete_from_disk(purge_cdn=False)
        assert not os.path.exists(first)
        assert not os.path.exists(second)

    def test_a_file_that_is_already_gone(self, env):
        file = a_file(file_path='app/static/media/probe_absent.png')
        file.delete_from_disk(purge_cdn=False)

    def test_a_source_url_of_ours_is_unlinked(self, env):
        path = a_media_file('probe_source.png')
        an_upload('probe_source.png').delete_from_disk(purge_cdn=False)
        assert not os.path.exists(path)

    def test_a_source_url_pointing_outside_app_deletes_nothing(self, env):
        """D1324, the defect itself. Measured before the repair: this deleted
        `/tmp/probe_delete_target`, a file no part of PieFed owns."""
        target = env.tmp_path / 'target'
        target.write_text('do not delete me')
        relative = os.path.relpath(str(target), os.path.realpath('app'))
        file = a_file(source_url=f'{env.server_url}/{relative}')
        file.delete_from_disk(purge_cdn=False)
        assert target.exists()

    def test_a_source_url_naming_application_code_deletes_nothing(self, env):
        target = os.path.join('app', 'probe_not_media.py')
        with open(target, 'w') as handle:
            handle.write('# do not delete me')
        try:
            for url in (f'{env.server_url}/probe_not_media.py', f'{env.server_url}/static/media/../../probe_not_media.py',
                        f'{env.server_url}/static/media/%2e%2e/%2e%2e/probe_not_media.py'):
                a_file(source_url=url).delete_from_disk(purge_cdn=False)
                assert os.path.exists(target), url
        finally:
            os.unlink(target)

    def test_a_source_url_on_a_peer_deletes_nothing(self, env):
        path = a_media_file('probe_peer.png')
        try:
            file = a_file(source_url='https://peer.test/static/media/probe_peer.png')
            file.delete_from_disk(purge_cdn=False)
            assert os.path.exists(path)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_a_source_url_that_is_not_http_is_left_alone(self, env):
        path = a_media_file('probe_bare.png')
        try:
            file = a_file(source_url='app/static/media/probe_bare.png')
            file.delete_from_disk(purge_cdn=False)
            assert os.path.exists(path)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_a_file_with_nothing_set_at_all(self, env):
        a_file().delete_from_disk(purge_cdn=False)


class TestWhatIsPurgedFromTheCdn:
    def purged(self, env, monkeypatch, file):
        seen = []
        monkeypatch.setattr('app.models.flush_cdn_cache',
                            lambda urls: seen.append(urls))
        file.delete_from_disk()
        return seen[0] if seen else []

    def test_a_local_file_is_purged_by_its_url(self, env, monkeypatch):
        path = a_media_file('probe_purge.png')
        file = a_file(file_path=path, file_name='probe_purge.png')
        assert self.purged(env, monkeypatch, file) == \
            [f'{env.server_url}/static/media/probe_purge.png']

    def test_a_source_url_of_ours_is_purged(self, env, monkeypatch):
        a_media_file('probe_purge_src.png')
        url = f'{env.server_url}/static/media/probe_purge_src.png'
        assert self.purged(env, monkeypatch, an_upload('probe_purge_src.png')) == [url]

    def test_a_peer_s_url_is_not_purged(self, env, monkeypatch):
        """It is not ours to purge, and asking Cloudflare to drop somebody
        else's url is a request that can only fail."""
        file = a_file(source_url='https://peer.test/media/probe.png')
        assert self.purged(env, monkeypatch, file) == []

    def test_a_traversing_url_is_not_purged_either(self, env, monkeypatch):
        file = a_file(source_url=f'{env.server_url}/../../tmp/target')
        assert self.purged(env, monkeypatch, file) == []

    def test_nothing_is_purged_when_the_caller_says_not_to(self, env, monkeypatch):
        path = a_media_file('probe_nopurge.png')
        file = a_file(file_path=path, file_name='probe_nopurge.png')
        seen = []
        monkeypatch.setattr('app.models.flush_cdn_cache',
                            lambda urls: seen.append(urls))
        file.delete_from_disk(purge_cdn=False)
        assert seen == []


class TestFilesKeptInS3:
    @pytest.fixture(autouse=True)
    def in_s3(self, monkeypatch):
        monkeypatch.setitem(current_app.config, 'S3_PUBLIC_URL', 'cdn.example')
        monkeypatch.setattr('app.models._store_files_in_s3', lambda: True)

    def deleted_keys(self, monkeypatch, file):
        seen = []
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.delete_from_s3',
            type('_Task', (), {'delay': staticmethod(lambda keys: seen.append(keys)),
                               '__call__': staticmethod(lambda keys: seen.append(keys))})())
        monkeypatch.setattr('app.models.flush_cdn_cache', lambda urls: None)
        file.delete_from_disk()
        return seen[0] if seen else []

    def test_an_s3_file_is_handed_to_the_task(self, env, monkeypatch):
        file = a_file(file_path='https://cdn.example/media/probe.png')
        assert self.deleted_keys(monkeypatch, file) == ['media/probe.png']

    def test_the_thumbnail_and_the_source_go_too(self, env, monkeypatch):
        file = a_file(file_path='https://cdn.example/media/a.png',
                      thumbnail_path='https://cdn.example/media/b.png',
                      source_url='https://cdn.example/media/c.png')
        assert self.deleted_keys(monkeypatch, file) == \
            ['media/a.png', 'media/b.png', 'media/c.png']

    def test_a_url_on_another_host_is_not_treated_as_s3(self, env, monkeypatch):
        file = a_file(file_path='https://peer.test/media/probe.png')
        assert self.deleted_keys(monkeypatch, file) == []


class TestTheKeyAUrlNames:
    """`s3_key_from_url` is the whole of the defence for the S3 branches, in the
    way `local_path_for_url` is for the on-disk one, so it is asserted directly
    as well as through the delete.

    D1343. Six sites turned a URL into an object key with
    `url.replace(f'https://{S3_PUBLIC_URL}/', '')` behind
    `url.startswith(f'https://{S3_PUBLIC_URL}')`. The prefix test has no
    boundary and the replacement is not anchored, so `source_url` -- a string a
    PEER chose -- reached `delete_objects` as any key it liked.
    """

    @pytest.fixture(autouse=True)
    def public_url(self, monkeypatch):
        monkeypatch.setitem(current_app.config, 'S3_PUBLIC_URL', 'cdn.example')

    def test_an_object_in_our_bucket(self):
        assert s3_key_from_url('https://cdn.example/media/a/b.png') == 'media/a/b.png'

    def test_a_host_that_merely_starts_with_ours(self):
        """Measured before the repair: the whole URL went to `delete_objects` as
        a key, because `startswith` matched and `replace` then found nothing to
        replace."""
        assert s3_key_from_url('https://cdn.example.evil.test/media/a.png') is None

    def test_a_host_that_merely_contains_ours(self):
        assert s3_key_from_url('https://evil.test/cdn.example/a.png') is None

    def test_a_key_climbing_out_of_the_bucket(self):
        assert s3_key_from_url('https://cdn.example/../../secret.png') is None

    def test_a_key_climbing_out_under_escaping(self):
        """`%2e%2e` is `..`, and the path is unquoted before it is read."""
        assert s3_key_from_url('https://cdn.example/%2e%2e/secret.png') is None

    def test_a_single_dot_segment(self):
        assert s3_key_from_url('https://cdn.example/media/./a.png') is None

    def test_a_doubled_separator(self):
        assert s3_key_from_url('https://cdn.example/media//a.png') is None

    def test_the_bucket_root(self):
        """Measured before the repair: the EMPTY key was handed to
        `delete_objects`."""
        assert s3_key_from_url('https://cdn.example/') is None

    def test_a_url_with_no_path(self):
        assert s3_key_from_url('https://cdn.example') is None

    def test_a_query_string_is_not_part_of_the_key(self):
        assert s3_key_from_url('https://cdn.example/media/a.png?v=2') == 'media/a.png'

    @pytest.mark.parametrize('url', ['ftp://cdn.example/media/a.png',
                                     'file:///etc/passwd',
                                     'not a url', '', None, 5, [],
                                     'https://peer.test/media/a.png'])
    def test_what_names_no_object_of_ours(self, url):
        assert s3_key_from_url(url) is None

    def test_http_as_well_as_https(self):
        """This instance publishes `https://{S3_PUBLIC_URL}/...`, but a peer that
        echoes the same object back over http is naming the same object, and the
        delete is equally legitimate."""
        assert s3_key_from_url('http://cdn.example/media/a.png') == 'media/a.png'

    def test_the_host_is_matched_without_regard_to_case(self):
        """A host is case-insensitive, so a peer echoing our own URL back in
        another case is naming the same object -- and whether that object was
        this row's is a separate question, asked separately."""
        assert s3_key_from_url('https://CDN.Example/media/a.png') == 'media/a.png'

    def test_the_key_keeps_its_case(self):
        """An S3 key IS case-sensitive, so the path must not be folded with the
        host: `Media/A.PNG` and `media/a.png` are two objects."""
        assert s3_key_from_url('https://cdn.example/Media/A.PNG') == 'Media/A.PNG'

    def test_a_public_url_carrying_a_path_prefix(self, monkeypatch):
        """`S3_PUBLIC_URL` is interpolated as `https://{it}/`, so a deployment can
        put a path in it. Then the prefix belongs to the bucket, not the key."""
        monkeypatch.setitem(current_app.config, 'S3_PUBLIC_URL', 'cdn.example/bucket')

        assert s3_key_from_url('https://cdn.example/bucket/media/a.png') == 'media/a.png'
        assert s3_key_from_url('https://cdn.example/other/media/a.png') is None
        assert s3_key_from_url('https://cdn.example/bucketing/a.png') is None
        assert s3_key_from_url('https://cdn.example/bucket/') is None

    def test_no_public_url_configured(self, monkeypatch):
        """An instance not using S3 has this empty, and `https:///media/a.png`
        must not read as an object of ours."""
        monkeypatch.setitem(current_app.config, 'S3_PUBLIC_URL', '')

        assert s3_key_from_url('https:///media/a.png') is None
        assert s3_key_from_url('https://cdn.example/media/a.png') is None


class TestAPeerNamingSomeoneElsesObject:
    """D1343, and the security half of it.

    `source_url` arrives from a peer. Measured before the repair, with
    `S3_PUBLIC_URL = cdn.example`: a File whose `source_url` was
    `https://cdn.example/users/victim/avatar.webp` sent exactly that key to
    `delete_objects` -- a remote instance deleting any object in this instance's
    bucket by naming it as its post's image and waiting for the post to be
    deleted.

    Refusing every URL that names an object another row still points at is what
    answers it, because naming an object is not owning it and no URL can settle
    which.
    """

    @pytest.fixture(autouse=True)
    def in_s3(self, monkeypatch):
        monkeypatch.setitem(current_app.config, 'S3_PUBLIC_URL', 'cdn.example')
        monkeypatch.setattr('app.models._store_files_in_s3', lambda: True)

    def deleted_keys(self, monkeypatch, file):
        seen = []
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.delete_from_s3',
            type('_Task', (), {'delay': staticmethod(lambda keys: seen.append(keys)),
                               '__call__': staticmethod(lambda keys: seen.append(keys))})())
        monkeypatch.setattr('app.models.flush_cdn_cache', lambda urls: None)
        file.delete_from_disk()
        return seen[0] if seen else []

    def test_an_avatar_another_file_row_still_uses(self, env, monkeypatch):
        victim = 'https://cdn.example/users/aa/bb/avatar.webp'
        a_file(file_path=victim)
        peers_post_image = a_file(source_url=victim)

        assert self.deleted_keys(monkeypatch, peers_post_image) == []

    @pytest.mark.parametrize('column', ['file_path', 'thumbnail_path', 'source_url'])
    def test_whichever_column_the_other_row_uses(self, env, monkeypatch, column):
        victim = 'https://cdn.example/users/aa/bb/avatar.webp'
        a_file(**{column: victim})
        peers_post_image = a_file(source_url=victim)

        assert self.deleted_keys(monkeypatch, peers_post_image) == []

    def test_an_object_a_post_still_serves(self, env, monkeypatch):
        video = 'https://cdn.example/posts/aa/bb/clip.mp4'
        env.baseline.post1.url = video
        db.session.commit()
        peers_post_image = a_file(source_url=video)

        assert self.deleted_keys(monkeypatch, peers_post_image) == []

    def test_an_object_only_this_row_points_at_is_still_deleted(self, env, monkeypatch):
        """The check has to refuse the peer's naming without breaking the
        legitimate delete it exists to allow: when this instance uploaded the
        original itself, this row is the only reference."""
        file = a_file(source_url='https://cdn.example/posts/aa/bb/original.png')

        assert self.deleted_keys(monkeypatch, file) == \
            ['posts/aa/bb/original.png']

    def test_the_rows_own_other_columns_do_not_block_it(self, env, monkeypatch):
        """A File whose three columns all name the same object must still delete
        it -- `s3_object_is_referenced_elsewhere` excludes the row it is asked
        about, or nothing would ever be deleted."""
        url = 'https://cdn.example/posts/aa/bb/one.png'
        file = a_file(file_path=url, thumbnail_path=url, source_url=url)

        assert self.deleted_keys(monkeypatch, file) == \
            ['posts/aa/bb/one.png', 'posts/aa/bb/one.png', 'posts/aa/bb/one.png']

    @pytest.mark.parametrize('source_url', [
        'https://cdn.example/../../secret.png',
        'https://cdn.example/%2e%2e/secret.png',
        'https://cdn.example/',
        'https://cdn.example.evil.test/anything.png',
    ])
    def test_the_shapes_that_used_to_reach_the_task(self, env, monkeypatch, source_url):
        file = a_file(source_url=source_url)

        assert self.deleted_keys(monkeypatch, file) == []

    def test_an_instance_not_using_s3(self, env, monkeypatch):
        """Without `_store_files_in_s3()` in front of it, a `source_url` naming
        the configured public URL would be handed to `delete_from_s3` on an
        instance that has no S3 credentials at all."""
        monkeypatch.setattr('app.models._store_files_in_s3', lambda: False)
        file = a_file(source_url='https://cdn.example/posts/aa/bb/original.png')

        assert self.deleted_keys(monkeypatch, file) == []

    def test_a_peer_url_still_takes_the_on_disk_branch(self, env, monkeypatch):
        """The S3 test must not swallow `source_url` values that are not ours:
        those go on to D1324's `local_path_for_url` check as before."""
        file = a_file(source_url=f'{env.server_url}/static/media/gone.png')

        assert self.deleted_keys(monkeypatch, file) == []


class TestOnlyWhatWeWroteIsUnlinked:
    """A File unlinks a local file named by its `source_url` only when our upload code wrote that file: the record
    has the name it gave the file (`file_name`). `source_url` is a string a peer or a user chose (a media-library URL
    row has a `user_file` link and is no upload), so any other File never unlinks anything, however it spells the URL,
    purge or not. Any locally owned record naming the same path keeps it, except from a moderation purge; a peer's
    never does. The CDN copy goes either way."""

    @pytest.fixture
    def owned(self, env):
        path = a_media_file('probe_owned.png')
        yield path
        if os.path.exists(path):
            os.unlink(path)

    @pytest.mark.parametrize('spelt', [
        '{base}/static/media/probe_owned.png',
        '{upper}/static/media/probe_owned.png',
        '{base}//static//media/probe_owned.png',
        '{base}/static/./media/./probe_owned.png',
        '{base}/static/media/%2e/probe_owned.png',
        '{base}/static/media/probe%5fowned.png',
        '{base}/static/media/x/../probe_owned.png',
    ])
    @pytest.mark.parametrize('purge', [False, True])
    def test_a_peers_file_never_unlinks_ours_however_it_is_spelt(self, env, owned, spelt, purge):
        url = spelt.format(base=env.server_url, upper=env.server_url.upper())
        gone(a_file(source_url=url), purge=purge)
        assert os.path.exists(owned), url

    @pytest.mark.parametrize('purge', [False, True])
    def test_a_media_library_url_row_never_unlinks_another_users_file(self, env, owned, purge):
        """The media library's URL box records any URL a user types, with their `user_file` link: that is not a file
        our code wrote. Pasting someone's avatar URL and deleting the row, or an admin purging that user's media, must
        leave the owner's file."""
        url = f'{env.server_url}/static/media/probe_owned.png'
        victim = env.baseline.user2
        victim.avatar = a_file(source_url=url)   # set through the API: no name of ours on it
        db.session.commit()
        attacker = env.baseline.user1
        pasted = a_file(source_url=url)
        db.session.execute(text('INSERT INTO "user_file" (file_id, user_id, size) VALUES (:f, :u, 0)'),
                           {'f': pasted.id, 'u': attacker.id})
        db.session.commit()
        process_file_delete(url, attacker.id, purge=purge)
        assert os.path.exists(owned)
        assert db.session.get(File, pasted.id) is None   # the row itself goes

    @pytest.mark.parametrize('owner', ['avatar', 'cover', 'icon', 'banner', 'post image', 'comment image'])
    def test_an_unmarked_local_record_keeps_the_file_however_it_spells_it(self, env, owned, owner):
        """API-set avatars, covers, icons and banners and legacy post images and thumbnails carry no name of ours, but
        a local account, community or post owns them: they keep the file. Matched by the path it resolves to."""
        spelt = f'{env.server_url}/static/./media//probe_owned.png'
        held = a_file(source_url=spelt)
        if owner in ('avatar', 'cover'):
            setattr(env.baseline.user2, 'avatar_id' if owner == 'avatar' else 'cover_id', held.id)
        elif owner in ('icon', 'banner'):
            community = make_community('localart')
            setattr(community, 'icon_id' if owner == 'icon' else 'image_id', held.id)
        else:   # a legacy post image or media thumbnail, or a comment's image
            post = make_post(env.baseline.community1, env.baseline.user1, ap_id=f'{env.server_url}/post/held')
            target = post if owner == 'post image' else make_post_reply(post, env.baseline.user1)
            target.image_id = held.id
        db.session.commit()
        gone(an_upload('probe_owned.png'))
        assert os.path.exists(owned)

    @pytest.mark.parametrize('column', ['file_path', 'thumbnail_path'])
    def test_a_path_the_server_wrote_on_another_record_keeps_the_file(self, env, owned, column):
        a_file(**{column: 'app/static/media/./probe_owned.png'})
        gone(an_upload('probe_owned.png'))
        assert os.path.exists(owned)

    def test_a_peers_unmarked_record_does_not_keep_our_upload(self, env, owned):
        peer = make_user(make_instance('peer.test'), 'peerowner')
        peer.avatar = a_file(source_url=f'{env.server_url}/static/media/probe_owned.png')
        db.session.commit()
        gone(an_upload('probe_owned.png'))
        assert not os.path.exists(owned)

    def test_our_upload_another_of_our_records_names_is_kept_but_purged_from_the_cdn(self, env, owned):
        an_upload('probe_owned.png')
        purged = []
        an_upload('probe_owned.png').delete_from_disk(cache_urls=purged)
        assert os.path.exists(owned)
        assert purged == [f'{env.server_url}/static/media/probe_owned.png']

    def test_a_peers_record_naming_our_upload_does_not_keep_it(self, env, owned):
        a_file(source_url=f'{env.server_url}/static/media/probe_owned.png')
        an_upload('probe_owned.png').delete_from_disk(purge_cdn=False)
        assert not os.path.exists(owned)

    def test_a_local_authors_post_linking_our_upload_keeps_it_and_a_peers_does_not(self, env, owned):
        url = f'{env.server_url}/static/media/probe_owned.png'
        peer = make_user(make_instance('peer.test'), 'peerauthor')
        remote = make_post(env.baseline.community1, peer, ap_id='https://peer.test/post/1')
        remote.url = url
        db.session.commit()
        local = make_post(env.baseline.community1, env.baseline.user1, ap_id=f'{env.server_url}/post/linker')
        local.url = url
        own = an_upload('probe_owned.png')
        db.session.commit()
        gone(an_upload('probe_owned.png'))
        assert os.path.exists(owned)   # the local author's post links it
        local.image_id = own.id   # its own image: no other reference
        db.session.commit()
        own.delete_from_disk(purge_cdn=False)
        assert not os.path.exists(owned)

    def test_a_moderation_purge_removes_our_upload_whatever_still_names_it(self, env, owned):
        local = make_post(env.baseline.community1, env.baseline.user1, ap_id=f'{env.server_url}/post/linker')
        local.url = f'{env.server_url}/static/media/probe_owned.png'
        an_upload('probe_owned.png')
        db.session.commit()
        purged = []
        an_upload('probe_owned.png').delete_from_disk(cache_urls=purged, purge=True)
        assert not os.path.exists(owned)
        assert purged == [local.url]


class TestALegacyRecord:
    """A record made before our upload code named its files (no `file_name`) cannot prove we wrote the file its
    `source_url` names: that file is never unlinked through it, purge or not (fail closed). Its CDN copy is purged
    whenever it goes, as at BASE. The copies the server itself wrote for it (`file_path`, `thumbnail_path`) go, unless
    any other record of any kind names them (final-review FI1, controller ruling)."""

    @pytest.fixture
    def owned(self, env):
        path = a_media_file('probe_legacy.png')
        yield path
        if os.path.exists(path):
            os.unlink(path)

    @pytest.fixture
    def url(self, env):
        return f'{env.server_url}/static/media/probe_legacy.png'

    def post_image(self, env, author, **columns):
        post = make_post(env.baseline.community1, author, ap_id=f'{env.server_url}/post/{author.user_name}')
        post.image = a_file(**columns)
        db.session.commit()
        return post.image

    def test_its_cdn_copy_is_purged_when_it_goes(self, env, owned, url):
        purged = []
        a_file(source_url=url).delete_from_disk(cache_urls=purged)
        assert purged == [url]
        assert os.path.exists(owned)

    @pytest.mark.parametrize('purge', [False, True])
    def test_its_source_is_never_unlinked_but_its_cdn_copy_is_purged(self, env, owned, url, purge):
        purged = []
        self.post_image(env, env.baseline.user1, source_url=url).delete_from_disk(cache_urls=purged, purge=purge)
        assert os.path.exists(owned)
        assert purged == [url]

    @pytest.mark.parametrize('naming', ['ownerless', 'peer'])
    def test_purging_an_attackers_row_naming_a_victims_legacy_file_leaves_it(self, env, owned, url, naming):
        """B pastes A's file's URL into B's media library and is purged by an admin. A's own row naming it may have no
        owner we can resolve, or a peer's: either way A's file stays."""
        if naming == 'ownerless':
            a_file(source_url=url)
        else:
            self.post_image(env, make_user(make_instance('peer.test'), 'peerowner'), source_url=url)
        pasted = a_file(source_url=url)
        db.session.execute(text('INSERT INTO "user_file" (file_id, user_id, size) VALUES (:f, :u, 0)'),
                           {'f': pasted.id, 'u': env.baseline.user2.id})
        db.session.commit()
        process_file_delete(url, env.baseline.user2.id, purge=True)
        assert os.path.exists(owned)

    @pytest.mark.parametrize('column', ['file_path', 'thumbnail_path'])
    def test_a_copy_the_server_wrote_and_nothing_else_names_goes_on_purge(self, env, column):
        copy = a_media_file('probe_copy.webp')
        image = self.post_image(env, env.baseline.user1, source_url=f'{env.server_url}/static/media/gone.png',
                                **{column: 'app/static/media/probe_copy.webp'})
        image.delete_from_disk(purge_cdn=False, purge=True)
        assert not os.path.exists(copy)

    @pytest.mark.parametrize('other', ['ownerless url', 'peer post link'])
    def test_a_copy_another_record_names_is_kept(self, env, other):
        copy = a_media_file('probe_copy.webp')
        try:
            copy_url = f'{env.server_url}/static/media/probe_copy.webp'
            if other == 'ownerless url':
                a_file(source_url=copy_url)
            else:
                peer = make_user(make_instance('peer.test'), 'peerlinker')
                make_post(env.baseline.community1, peer, ap_id='https://peer.test/post/link').url = copy_url
                db.session.commit()
            image = self.post_image(env, env.baseline.user1, file_path='app/static/media/probe_copy.webp')
            image.delete_from_disk(purge_cdn=False, purge=True)
            assert os.path.exists(copy)
        finally:
            if os.path.exists(copy):
                os.unlink(copy)


MIGRATION = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'migrations', 'versions',
                         'e5b9c3d7a1f4_upload_file_names.py')


def load_migration():
    import importlib.util
    spec = importlib.util.spec_from_file_location('upload_file_names_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_backfill_names_only_what_process_upload_recorded(env):
    """`process_upload` records a file's size (os.path.getsize, from f627ef1f7); the media-library URL box records 0
    (bc7bf012a). A size above 0 is proof the upload wrote the file, so it is given the name the upload gave it."""
    migration = load_migration()
    assert (migration.revision, migration.down_revision) == ('e5b9c3d7a1f4', 'd3a7e5c1f9b2')
    rows = {}
    for key, size, source_url, file_name in (
            ('upload', 123, f'{env.server_url}/static/media/posts/ab/cd/abcdefghijklmno.png', None),
            ('bookmark', 0, f'{env.server_url}/static/media/users/xx/yy/victimavatar.png', None),
            ('unsized', None, f'{env.server_url}/static/media/posts/ef/gh/efghijklmnopqrs.png', None),
            ('named', 9, f'{env.server_url}/static/media/posts/ij/kl/ijklmnopqrstuvw.png', 'kept.png')):
        file = a_file(source_url=source_url, file_name=file_name)
        db.session.execute(text('INSERT INTO "user_file" (file_id, user_id, size) VALUES (:f, :u, :s)'),
                           {'f': file.id, 'u': env.baseline.user1.id, 's': size})
        rows[key] = file.id
    unlinked = a_file(source_url=f'{env.server_url}/static/media/posts/mn/op/mnopqrstuvwxyza.png')
    db.session.commit()
    migration.backfill_file_names(db.session.connection())
    db.session.expire_all()
    names = {key: db.session.get(File, file_id).file_name for key, file_id in rows.items()}
    assert names == {'upload': 'abcdefghijklmno.png', 'bookmark': None, 'unsized': None, 'named': 'kept.png'}
    assert db.session.get(File, unlinked.id).file_name is None


class TestAnUploadIsItsUploaders:
    """An upload we can prove we wrote is kept only by a record of its uploader's own account (or a path the server
    wrote). Another account's bookmark, post image or link post naming it never keeps it: deleting your upload removes
    it, as on any site (final-review FI2)."""

    @pytest.fixture
    def owned(self, env):
        path = a_media_file('probe_mine.png')
        yield path
        if os.path.exists(path):
            os.unlink(path)

    @pytest.fixture
    def upload(self, env):
        """user1's media-library upload."""
        file = an_upload('probe_mine.png')
        db.session.execute(text('INSERT INTO "user_file" (file_id, user_id, size) VALUES (:f, :u, 9)'),
                           {'f': file.id, 'u': env.baseline.user1.id})
        db.session.commit()
        return file

    @pytest.mark.parametrize('reference', ['bookmark', 'post image', 'link post'])
    def test_another_accounts_reference_does_not_keep_it(self, env, owned, upload, reference):
        other = env.baseline.user2
        url = upload.source_url
        if reference == 'bookmark':
            bookmark = a_file(source_url=url)
            db.session.execute(text('INSERT INTO "user_file" (file_id, user_id, size) VALUES (:f, :u, 0)'),
                               {'f': bookmark.id, 'u': other.id})
        else:
            post = make_post(env.baseline.community1, other, ap_id=f'{env.server_url}/post/theirs')
            if reference == 'post image':
                post.image = a_file(source_url=url)
            else:
                post.url = url
        db.session.commit()
        gone(upload)
        assert not os.path.exists(owned)

    @pytest.mark.parametrize('reference', ['avatar', 'link post'])
    def test_its_uploaders_own_other_record_keeps_it(self, env, owned, upload, reference):
        mine = env.baseline.user1
        if reference == 'avatar':
            mine.avatar = a_file(source_url=upload.source_url)
        else:
            make_post(env.baseline.community1, mine, ap_id=f'{env.server_url}/post/mine').url = upload.source_url
        db.session.commit()
        gone(upload)
        assert os.path.exists(owned)


class TestWhatAWrittenPathMayRemove:
    """`file_path` and `thumbnail_path` are unlinked only inside the media folders, and only by a record that is ours:
    our upload's (`file_name`) or one a local account, community or feed owns. Until 397a91156 a peer's Image
    attachment could set `file_path` to any path, so a peer post's File never unlinks one (the disk is not proof:
    a forged path can name a real file of ours)."""

    def test_a_path_outside_the_media_folders_is_never_unlinked(self, env):
        target = env.tmp_path / 'outside.txt'
        target.write_text('do not delete me')
        relative = os.path.join('app', os.path.relpath(str(target), os.path.realpath('app')))
        inside_app = os.path.join('app', 'probe_not_media.txt')
        with open(inside_app, 'w') as handle:
            handle.write('do not delete me')
        link = os.path.join('app/static/media', 'probe_link.png')
        os.makedirs('app/static/media', exist_ok=True)
        os.symlink(str(target), link)
        try:
            for path in (str(target), relative, inside_app, 'app/static/media/../probe_not_media.txt', link):
                a_file(file_path=path, thumbnail_path=path, file_name='ours.png').delete_from_disk(purge_cdn=False)
            assert target.exists() and os.path.exists(inside_app) and os.path.lexists(link)
        finally:
            os.unlink(inside_app)
            os.unlink(link)

    @pytest.mark.parametrize('column', ['file_path', 'thumbnail_path'])
    def test_a_peer_posts_file_never_unlinks_a_path(self, env, column):
        path = a_media_file('probe_named.png')
        try:
            peer = make_user(make_instance('peer.test'), 'peerpath')
            post = make_post(env.baseline.community1, peer, ap_id='https://peer.test/post/path')
            post.image = a_file(source_url='https://peer.test/i.png', **{column: path})
            db.session.commit()
            post.image.delete_from_disk(purge_cdn=False, purge=True)
            assert os.path.exists(path)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_an_ownerless_unnamed_record_never_unlinks_a_path(self, env):
        path = a_media_file('probe_orphan.png')
        try:
            a_file(file_path=path).delete_from_disk(purge_cdn=False, purge=True)
            assert os.path.exists(path)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    @pytest.mark.parametrize('owner', ['avatar', 'post image'])
    def test_a_local_owners_record_unlinks_its_written_copy(self, env, owner):
        path = a_media_file('probe_resized.webp')
        file = a_file(source_url='https://elsewhere.example/a.png', file_path=path)
        if owner == 'avatar':
            env.baseline.user1.avatar_id = file.id
        else:
            make_post(env.baseline.community1, env.baseline.user1, ap_id=f'{env.server_url}/post/own').image_id = file.id
        db.session.commit()
        file.delete_from_disk(purge_cdn=False)
        assert not os.path.exists(path)


def test_a_files_other_records_are_found_by_trigram_index(env):
    """`_records_naming` asks `LIKE '%<name>%'` of file's three path columns and post's url and archived: a leading
    wildcard no btree serves, so on prod both were sequential scans (~400 ms a delete). pg_trgm GIN indexes serve it
    (migration f7c2a4e9b1d3). Here: they exist with gin_trgm_ops, and the planner can use them for the exact query
    the model builds (sequential scans priced out, as a tiny test table would never pick an index on cost; prod's plan
    is checked after deploy)."""
    from sqlalchemy import or_
    from sqlalchemy.dialects import postgresql
    from app.models import Post
    definitions = dict(db.session.execute(text(
        "SELECT indexname, indexdef FROM pg_indexes WHERE indexname LIKE '%\\_trgm'")).all())
    for name, column in (('ix_file_file_path_trgm', 'file_path'), ('ix_file_thumbnail_path_trgm', 'thumbnail_path'),
                         ('ix_file_source_url_trgm', 'source_url'), ('ix_post_url_trgm', 'url'),
                         ('ix_post_archived_trgm', 'archived')):
        assert 'USING gin' in definitions[name] and f'{column} gin_trgm_ops' in definitions[name], name
    name = '%abcdefghijklmno.png%'
    queries = (db.session.query(File.id).filter(File.id != 1, or_(
                   File.file_path.like(name), File.thumbnail_path.like(name), File.source_url.like(name))),
               db.session.query(Post.id).filter(or_(Post.url.like(name), Post.archived.like(name))))
    db.session.execute(text('SET LOCAL enable_seqscan = off'))
    for query in queries:
        sql = str(query.statement.compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True}))
        plan = '\n'.join(db.session.execute(text(f'EXPLAIN {sql}')).scalars())
        assert 'Bitmap Index Scan on ix_' in plan and '_trgm' in plan, plan
    db.session.rollback()
