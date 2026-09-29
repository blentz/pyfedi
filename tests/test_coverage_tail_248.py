"""Round 248: markdown that breaks the renderer, and what an upload is stored as.

Two clusters in `app/utils.py`.

`markdown_to_html` has a fallback for markdown that makes `markdown2` raise. The comment names
two real posts that did it, and the retry drops one extra (`smarty-pants` keeps, `link-patterns`
goes) before giving up and rendering NOTHING. That last arm matters: the input is a peer's post
body, so the difference between the fallback and an unhandled exception is whether one strange
post breaks the page it appears on.

`move_file_to_s3` copies a stored upload to object storage and rewrites the File row to point
at it. Three near-identical blocks -- thumbnail, file, source_url -- each of which consults
`S3_STORAGE_CLASS` and `S3_PUBLIC_ACL`. The second of those decides whether the object is
world-readable, which makes it the security-relevant line in this function, and neither had a
row.
"""
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.models import File, Site
from app.utils import markdown_to_html, move_file_to_s3
from tests.factories import make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    return SimpleNamespace(app=app, site=g.site, baseline=api_baseline)


# --------------------------------------------------------------------------
# Markdown the renderer cannot read
# --------------------------------------------------------------------------


class TestMarkdownThatBreaksTheRenderer:
    """The body comes from a peer, so `markdown2` raising is an ordinary event rather than a
    bug -- the comment in the source names two live posts that did it. There are three
    attempts: the full extras set, a reduced one, and nothing at all.
    """

    def test_ordinary_markdown_is_rendered(self, env):
        """The control for the two fallbacks below: they must not be reached by anything
        that works."""
        assert '<strong>bold</strong>' in markdown_to_html('**bold**')

    def test_the_reduced_extras_are_tried_when_the_first_pass_raises(self, env):
        """`except TypeError`. The retry keeps almost everything and drops exactly one extra,
        `markdown-in-html` and `fenced-code-blocks` -- the comment in the source names two live
        posts whose bodies made
        `markdown2` raise from inside its pygments handling. The answer is still rendered
        markdown rather than an empty string, which is what says the SECOND attempt produced
        it."""
        calls = []
        real = __import__('markdown2').Markdown

        class Fussy(real):
            def convert(self, text):
                calls.append(sorted(self.extras))
                if len(calls) == 1:
                    raise TypeError('argument after ** must be a mapping')
                return super().convert(text)

        with patch('app.utils.markdown2.Markdown', Fussy):
            html = markdown_to_html('**bold**')

        assert len(calls) == 2
        assert set(calls[0]) - set(calls[1]) == {'markdown-in-html',
                                                 'fenced-code-blocks'}
        assert '<strong>bold</strong>' in html

    def test_a_body_that_defeats_both_attempts_renders_as_nothing(self, env):
        """The bare `except: raw_html = ''`. An unrenderable body becomes empty rather than a
        500 on every page the post appears on -- so the post survives and its body does not.
        """
        real = __import__('markdown2').Markdown

        class Broken(real):
            def convert(self, text):
                raise TypeError('argument after ** must be a mapping')

        with patch('app.utils.markdown2.Markdown', Broken):
            assert markdown_to_html('**bold**') == ''

    def test_a_second_failure_of_another_kind_is_also_swallowed(self, env):
        """The inner handler is a BARE `except`, not `except TypeError` -- the retry uses a
        different extras set, so it can fail differently, and this row says that is handled
        too."""
        real = __import__('markdown2').Markdown
        calls = []

        class Awkward(real):
            def convert(self, text):
                calls.append(1)
                raise TypeError('first') if len(calls) == 1 else RuntimeError('second')

        with patch('app.utils.markdown2.Markdown', Awkward):
            assert markdown_to_html('**bold**') == ''

        assert len(calls) == 2

    def test_images_are_escaped_when_they_are_not_allowed(self, env):
        """`if not allow_img: escape_img(...)`. It runs after whichever attempt produced the
        HTML, so a body rendered by the FALLBACK is escaped too -- which is the case where
        forgetting it would matter, since that body is the strange one."""
        html = markdown_to_html('![x](https://example.com/x.png)', allow_img=False)

        assert '<img' not in html


# --------------------------------------------------------------------------
# Copying an upload to object storage
# --------------------------------------------------------------------------


class TestMovingAnUploadToObjectStorage:
    """`move_file_to_s3` uploads each of a File's three local paths and rewrites the row to
    the public URL. The rows drive one path at a time, because the three blocks are
    near-identical copies and a mistake in one is invisible while the others work.
    """

    @pytest.fixture
    def configured(self, env, monkeypatch, tmp_path):
        for key, value in (('S3_ACCESS_KEY', 'a key'),
                           ('S3_ACCESS_SECRET', 'a secret'),
                           ('S3_ENDPOINT', 'https://s3.example'),
                           ('S3_BUCKET', 'a-bucket'),
                           ('S3_PUBLIC_URL', 'cdn.example')):
            monkeypatch.setitem(env.app.config, key, value)
        monkeypatch.setitem(env.app.config, 'S3_STORAGE_CLASS', '')
        monkeypatch.setitem(env.app.config, 'S3_PUBLIC_ACL', False)
        env.media = 'app/static/media/test-s3'
        os.makedirs(env.media, exist_ok=True)
        yield env
        for name in os.listdir(env.media):
            os.unlink(os.path.join(env.media, name))
        os.rmdir(env.media)

    def _file(self, env, **paths):
        written = {}
        for field, name in paths.items():
            path = f'{env.media}/{name}'
            with open(path, 'wb') as handle:
                handle.write(b'some bytes')
            written[field] = path
        row = File(source_url='https://test.piefed.local/original.png', **written)
        db.session.add(row)
        db.session.commit()
        return row

    class _S3:
        def __init__(self):
            self.uploads = []

        def upload_file(self, local, bucket, key, ExtraArgs=None):
            self.uploads.append(SimpleNamespace(local=local, bucket=bucket, key=key,
                                                extra=ExtraArgs))

    def test_the_thumbnail_is_uploaded_and_the_row_rewritten(self, configured):
        row = self._file(configured, thumbnail_path='thumb.png')
        local = row.thumbnail_path
        s3 = self._S3()

        move_file_to_s3(row.id, s3)

        assert [upload.key for upload in s3.uploads] == ['test-s3/thumb.png']
        assert s3.uploads[0].bucket == 'a-bucket'
        db.session.refresh(row)
        assert row.thumbnail_path == 'https://cdn.example/test-s3/thumb.png'
        # The local copy is removed, which is the point of moving rather than copying.
        assert not os.path.isfile(local)

    def test_the_local_copy_of_the_file_is_removed_too(self, configured):
        """`os.unlink` in the file block, not just the thumbnail one -- the whole point is to
        move rather than copy, and leaving the bytes behind fills the disk this instance was
        trying to empty."""
        row = self._file(configured, file_path='full.png')
        local = row.file_path
        s3 = self._S3()

        move_file_to_s3(row.id, s3)

        assert not os.path.isfile(local)

    @pytest.mark.parametrize('field,name', [('thumbnail_path', 'thumb.png'),
                                            ('source_url', 'source.png')])
    def test_no_block_sends_a_storage_class_or_an_acl_by_default(self, configured, field,
                                                                name):
        """The negative case in every block. `S3_STORAGE_CLASS` is empty and `S3_PUBLIC_ACL`
        is off by default, so an instance that configured neither must not have its objects
        made world-readable by a copy of the block nobody checked."""
        path = f'{configured.media}/{name}'
        with open(path, 'wb') as handle:
            handle.write(b'some bytes')
        row = File(thumbnail_path=path,
                   source_url='https://test.piefed.local/original.png') \
            if field == 'thumbnail_path' else File(source_url=path)
        db.session.add(row)
        db.session.commit()
        s3 = self._S3()

        move_file_to_s3(row.id, s3)

        assert 'StorageClass' not in s3.uploads[0].extra
        assert 'ACL' not in s3.uploads[0].extra

    def test_the_file_itself_is_uploaded_too(self, configured):
        row = self._file(configured, file_path='full.png')
        s3 = self._S3()

        move_file_to_s3(row.id, s3)

        assert [upload.key for upload in s3.uploads] == ['test-s3/full.png']
        db.session.refresh(row)
        assert row.file_path == 'https://cdn.example/test-s3/full.png'

    def test_the_content_type_is_set_from_the_extension(self, configured):
        """`ContentType` is what the CDN serves the object as, so it is the same security
        answer `guess_mime_type` gives everywhere else."""
        row = self._file(configured, file_path='full.png')
        s3 = self._S3()

        move_file_to_s3(row.id, s3)

        assert s3.uploads[0].extra['ContentType'] == 'image/png'

    def test_a_storage_class_is_passed_only_when_configured(self, configured,
                                                           monkeypatch):
        """`if current_app.config.get('S3_STORAGE_CLASS')`. The default is empty, and passing
        `StorageClass: ''` is an error at the provider rather than a default."""
        row = self._file(configured, file_path='full.png')
        s3 = self._S3()

        move_file_to_s3(row.id, s3)
        assert 'StorageClass' not in s3.uploads[0].extra

        monkeypatch.setitem(configured.app.config, 'S3_STORAGE_CLASS', 'GLACIER')
        second = self._file(configured, file_path='another.png')
        move_file_to_s3(second.id, s3)

        assert s3.uploads[1].extra['StorageClass'] == 'GLACIER'

    def test_a_public_acl_is_attached_only_when_the_admin_asked_for_it(self, configured,
                                                                      monkeypatch):
        """The security-relevant line in this function: `ACL: public-read` makes the object
        world-readable at the provider. It is attached only when `S3_PUBLIC_ACL` is set, and
        the row asserts BOTH states, because a bucket that is already public by policy does
        not need it and one that is not must not be given it silently."""
        row = self._file(configured, file_path='full.png')
        s3 = self._S3()

        move_file_to_s3(row.id, s3)
        assert 'ACL' not in s3.uploads[0].extra

        monkeypatch.setitem(configured.app.config, 'S3_PUBLIC_ACL', True)
        second = self._file(configured, file_path='another.png')
        move_file_to_s3(second.id, s3)

        assert s3.uploads[1].extra['ACL'] == 'public-read'

    def test_the_source_url_can_itself_be_a_local_path(self, configured):
        """The third block. `File.source_url` usually holds a remote URL, but for an image
        somebody uploaded HERE it is a local path -- so it is uploaded and rewritten like the
        other two, and this row is what says the third copy of the block works."""
        path = f'{configured.media}/source.png'
        with open(path, 'wb') as handle:
            handle.write(b'some bytes')
        row = File(source_url=path)
        db.session.add(row)
        db.session.commit()
        s3 = self._S3()

        move_file_to_s3(row.id, s3)

        assert [upload.key for upload in s3.uploads] == ['test-s3/source.png']
        db.session.refresh(row)
        assert row.source_url == 'https://cdn.example/test-s3/source.png'

    @pytest.mark.parametrize('field,name', [('thumbnail_path', 'thumb.png'),
                                            ('source_url', 'source.png')])
    def test_every_block_honours_the_storage_class_and_the_acl(self, configured,
                                                              monkeypatch, field, name):
        """The same two config reads appear in all three blocks. They are asserted in each,
        because three copies is three places to forget one -- and the ACL is what makes the
        object world-readable."""
        monkeypatch.setitem(configured.app.config, 'S3_STORAGE_CLASS', 'GLACIER')
        monkeypatch.setitem(configured.app.config, 'S3_PUBLIC_ACL', True)
        path = f'{configured.media}/{name}'
        with open(path, 'wb') as handle:
            handle.write(b'some bytes')
        row = File(**{field: path}) if field != 'thumbnail_path' else File(
            thumbnail_path=path, source_url='https://test.piefed.local/original.png')
        db.session.add(row)
        db.session.commit()
        s3 = self._S3()

        move_file_to_s3(row.id, s3)

        assert s3.uploads[0].extra['StorageClass'] == 'GLACIER'
        assert s3.uploads[0].extra['ACL'] == 'public-read'

    def test_a_path_that_is_already_a_url_is_left_alone(self, configured):
        """`not file.file_path.startswith('http')`. The function is called more than once
        over a File's life, and a second call must not try to upload a CDN URL as a local
        path.

        Dropping that half alone is an EQUIVALENT MUTANT: a CDN URL does not start with
        `app/static/media` either, so the second half of the `and` refuses it anyway. The
        `http` test is what makes the intent readable, and it would matter if the media
        prefix ever moved.
        """
        row = File(file_path='https://cdn.example/already/there.png',
                   source_url='https://test.piefed.local/original.png')
        db.session.add(row)
        db.session.commit()
        s3 = self._S3()

        move_file_to_s3(row.id, s3)

        assert s3.uploads == []
        db.session.refresh(row)
        assert row.file_path == 'https://cdn.example/already/there.png'

    def test_a_path_outside_the_media_directory_is_left_alone(self, configured):
        """`startswith('app/static/media')`. The path comes from a File row, and uploading
        anything else -- a template, a config file -- would publish it."""
        row = File(file_path='app/static/images/logo.png',
                   source_url='https://test.piefed.local/original.png')
        db.session.add(row)
        db.session.commit()
        s3 = self._S3()

        move_file_to_s3(row.id, s3)

        assert s3.uploads == []

    def test_a_row_whose_bytes_are_gone_is_left_alone(self, configured):
        """`if os.path.isfile(...)`. `delete_from_disk` leaves the row, so a File pointing at
        a deleted path is an ordinary state, and `upload_file` on a missing path would
        raise."""
        row = self._file(configured, file_path='full.png')
        os.unlink(row.file_path)
        s3 = self._S3()

        move_file_to_s3(row.id, s3)

        assert s3.uploads == []

    def test_nothing_happens_when_s3_is_not_configured(self, env, monkeypatch):
        """`if store_files_in_s3()`. Most instances store locally, and this function is
        called on every upload -- so the whole body is behind that test."""
        monkeypatch.setitem(env.app.config, 'S3_ACCESS_KEY', '')
        os.makedirs('app/static/media/test-s3-off', exist_ok=True)
        path = 'app/static/media/test-s3-off/whatever.png'
        # The bytes have to EXIST, or the `os.path.isfile` guard inside answers for the
        # `store_files_in_s3()` one and this row proves nothing.
        with open(path, 'wb') as handle:
            handle.write(b'some bytes')
        row = File(file_path=path,
                   source_url='https://test.piefed.local/original.png')
        db.session.add(row)
        db.session.commit()
        s3 = self._S3()

        try:
            move_file_to_s3(row.id, s3)

            assert s3.uploads == []
            assert os.path.isfile(path)
        finally:
            if os.path.isfile(path):
                os.unlink(path)
            os.rmdir('app/static/media/test-s3-off')

    def test_a_file_id_nobody_holds_is_harmless(self, configured):
        """`if file:`. The id arrives from a celery task queued earlier, so the row may have
        been deleted in between."""
        s3 = self._S3()

        move_file_to_s3(999999, s3)

        assert s3.uploads == []
