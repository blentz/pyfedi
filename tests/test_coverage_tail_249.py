"""Round 249: what happens to an image a peer sent us.

`make_image_sizes_async` downloads the image behind a federated post, resizes it, and on the
way does two things that are not about resizing at all:

    1911            reads the C2PA manifest and sets `Post.ai_generated` for every post using
                    this image
    2113-2149       when the community is marked toxic and the instance has the chan filter
                    on, runs OCR over the image and notifies user 1 if the text looks like a
                    4chan screenshot

Neither had a row. Both are moderation decisions taken automatically about somebody else's
content: the first puts a label on a post, the second raises a report to the site's first
admin. The OCR arm is also the federated sibling of D1415 -- the same filter in
`app/community/forms.py` raised `TesseractNotFoundError` out of form validation because the
handler was too narrow. Here the handler is `except Exception`, which is what this round
asserts.
"""
import io
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.constants import NOTIF_REPORT
from app.models import File, Notification, Post, Site
from tests.factories import make_community, make_community_member, make_post, make_user


def a_png(size=(8, 8)):
    """A real PNG, because Pillow opens this for real -- the resize is not the thing under
    test but it has to work for the lines after it to run."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new('RGB', size, (10, 20, 30)).save(buffer, format='PNG')
    return buffer.getvalue()


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.enable_chan_image_filter = False
    g.site = site
    db.session.commit()
    return SimpleNamespace(app=app, site=site, baseline=api_baseline)


@pytest.fixture
def seeded(env):
    community = make_community('imageland')
    author = make_user(env.baseline.instance_local, 'imageauthor', local=True)
    admin = env.baseline.user1
    db.session.commit()
    make_community_member(author, community)
    image = File(source_url='https://peer.example/meme.png')
    db.session.add(image)
    db.session.commit()
    post = make_post(community, author, ap_id='https://peer.example/p/1',
                     title='a post with a picture')
    post.image_id = image.id
    db.session.commit()
    env.community = community
    env.author = author
    env.admin = admin
    env.image = image
    env.post = post
    return env


def _response(content, content_type='image/png', status=200):
    return SimpleNamespace(status_code=status,
                           headers={'content-type': content_type},
                           content=content,
                           close=lambda: None)


def _run(env, *, content=None, content_type='image/png', directory='posts',
         toxic=False, c2pa=None, ocr=None, ocr_error=None):
    """Drive the task with the download, the C2PA read and the OCR replaced.

    Everything between them -- Pillow, the file writes, the thumbnailing -- runs for real,
    which is why the written files are cleaned up afterwards.
    """
    from app.activitypub import util

    created = []
    ocr_calls = []

    def fake_ocr(image, *args, **kwargs):
        ocr_calls.append(image)
        if ocr_error is not None:
            raise ocr_error
        return ocr if ocr is not None else ''

    with patch.object(util, 'get_request',
                      return_value=_response(content if content is not None else a_png(),
                                             content_type=content_type)), \
            patch.object(util, 'inspect_image_c2pa',
                         return_value=c2pa if c2pa is not None
                         else {'c2pa': {'present': False, 'ai_generated': False,
                                        'creator': None, 'software': None}}), \
            patch.object(util.pytesseract, 'image_to_string', fake_ocr):
        util.make_image_sizes_async(env.image.id, 50, 120, directory, toxic)

    db.session.expire_all()
    return SimpleNamespace(ocr_calls=ocr_calls, created=created)


@pytest.fixture(autouse=True)
def clean_media():
    """The task writes real files under `app/static/media/<directory>/xx/yy/`. They are
    named with `gibberish(15)`, so the cleanup is by directory rather than by name."""
    before = set()
    for root in ('app/static/media/posts', 'app/static/media/probes'):
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                before.add(os.path.join(dirpath, name))
    yield
    for root in ('app/static/media/posts', 'app/static/media/probes'):
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                path = os.path.join(dirpath, name)
                if path not in before:
                    os.unlink(path)


# --------------------------------------------------------------------------
# The AI label
# --------------------------------------------------------------------------


class TestLabellingAnImageAsAiGenerated:
    """The C2PA read happens only for `directory == 'posts'`, and its answer is written with
    one UPDATE against every post using this image -- so the label follows the IMAGE, not the
    post that happened to be processed.
    """

    AI = {'c2pa': {'present': True, 'ai_generated': True, 'creator': 'Some Model',
                   'software': None}}
    NOT_AI = {'c2pa': {'present': True, 'ai_generated': False, 'creator': 'A Camera',
                       'software': None}}

    def test_a_generated_image_labels_its_post(self, seeded):
        _run(seeded, c2pa=self.AI)

        assert db.session.get(Post, seeded.post.id).ai_generated is True

    def test_a_photograph_does_not(self, seeded):
        """The expensive direction: a label on somebody's own photograph."""
        _run(seeded, c2pa=self.NOT_AI)

        assert db.session.get(Post, seeded.post.id).ai_generated is False

    def test_every_post_using_the_image_is_labelled(self, seeded):
        """The UPDATE is `WHERE image_id = :file_id`, not by post id -- a cross-posted image
        appears on several posts and the claim is about the image."""
        other = make_post(seeded.community, seeded.author,
                          ap_id='https://peer.example/p/2', title='the same picture again')
        other.image_id = seeded.image.id
        db.session.commit()

        _run(seeded, c2pa=self.AI)

        assert db.session.get(Post, other.id).ai_generated is True

    def test_an_avatar_is_not_inspected(self, seeded):
        """`if directory == 'posts'`. The same task resizes avatars and community icons, and
        an AI-generated avatar is not a post to label."""
        _run(seeded, c2pa=self.AI, directory='probes')

        assert db.session.get(Post, seeded.post.id).ai_generated is False


# --------------------------------------------------------------------------
# The chan image filter
# --------------------------------------------------------------------------


class TestTheChanImageFilter:
    """Three conditions have to hold before OCR runs: the site has the filter on, the
    community is marked toxic, and `ALLOW_4CHAN` is unset in the environment. Then the text
    has to look like a 4chan screenshot -- `Anonymous` plus `No.` -- before anybody is
    notified.
    """

    SCREENSHOT = 'Anonymous 01/02/26(Fri)12:00:00 No.123456789'

    @pytest.fixture
    def filtered(self, seeded, monkeypatch):
        seeded.site.enable_chan_image_filter = True
        db.session.commit()
        monkeypatch.delenv('ALLOW_4CHAN', raising=False)
        return seeded

    def _notifications(self):
        return Notification.query.filter_by(
            subtype='post_with_suspicious_image').all()

    def test_a_screenshot_in_a_toxic_community_is_reported(self, filtered):
        _run(filtered, toxic=True, ocr=self.SCREENSHOT)

        notification = self._notifications()[0]
        assert notification.user_id == 1
        assert notification.notif_type == NOTIF_REPORT
        assert notification.author_id == filtered.post.user_id
        assert notification.url == filtered.post.slug

    def test_the_notification_names_the_post_and_its_author(self, filtered):
        """D1391's shape: the template reads `orig_post_title`, `orig_post_body` and
        `suspect_user_user_name`, and a key no producer writes renders as blank rather than
        raising -- so the Author line was empty on every one of these until the dict was
        fixed."""
        _run(filtered, toxic=True, ocr=self.SCREENSHOT)

        targets = self._notifications()[0].targets
        assert targets['post_id'] == filtered.post.id
        assert targets['orig_post_title'] == 'a post with a picture'
        assert targets['suspect_user_user_name'] == filtered.author.user_name

    def test_text_that_is_not_a_screenshot_is_not_reported(self, filtered):
        """Both halves of `'Anonymous' in text and ('No.' in text or ...)`. A poster on any
        imageboard-shaped page is not what this looks for; the pair is."""
        _run(filtered, toxic=True, ocr='Anonymous donations welcome')

        assert self._notifications() == []

    def test_a_community_that_is_not_toxic_is_not_scanned(self, filtered):
        """`toxic_community` is passed by the caller from the community's own flag. OCR is
        expensive, and running it over every federated image would be a per-image cost on
        every instance."""
        result = _run(filtered, toxic=False, ocr=self.SCREENSHOT)

        assert result.ocr_calls == []
        assert self._notifications() == []

    def test_the_filter_can_be_switched_off_by_the_site(self, seeded):
        """`site.enable_chan_image_filter` is off by default, so the whole block is opt-in."""
        result = _run(seeded, toxic=True, ocr=self.SCREENSHOT)

        assert result.ocr_calls == []
        assert self._notifications() == []

    def test_an_environment_opt_out_skips_the_scan(self, filtered, monkeypatch):
        """`if os.environ.get('ALLOW_4CHAN', None) is None`. An operator who wants that
        content sets the variable, and then no OCR runs at all -- which is also the switch
        that makes the tesseract dependency optional."""
        monkeypatch.setenv('ALLOW_4CHAN', '1')

        result = _run(filtered, toxic=True, ocr=self.SCREENSHOT)

        assert result.ocr_calls == []
        assert self._notifications() == []

    def test_a_tesseract_failure_is_swallowed(self, filtered):
        """`except Exception: image_text = ''`. This is the federated sibling of D1415, where
        the same filter in `app/community/forms.py` caught only `FileNotFoundError` and let
        `TesseractNotFoundError` out of form validation. Here the handler is wide, so an
        instance with the filter on and tesseract missing keeps ingesting images -- it just
        does not scan them.
        """
        import pytesseract

        result = _run(filtered, toxic=True,
                      ocr_error=pytesseract.TesseractNotFoundError())

        assert result.ocr_calls != []
        assert self._notifications() == []
        # The image was still processed: the row's width was written.
        assert db.session.get(File, filtered.image.id).width is not None


# --------------------------------------------------------------------------
# What the task refuses to process
# --------------------------------------------------------------------------


class TestWhatTheTaskWillNotProcess:

    def test_a_gif_is_left_alone(self, seeded):
        """Resizing a gif breaks its animation, so the task returns before downloading
        anything."""
        seeded.image.source_url = 'https://peer.example/meme.gif'
        db.session.commit()

        from app.activitypub import util

        with patch.object(util, 'get_request') as fetch:
            util.make_image_sizes_async(seeded.image.id, 50, 120, 'posts', False)

        assert fetch.call_count == 0

    def test_a_non_image_content_type_is_not_resized(self, seeded):
        """The content type is the peer's claim about what it served. Only `image/*` -- plus
        `application/octet-stream` for an `.avif` url -- is opened by Pillow."""
        _run(seeded, content=b'<html>not an image</html>', content_type='text/html')

        assert db.session.get(File, seeded.image.id).width is None

    def test_a_file_id_nobody_holds_is_harmless(self, seeded):
        """The task is queued with an id and runs up to ten seconds later, so the row may be
        gone by then."""
        from app.activitypub import util

        with patch.object(util, 'get_request') as fetch:
            util.make_image_sizes_async(999999, 50, 120, 'posts', False)

        assert fetch.call_count == 0


class TestTheWidthCutoffAndTheMissingSite:

    SCREENSHOT = 'Anonymous 01/02/26(Fri)12:00:00 No.123456789'

    @pytest.fixture
    def filtered(self, seeded, monkeypatch):
        seeded.site.enable_chan_image_filter = True
        db.session.commit()
        monkeypatch.delenv('ALLOW_4CHAN', raising=False)
        return seeded

    def test_a_large_image_is_not_scanned(self, filtered):
        """`img_width < 2000`, with the source's own reason: images wider than 2000px tend to
        be photographs rather than screenshots. It is a cost decision as much as an accuracy
        one -- OCR over a large image is the expensive case."""
        result = _run(filtered, content=a_png(size=(2200, 40)), toxic=True,
                      ocr=self.SCREENSHOT)

        assert result.ocr_calls == []
        assert Notification.query.filter_by(
            subtype='post_with_suspicious_image').all() == []

    def test_an_image_just_under_the_cutoff_is_scanned(self, filtered):
        """The other side of the same comparison, so a mutant moving the boundary is
        visible."""
        result = _run(filtered, content=a_png(size=(1999, 40)), toxic=True,
                      ocr=self.SCREENSHOT)

        assert result.ocr_calls != []
        assert Notification.query.filter_by(
            subtype='post_with_suspicious_image').all() != []

    def test_an_instance_with_no_site_row_does_not_scan(self, filtered):
        """`if site is None: site = Site()` -- an unsaved Site has `enable_chan_image_filter`
        unset, so the filter is off. The row exists because the alternative is
        `AttributeError` on None inside a celery task, where nothing would see it, and
        because the task runs in its OWN session: a Site row this session holds is not
        necessarily one that session can see.
        """
        from app.activitypub import util

        real_get = None

        def fake_get(model, ident):
            if model is Site:
                return None
            return real_get(model, ident)

        with patch.object(util, 'get_request',
                          return_value=_response(a_png())), \
                patch.object(util, 'inspect_image_c2pa',
                             return_value={'c2pa': {'present': False,
                                                    'ai_generated': False,
                                                    'creator': None, 'software': None}}), \
                patch.object(util.pytesseract, 'image_to_string',
                             return_value=self.SCREENSHOT) as ocr:
            session = util.get_task_session()
            real_get = session.get
            with patch.object(session.__class__, 'get', side_effect=fake_get,
                              autospec=False):
                with patch.object(util, 'get_task_session', return_value=session):
                    util.make_image_sizes_async(filtered.image.id, 50, 120, 'posts', True)

        assert ocr.call_count == 0
