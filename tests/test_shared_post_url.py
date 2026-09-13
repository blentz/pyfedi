"""`edit_post`'s two URL-type dispatches and the tail that closes the module.

SCOPE. Sub-project 39, the last 41 statements and 35 branch arcs in
`app/shared/post.py`. Every one of them is in `edit_post`; every other
function in the module is at zero. Three regions:

  - `:387-460` -- the permission compound, the type dispatch on `post.url`
    (the EXISTING url), the scheduled-post gate, and the old-file teardown.
  - `:565-661` -- the type dispatch on `url` (the NEWLY SUBMITTED one), which
    additionally builds thumbnails and `File` rows.
  - `:662-703` -- five arms of the poll and event tail whose lines all run.

ENTRY is a direct call. `edit_post` opens with `if not user:` on both
branches (`:252` SRC_API, `:316` SRC_WEB), so passing `user=` skips
`authorise_api_user` and `current_user` alike -- no request context, no
login, no token. Every test here does that.

`from_scratch=True` SWITCHES OFF `:421-459`. That block holds the
notification cleanup, the poll-vote deletes, the teardown at `:435-451`, the
tag clear and `:459`'s commit. Every test here passes `from_scratch=True`
EXCEPT the teardown tests, which are the ones aimed at that block.

THE HEAD REQUEST, AND WHY THIS FILE INVERTS THE UPLOAD FILE'S RULE.
`is_image_url` (app/utils.py:247) issues an httpx HEAD through
`mime_type_using_head`, and `edit_post` can call it twice -- `:410` on
`post.url`, `:601` on `url`. `tests/README.md` fact 229 point 3 records the
rule `tests/test_shared_post_upload.py` follows: a HEAD reporting
`image/png` and NO GET route, because an image content type makes `:601`
true and `:601` is the only one of the four arms in the
`if`/`elif`/`elif`/`else` chain at `:601`/`:619`/`:630`/`:641` that does not
call `opengraph_parse`.

THIS FILE NEEDS THE OPPOSITE on the arms it targets. A HEAD reporting
`text/html` makes `is_image_url` false (`.html` is not in
`common_image_extensions`, app/utils.py:248-249), control reaches `:619`
onward, `opengraph_parse` runs, and a GET route becomes REQUIRED rather than
forbidden. Both conventions appear in this file; they are chosen per test,
never by a module-level fixture.

THE INVERSION IS MEASURED, NOT REASONED. Sub-project 39 Task 1 ran it
directly under `app.app_context()` in this container, with the same
`respx.mock(assert_all_called=True)` router `http_mock` uses:

    text/html  -> False
    image/png  -> True

so the whole of this file's Region B strategy rests on an observation
rather than on a reading of `app/utils.py:271-273`.

`mime_type_using_head` IS `@cache.memoize`-DECORATED (app/utils.py:332), so
two tests that HEAD the SAME url with DIFFERENT content types would be a
cross-test leak under a real cache -- the second test's route would never be
fetched and `assert_all_called=True` would fail it at teardown. It is safe
here only because `TestConfig` sets `CACHE_TYPE = 'NullCache'`
(tests/conftest.py:68), which makes the memoization a no-op. The pair
`test_an_existing_image_url_retypes_the_post_as_image` /
`test_an_existing_url_that_is_neither_video_nor_image_leaves_the_type_alone`
deliberately shares one url and differs only in the content type, and that
pair is what would break first if the cache type ever changed.

`http_mock` is `assert_all_called=True` (tests/conftest.py:342), so a
registered route that is never reached fails the test at teardown, and
respx's unmatched-request error is neither `httpx.HTTPError` nor
`httpx.InvalidURL` -- it escapes `app/utils.py:345`'s handler rather than
being swallowed as `''`. Count your HEADs and your GETs.

`fixup_url` (app/utils.py:3311) RETURNS `(url, url)` for every url here. It
diverges only for YouTube domains and for peertube urls whose last 25
characters begin `/w/` (`:3330`). So `thumbnail_url == embed_url == url`,
which is why the GET for `opengraph_parse` is registered on the submitted
url itself -- and why `post.url` alone cannot tell `:640` (`post.url = url`)
apart from `:652` (`post.url = embed_url`). See TestLoopsArm for what does.
"""

import linecache
import warnings
from datetime import datetime, timedelta
from io import BytesIO

import httpx
from PIL import Image
from sqlalchemy import text

from app import db
from app.constants import (
    POST_STATUS_SCHEDULED, POST_TYPE_ARTICLE, POST_TYPE_EVENT,
    POST_TYPE_IMAGE, POST_TYPE_LINK, POST_TYPE_POLL, POST_TYPE_VIDEO,
    ROLE_ADMIN, SRC_API, SRC_WEB,
)
from app.models import (
    Domain, Event, File, Poll, PollChoice, PollChoiceVote, Role,
)
from app.shared.post import edit_post
from app.utils import store_files_in_s3
from tests.factories import make_community_member, make_poll_choice, make_user
from tests.test_shared_post_edit import _api_input, _make_admin, _seed, _web_form
from tests.test_shared_post_upload import chdir_upload  # noqa: F401


def _opengraph_page(http_mock, url, **tags):
    """Serve `url` as an HTML page carrying `tags` as opengraph <meta> elements.

    `:621`/`:632`/`:642`'s `opengraph_parse` (app/utils.py:2998-3007)
    delegates to `parse_page` (app/utils.py:3165-3225), which GETs the page
    and requires BOTH a 200 (app/utils.py:3195-3196) and 'text/html' in the
    Content-Type (app/utils.py:3198-3199) before it parses; either missing
    makes it return False. The header is load-bearing, not decoration.

    A keyword cannot carry a ':', so each tag is named with '_' and
    translated: `og_image_url=...` becomes `<meta property="og:image:url" ...>`.

    With NO tags this still returns a page `parse_page` reads successfully and
    finds nothing in -- an EMPTY dict, which is falsy. That is a different
    input from `_unreadable_page` below, which returns False, and the two are
    not interchangeable for the mutation pass.
    """
    meta = ''.join(f'<meta property="{name.replace("_", ":")}" content="{value}">'
                   for name, value in tags.items())
    http_mock.get(url).respond(200, headers={'Content-Type': 'text/html'},
                               text=f'<html><head>{meta}</head><body></body></html>')


def _unreadable_page(http_mock, url):
    """Serve `url` as a 404, so `parse_page` returns False at
    app/utils.py:3195-3196 and `:622`/`:633`/`:643`'s leading `if opengraph`
    is False.

    False rather than None, and the difference matters to Task 9's mutation
    pass: forcing the first conjunct true reaches `False.get('og:image', '')`
    and raises AttributeError, which is a crash-kill and not an
    assertion-kill.
    """
    http_mock.get(url).respond(404)


def _make_is_admin(user):
    """Make `user` satisfy `User.is_admin()` (app/models.py:1259-1265).

    `_make_admin` (tests/test_shared_post_edit.py:219-241) IS NOT ENOUGH ON ITS
    OWN, and finding that out is part of Task 2's job. That helper exists for
    `Site.admins()`, whose filter is `user_role.c.role_id == ROLE_ADMIN`
    (app/models.py:3999-4000), so all it has to get right is the Role row's
    ID; it names the row `f'role-{ROLE_ADMIN}'`, i.e. 'role-4'.
    `User.is_admin()` consults neither `Site.admins()` nor the role id:

        if self.id == 1:
            return True
        for role in self.roles:
            if role.name == 'Admin':
                return True
        return False

    -- so it wants the NAME. Measured in this container on a user whose id is
    not 1: after `_make_admin(u)`, `[r.name for r in u.roles]` is `['role-4']`
    and `u.is_admin()` is False. This delegates to `_make_admin` for the row
    and the association, then supplies the one thing it does not.
    `tests/test_shared_post_lifecycle.py:143` builds the same combined shape
    (`Role(id=ROLE_ADMIN, name='Admin', weight=0)`) in one step.
    """
    _make_admin(user)
    db.session.get(Role, ROLE_ADMIN).name = 'Admin'
    db.session.commit()


class TestExistingUrlTypeDispatch:
    """`:403-411` -- the type dispatch on `post.url`, the url the post ALREADY
    has, tested before `:565` ever looks at the submitted one.

    Every test here passes `url=None` in the input, so `:565`'s
    `if url and (from_scratch or url_changed):` and `:660`'s `elif url and ...`
    are both false and the whole tail from `:565` is skipped. That leaves
    `:398`'s `post.type = type` and this block as the ONLY writers of
    `post.type`, which is what makes `post.type` a witness here at all.
    """

    def test_an_existing_pixelfed_url_retypes_the_post_as_image(self, db_session):
        """`:404` true -> `:405`. Arc 404->405, statement 405.

        No `http_mock`: `:404`'s match short-circuits the elif chain before
        `:408`'s `is_video_url` (pure) and `:410`'s `is_image_url` (which would
        issue a HEAD), so this path makes no outbound request at all.

        `type=POST_TYPE_ARTICLE` is passed so `:398` writes ARTICLE and only
        `:405` can produce IMAGE.
        """
        s = _seed(url='https://pixelfed.social/p/someone/1')

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_IMAGE

    def test_an_existing_loops_url_retypes_the_post_as_video(self, db_session):
        """`:404` false, `:406` true -> `:407`. Arc 406->407, statement 407.

        `loops.video` is neither pixelfed host, so `:404` is false; `:406`'s
        `startswith('https://loops.video/')` is what decides. The path has no
        video EXTENSION, so `:408`'s `is_video_url` would be false and `:409`
        cannot be the line that produces VIDEO -- which is what keeps this
        test distinct from the one below (false-witness mechanism 4).
        """
        s = _seed(url='https://loops.video/v/abc123')

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_VIDEO

    def test_an_existing_video_extension_retypes_the_post_as_video(self, db_session):
        """`:408`'s `is_video_url(post.url)` true -> `:409`. Arc 408->409,
        statement 409.

        Still no `http_mock`: `is_video_url` (app/utils.py:294) is pure
        urlparse plus an extension test and issues no request. The url is
        deliberately NOT a pixelfed or loops host, so `:404` and `:406` are
        both false and `:408` is the line that decides.
        """
        s = _seed(url='https://example.com/clip.mp4')

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_VIDEO

    def test_an_existing_image_url_retypes_the_post_as_image(self, db_session, http_mock):
        """`:410`'s `is_image_url(post.url)` true -> `:411`.

        THE POSITIVE CONTROL for the test below, and the reason both exist.
        The false-arm test can only assert that `post.type` is still what
        `:398` wrote, which is also what a broken `:410` that never ran would
        leave -- false-witness mechanism 1. This test differs from it in ONE
        byte of the HEAD's Content-Type and produces a different type, so the
        pair proves `:410` is actually deciding.
        """
        http_mock.head('https://example.com/page.html').respond(
            200, headers={'Content-Type': 'image/png'})
        s = _seed(url='https://example.com/page.html')

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_IMAGE

    def test_an_existing_url_that_is_neither_video_nor_image_leaves_the_type_alone(
            self, db_session, http_mock):
        """`:410` false -> `:413`. Arc 410->413.

        Identical to the test above except the HEAD reports `text/html`
        instead of `image/png`, so `is_image_url` returns False at
        app/utils.py:273 (`.html` is not in `common_image_extensions`) and no
        arm of `:403-411` fires.

        `http_mock`'s `assert_all_called=True` is doing real work here on top
        of the type assertion: it proves the HEAD was actually ISSUED, so
        `:410` was reached and evaluated rather than skipped -- which is the
        part a bare "type is still ARTICLE" assertion could not tell apart
        from the elif chain never running.
        """
        http_mock.head('https://example.com/page.html').respond(
            200, headers={'Content-Type': 'text/html'})
        s = _seed(url='https://example.com/page.html')

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_ARTICLE


class TestStickyPermission:
    """`:387`'s three-disjunct compound and the `post.sticky` write it guards.

    THE WITNESS IS A SEEDED `True`. `:388` is
    `post.sticky = False if src == SRC_API else input.sticky.data`, so under
    SRC_API the permitted path always writes False -- and False is also what a
    freshly made post already carries (measured: `_seed().post.sticky` is
    `False`). Asserting `post.sticky is False` on a default post witnesses
    nothing at all (false-witness mechanism 1). Every test here seeds
    `post.sticky = True` first, so the permitted arm CHANGES it and the refused
    arm LEAVES it.

    NO TEST HERE MAY USE `_seed()`'s OWN USER, and this is a correction to the
    brief rather than a stylistic choice. `User.is_admin()`
    (app/models.py:1259-1265) returns True unconditionally for `self.id == 1`,
    and `_seed()`'s user IS id 1 -- `make_community` hardcodes `user_id=1` and
    the db_session teardown resets every sequence, so the first `make_user` in
    a test gets id 1 (tests/test_shared_post_edit.py:196-199). Measured in this
    container: `_seed().user.id` is 1 and `_seed().user.is_admin()` is True
    BEFORE anything is done to it. A moderator test written on that user would
    move the first and third disjuncts together and could not tell a swap
    between them apart -- false-witness mechanism 5. Each test below therefore
    builds its own second user (id 2, `is_admin()` False, measured) and asserts
    the operands it is NOT exercising are false, inline.

    ONLY TWO OF THE THREE OPERANDS CAN BE WITNESSED ALONE. `moderators()`
    (app/models.py:716-722) admits a `CommunityMember` row on
    `is_owner OR is_moderator`, and `is_moderator(user)` (`:740`) then tests
    only `user_id` over that list -- so a row with `is_owner=True,
    is_moderator=False` makes BOTH predicates true, and the second disjunct can
    never be the one that decides. Task 2 verified this by construction, not by
    reading: on such a row `is_owner` -> True and `is_moderator` -> True.
    `test_an_owner_may_set_sticky` documents the subsumption rather than
    isolating the operand, because isolating it is impossible.
    """

    def _seed_sticky(self):
        s = _seed()
        s.post.sticky = True
        db.session.commit()
        return s

    def test_a_plain_member_may_not_set_sticky(self, db_session):
        """All three disjuncts false -> `:389`. Arc 387->389.

        The user is a second, unrelated account. It has to be: `_seed()`'s own
        user is id 1, for which `User.is_admin()` short-circuits to True at
        app/models.py:1260, so the third disjunct would be true and `:387`
        would take its TRUE arm -- the exact opposite of what this test wants.
        The second user is id 2 and carries no `CommunityMember` row, so
        `moderators()` is empty and all three operands are false.
        """
        s = self._seed_sticky()
        outsider = make_user(s.instance, 'outsider', local=True)

        assert s.community.is_moderator(outsider) is False
        assert s.community.is_owner(outsider) is False
        assert outsider.is_admin() is False

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=outsider, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.sticky is True

    def test_a_moderator_may_set_sticky(self, db_session):
        """First disjunct alone: `is_moderator=True, is_owner=False`, so
        `is_moderator(user)` is true and `is_owner(user)` is false. This is the
        one operand that CAN be isolated.

        The two inline assertions are what make the isolation real: the
        moderator is a second user so `is_admin()` is False, which keeps the
        third disjunct out of lockstep with the first.
        """
        s = self._seed_sticky()
        mod = make_user(s.instance, 'mod', local=True)
        make_community_member(mod, s.community, is_moderator=True)

        assert s.community.is_owner(mod) is False
        assert mod.is_admin() is False

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=mod, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.sticky is False

    def test_an_owner_may_set_sticky(self, db_session):
        """Second disjunct, WHICH CANNOT BE ISOLATED -- an owner row satisfies
        `is_moderator` too. The inline assertion records that, so a reader does
        not mistake this for an isolating witness.

        The third disjunct IS isolated out, as everywhere in this class: the
        owner is a second user, so `is_admin()` is False.
        """
        s = self._seed_sticky()
        owner = make_user(s.instance, 'owner', local=True)
        member = make_community_member(owner, s.community, is_moderator=False)
        member.is_owner = True
        db.session.commit()

        assert s.community.is_moderator(owner) is True  # the subsumption
        assert owner.is_admin() is False

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=owner, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.sticky is False

    def test_an_admin_may_set_sticky(self, db_session):
        """Third disjunct alone: no `CommunityMember` row exists, so the first
        two are false and `user.is_admin()` is the only thing that can be true.

        The admin is a SECOND user, made admin through `_make_is_admin`'s named
        Role, deliberately and not for symmetry: on `_seed()`'s id-1 user
        `is_admin()` is already True and no helper would be exercised at all,
        so the test would witness the id short-circuit at app/models.py:1260
        rather than anything a test set up. Here `is_admin()` is False until
        the Role named 'Admin' is attached, which is what the first two
        assertions and the helper's own docstring pin down.
        """
        s = self._seed_sticky()
        admin = make_user(s.instance, 'anadmin', local=True)
        _make_is_admin(admin)

        assert s.community.is_moderator(admin) is False
        assert s.community.is_owner(admin) is False
        assert admin.is_admin() is True

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=admin, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.sticky is False


class TestScheduledGate:
    """`:413-416` -- the scheduled-post gate.

    MUST USE THE WEB BRANCH. `:281` is a bare `scheduled_for = None` on the
    SRC_API branch, so `:413`'s `if scheduled_for:` can never be true through
    `_api_input`; only `:337`'s `scheduled_for = input.scheduled_for.data`
    (SRC_WEB) can make it truthy. A scheduled test written against the API
    branch would take `:413`'s false arm and witness nothing -- false-witness
    mechanism 4.

    `:414` reads `post.timezone`, which `:401` has just written from
    `input.timezone.data`; `_web_form` defaults it to 'UTC'.

    WHICH URL FIELD, AND WHICH HTTP ROUTES. `:320-321` is
    `if type == POST_TYPE_LINK: url = input.link_url.data.strip()` -- for
    POST_TYPE_LINK the SRC_WEB branch reads `link_url`, never `video_url`
    (`video_url` is `:323`, POST_TYPE_VIDEO only). `_web_form`'s `link_url`
    default is 'https://example.com/page'
    (tests/test_shared_post_edit.py:152), so that is the one url these tests
    register, and POST_TYPE_LINK is what is passed so `:321` is the line that
    reads it.

    Two routes per test, not one. `_seed()` leaves `post.url` None, so `:403`
    is false and the FIRST `is_image_url` never runs; but `url` is truthy and
    `from_scratch=True`, so `:565` opens and `:601`'s `is_image_url(url)`
    issues a HEAD. The HEAD answers `text/html`, which makes `is_image_url`
    False and drops control into `:641`'s generic arm, whose `opengraph_parse`
    needs a GET -- served by `_unreadable_page`. The alternative (a HEAD of
    `image/png` and no GET, fact 229's convention) was rejected: it takes
    `:601`'s true arm, which calls `make_image_sizes` and would drag the
    `chdir_upload` fixture and a real image payload into a class that is about
    the scheduled gate and nothing else. `http_mock`'s `assert_all_called=True`
    means both routes are proven to have been fetched.
    """

    def test_a_future_schedule_marks_the_post_scheduled(self, db_session, http_mock):
        """`:415` true -> `:416`. THE POSITIVE CONTROL for the test below:
        without it, `post.status` merely still holding its seeded value proves
        nothing about whether `:415` ran."""
        http_mock.head('https://example.com/page').respond(
            200, headers={'Content-Type': 'text/html'})
        _unreadable_page(http_mock, 'https://example.com/page')
        s = _seed()
        future = datetime.utcnow() + timedelta(days=2)

        edit_post(_web_form(scheduled_for=future), s.post, POST_TYPE_LINK,
                  SRC_WEB, user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.status == POST_STATUS_SCHEDULED

    def test_a_schedule_already_in_the_past_does_not_mark_the_post_scheduled(
            self, db_session, http_mock):
        """`:415` false -> `:418`. Arc 415->418.

        Differs from the control above in the DATE alone.
        """
        http_mock.head('https://example.com/page').respond(
            200, headers={'Content-Type': 'text/html'})
        _unreadable_page(http_mock, 'https://example.com/page')
        s = _seed()
        past = datetime.utcnow() - timedelta(days=2)

        edit_post(_web_form(scheduled_for=past), s.post, POST_TYPE_LINK,
                  SRC_WEB, user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.status != POST_STATUS_SCHEDULED


class TestOldImageTeardown:
    """`:435-441` -- removing the post's old `File` when the url changes.

    `from_scratch=False` on every test here: the block lives under `:421`'s
    `if not from_scratch:`. The gate above it, `:435`'s
    `if url != post.url or uploaded_file:`, is opened by submitting `url=None`
    against a post that HAS a url.

    `File.delete_from_disk` (app/models.py:421-469) is what `:440` calls, and
    what it removes decides the witness. Its `file_path` arm is::

        424     if self.file_path:
        425         if self.file_path.startswith(f'https://{...S3_PUBLIC_URL...}') and _store_files_in_s3():
        ...
        429         elif os.path.isfile(self.file_path):
        430             try:
        431                 os.unlink(self.file_path)

    so with S3 unconfigured (`.env.test` sets no S3_* variables, so
    `config.py:104-108` leaves them all `''`) a RELATIVE `file_path` is
    resolved against the working directory and unlinked. `chdir_upload` makes
    that working directory pytest's `tmp_path`, so the unlink happens inside
    the fixture and never in the repository.

    The `source_url` arm (`:449-459`) is deliberately inert on these rows:
    `:450`'s first conjunct is satisfied by any https url when `S3_PUBLIC_URL`
    is `''`, but `_store_files_in_s3()` is False, and `:454`'s
    `current_app.config['SERVER_NAME'] in self.source_url` is False because
    `TestConfig.SERVER_NAME` is 'test.piefed.local' (tests/conftest.py:69) and
    the source_url here is on example.com. Nothing outside `chdir_upload` is
    touched.

    `flush_cdn_cache` (`:468`) is likewise a no-op: `CLOUDFLARE_ZONE_ID` and
    `CLOUDFLARE_API_TOKEN` default to `''` (config.py:78-79) and `.env.test`
    sets neither, so `:483`'s `if zone_id and token:` is false and no request
    is made -- which is why these tests need no `http_mock`.
    """

    def test_a_url_change_deletes_the_old_image_from_disk(self, db_session, chdir_upload):
        """`:437` true -> `:438`, `:439` true -> `:440`, then `:441`.
        Arcs 437->438 and 439->440; statements 438, 439, 440, 441.

        `chdir_upload` is here so the `File`'s `file_path` is a real path under
        pytest's `tmp_path` rather than anywhere in the repository, and so the
        witness can be the FILE ITSELF disappearing. Asserting only that
        `post.image_id` is None afterward would witness `:441`, which runs on
        BOTH arms of `:439` -- false-witness mechanism 1.
        """
        s = _seed(url='https://example.com/clip.mp4')
        target = chdir_upload / 'app' / 'static' / 'media' / 'posts' / 'ab' / 'cd'
        target.mkdir(parents=True)
        on_disk = target / 'old.png'
        Image.new('RGB', (8, 8), (1, 2, 3)).save(on_disk, format='PNG')
        old = File(source_url='https://example.com/old.png',
                   file_path=str(on_disk.relative_to(chdir_upload)))
        db.session.add(old)
        db.session.commit()
        s.post.image_id = old.id
        db.session.commit()
        assert on_disk.exists()

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=False)

        db.session.refresh(s.post)
        assert not on_disk.exists()
        assert s.post.image_id is None

    def test_a_dangling_image_id_is_cleared_without_a_delete(self, db_session, chdir_upload):
        """`:437` true, `:439` FALSE -> `:441`. Arc 439->441.

        `post.image_id` points at a row that does not exist, so
        `File.query.get` returns None and `:440` is skipped.

        THE FOREIGN KEY HAS TO BE SUPPRESSED TO BUILD THIS STATE, and that is a
        CORRECTION to the brief, which set `post.image_id = 999999` and
        committed. `Post.image_id` is `db.ForeignKey('file.id')`
        (app/models.py:1705) and tests run with constraints live -- the
        db_session teardown's `session_replication_role = replica` is scoped to
        the teardown statement alone (tests/conftest.py:129 and 169-173). Measured in
        this container, the brief's plain commit raises::

            IntegrityError (psycopg2.errors.ForeignKeyViolation)
            insert or update on table "post" violates foreign key constraint
            "post_image_id_fkey"
            DETAIL:  Key (image_id)=(999999) is not present in table "file".

        so the row is written with the FK triggers disabled for that one
        transaction instead. The subsequent `edit_post` never re-checks the
        constraint: PostgreSQL's referential-integrity trigger on the child
        skips an UPDATE that does not change the key columns, and the only
        other write to `image_id` is `:441`'s NULL, which satisfies it. The
        repository already sanctions this mechanism for exactly this kind of
        orphan: tests/test_shared_post_lifecycle.py:928-937 disables the same
        GUC to build a `CommunityMember` row whose `user_id` no `user` row
        backs.

        THE BRANCH IS NOT DEAD, and this paragraph exists so nobody reads the
        one above and concludes it is. The foreign key forbids CONSTRUCTING
        the state directly; it does not forbid OBSERVING it. `:438` reads an
        in-memory `post.image_id`, not the current database value, and both
        code paths in app/ that delete a post's `File` row null the reference,
        commit, and only THEN delete the row and commit again::

            app/post/routes.py
            2247	            if post.image_id:
            2248	                file_entry_to_delete = post.image_id
            2249	            post.image_id = None
            2250	            post.url = None
            2251	            db.session.commit()
            2252	            if file_entry_to_delete:
            2253	                File.query.filter_by(id=file_entry_to_delete).delete()
            2254	                db.session.commit()

            app/activitypub/util.py   (id captured at :3390 / :3475)
            3565	                post.image_id = None
            ...
            3569	        db.session.commit()
            3570	        if old_db_entry_to_delete:
            3571	            File.query.filter_by(id=old_db_entry_to_delete).delete()
            3572	            db.session.commit()

        A concurrent `edit_post` that loaded the post before that FIRST commit
        still holds the old id in its own session. Under READ COMMITTED -- the
        PostgreSQL default -- its `File.query.get` at `:438` is a NEW statement
        and therefore sees the SECOND commit, so it gets None back. The
        constraint is never violated at any point: by the time the `File` row
        is gone, the only committed `post.image_id` is NULL; the stale id lives
        only in the editing session's memory. That race is what `:439` guards
        against, so the branch must stay covered rather than acquire a
        `# pragma: no branch`.

        The constraint carries no `ON DELETE` clause --
        migrations/versions/54f1dd40e066_initial_tables.py:200 is
        `sa.ForeignKeyConstraint(['image_id'], ['file.id'], )`, i.e. NO ACTION
        -- which is precisely why the delete has to be ordered after the null
        in both call sites, and therefore why the window exists at all.

        THE POSITIVE CONTROL is the test above, which uses the same mechanism
        -- a real file under `chdir_upload` -- and shows it being removed.
        Without that pair, "nothing was deleted" is indistinguishable from a
        broken fixture that never created anything (false-witness mechanism 3).
        The unrelated file written here is the in-test half of the same
        control: it must SURVIVE.
        """
        s = _seed(url='https://example.com/clip.mp4')
        target = chdir_upload / 'app' / 'static' / 'media' / 'posts' / 'ef' / 'gh'
        target.mkdir(parents=True)
        bystander = target / 'unrelated.png'
        Image.new('RGB', (8, 8), (4, 5, 6)).save(bystander, format='PNG')

        db.session.commit()
        db.session.execute(text('SET LOCAL session_replication_role = replica'))
        db.session.execute(text('UPDATE post SET image_id = 999999 WHERE id = :pid'),
                           {'pid': s.post.id})
        db.session.commit()
        db.session.refresh(s.post)

        # 999999 is genuinely absent rather than assumed to be: db_session
        # resets every sequence (tests/conftest.py:131-132), so nothing reaches
        # that id, but `:439`'s false arm depends on it and is asserted.
        assert s.post.image_id == 999999
        assert db.session.get(File, 999999) is None

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=False)

        db.session.refresh(s.post)
        assert s.post.image_id is None
        assert bystander.exists()
        assert db.session.get(File, 999999) is None


class _RecordingDeleteFromS3:
    """Stand-in for the Celery task at `:447`, recording both call shapes.

    `:449` calls it directly and `:451` calls `.delay(...)`, so the double
    needs both, kept in SEPARATE lists -- a single combined list could not
    tell the two arcs apart, which is the whole point of the pair.
    """

    def __init__(self):
        self.direct = []
        self.delayed = []

    def __call__(self, urls):
        self.direct.append(urls)

    def delay(self, urls):
        self.delayed.append(urls)


S3_HOST = 's3.example.com'
S3_VIDEO_URL = f'https://{S3_HOST}/posts/ab/cd/vid.mp4'


class TestS3VideoTeardown:
    """`:442-451` -- deleting the old video from S3 when the url changes.

    THE THIRD CONJUNCT IS UNFALSIFIABLE UNDER THE DEFAULT CONFIG.
    `config.py:108` makes `S3_PUBLIC_URL` `''`, so
    `f'https://{S3_PUBLIC_URL}'` is `'https://'` and every https url starts
    with it. Every test here sets a real host, so the operand can be both
    satisfied and refused.

    `:447`'s import is INSIDE the function body and re-runs on every call, so
    `app.shared.tasks.maintenance.delete_from_s3` is the name to patch;
    patching `app.shared.post.delete_from_s3` would be rebound and ignored.

    THE THREE CONJUNCTS ARE NOT MOVED IN LOCKSTEP. Each of the three
    single-operand tests below asserts inline that the OTHER two are satisfied,
    so a swap between them would be detected -- false-witness mechanism 5.
    Those assertions are also what proves `_configure` did anything at all:
    `store_files_in_s3()` (app/utils.py:4317-4319) reads the three keys the
    helper sets.

    `post.type` reaching `:446` is written by `:398` AND, for these urls, by
    `:409` (`is_video_url` is true for a '.mp4' path), so it is
    over-determined -- but only in the direction each test asserts, and each
    test asserts the value it depends on rather than assuming it.
    """

    def _configure(self, app, monkeypatch, recorder, s3_on=True):
        monkeypatch.setitem(app.config, 'S3_PUBLIC_URL', S3_HOST)
        monkeypatch.setitem(app.config, 'S3_ACCESS_KEY', 'k' if s3_on else '')
        monkeypatch.setitem(app.config, 'S3_ACCESS_SECRET', 's' if s3_on else '')
        monkeypatch.setitem(app.config, 'S3_ENDPOINT', 'e' if s3_on else '')
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_from_s3',
                            recorder, raising=True)

    def test_a_video_on_s3_is_queued_for_deletion(self, db_session, app, monkeypatch):
        """All three conjuncts true, `:448` false -> `:451`.
        Arcs 444->445, 446->447 and 448->451; statements 447, 448, 451.

        ALSO THE POSITIVE CONTROL for `test_a_hostless_url_skips_the_domain_decrement`
        below. `domain_from_url` (app/utils.py:1583-1593) creates the Domain
        row when the url HAS a hostname, and `:445` then decrements its
        `post_count` from the column default 0 (app/models.py:3456) to -1. The
        hostless test asserts no row appears; without this half, that assertion
        could equally be produced by a `domain_from_url` that never creates
        anything (false-witness mechanism 3).
        """
        rec = _RecordingDeleteFromS3()
        self._configure(app, monkeypatch, rec)
        s = _seed(url=S3_VIDEO_URL)
        assert app.debug is False
        assert Domain.query.count() == 0

        edit_post(_api_input(), s.post, POST_TYPE_VIDEO, SRC_API,
                  user=s.user, from_scratch=False)

        assert rec.delayed == [[S3_VIDEO_URL]]
        assert rec.direct == []
        assert {(d.name, d.post_count) for d in Domain.query.all()} == {(S3_HOST, -1)}

    def test_debug_mode_deletes_from_s3_inline(self, db_session, app, monkeypatch):
        """`:448` true -> `:449`. Arc 448->449, statement 449.

        Differs from the test above in `DEBUG` alone. `app.debug` reads
        `config['DEBUG']`, so setting the config item moves the branch.
        """
        rec = _RecordingDeleteFromS3()
        self._configure(app, monkeypatch, rec)
        monkeypatch.setitem(app.config, 'DEBUG', True)
        s = _seed(url=S3_VIDEO_URL)
        assert app.debug is True

        edit_post(_api_input(), s.post, POST_TYPE_VIDEO, SRC_API,
                  user=s.user, from_scratch=False)

        assert rec.direct == [[S3_VIDEO_URL]]
        assert rec.delayed == []

    def test_a_non_video_post_on_s3_is_not_deleted(self, db_session, app, monkeypatch, http_mock):
        """FIRST conjunct false, the other two true. Witnesses the operand
        alone.

        The url is an image rather than a video, so `:408` is false and `:410`
        issues a HEAD -- hence `http_mock`. Under `image/png` `:411` writes
        POST_TYPE_IMAGE, which is also what makes the first conjunct false.
        """
        rec = _RecordingDeleteFromS3()
        self._configure(app, monkeypatch, rec)
        image_url = f'https://{S3_HOST}/posts/ab/cd/pic.png'
        http_mock.head(image_url).respond(200, headers={'Content-Type': 'image/png'})
        s = _seed(url=image_url)

        assert store_files_in_s3() is True                      # second conjunct
        assert image_url.startswith(f'https://{S3_HOST}')       # third conjunct

        edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API,
                  user=s.user, from_scratch=False)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_IMAGE                   # first conjunct false
        assert rec.direct == [] and rec.delayed == []

    def test_a_video_is_not_deleted_when_s3_is_not_configured(self, db_session, app, monkeypatch):
        """SECOND conjunct false, the other two true. `store_files_in_s3()`
        (app/utils.py:4317-4319) is false with the keys empty, while
        `S3_PUBLIC_URL` stays set so the third conjunct would still match.

        The keys are already `''` by default here, so `_configure(s3_on=False)`
        changes only `S3_PUBLIC_URL` -- which is exactly the point, and why the
        `store_files_in_s3()` assertion below is written rather than assumed.
        """
        rec = _RecordingDeleteFromS3()
        self._configure(app, monkeypatch, rec, s3_on=False)
        s = _seed(url=S3_VIDEO_URL)

        assert store_files_in_s3() is False                     # second conjunct
        assert S3_VIDEO_URL.startswith(f'https://{S3_HOST}')    # third conjunct

        edit_post(_api_input(), s.post, POST_TYPE_VIDEO, SRC_API,
                  user=s.user, from_scratch=False)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_VIDEO                   # first conjunct
        assert rec.direct == [] and rec.delayed == []

    def test_a_video_hosted_elsewhere_is_not_deleted_from_s3(self, db_session, app, monkeypatch):
        """THIRD conjunct false, the other two true. The video is real and S3
        is configured; the url simply is not on the S3 host.

        This is the test `S3_PUBLIC_URL` has to be set for: under the default
        `''` the compared prefix is the bare `'https://'`, which EVERY url here
        starts with, so the operand could not be refused at all and Task 9's
        mutation of it would be structurally void rather than killed.
        """
        rec = _RecordingDeleteFromS3()
        self._configure(app, monkeypatch, rec)
        other_url = 'https://elsewhere.example.com/posts/vid.mp4'
        s = _seed(url=other_url)

        assert store_files_in_s3() is True                      # second conjunct
        assert not other_url.startswith(f'https://{S3_HOST}')   # third conjunct false
        assert other_url.startswith('https://')                 # ... but not under the default

        edit_post(_api_input(), s.post, POST_TYPE_VIDEO, SRC_API,
                  user=s.user, from_scratch=False)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_VIDEO                   # first conjunct
        assert rec.direct == [] and rec.delayed == []

    def test_a_hostless_url_skips_the_domain_decrement(self, db_session, app, monkeypatch):
        """`:444` false -> `:446`. Arc 444->446.

        `domain_from_url` returns None when `parsed_url.hostname` is falsy
        (app/utils.py:1583/1595-1596), which a relative url produces, so
        `:445`'s decrement is skipped and -- because app/utils.py:1590-1593's
        create path
        sits inside the SAME `if parsed_url and parsed_url.hostname:` arm -- no
        `Domain` row is created either. The row count is therefore a POSITIVE
        statement about `:444`'s false arm rather than the mere absence of a
        crash; `test_a_video_on_s3_is_queued_for_deletion` above is the
        same-mechanism control showing the row DOES appear, with post_count -1,
        when the hostname is present.

        THIS ARC CANNOT SHARE AN INPUT WITH `446->447`: the S3 conjunct needs
        a url starting `https://<host>`, which necessarily HAS a hostname and
        therefore makes `:444` true. The two arcs need different urls, and
        that is why they are different tests.
        """
        rec = _RecordingDeleteFromS3()
        self._configure(app, monkeypatch, rec, s3_on=False)
        s = _seed(url='/relative/clip.mp4')
        assert Domain.query.count() == 0

        edit_post(_api_input(), s.post, POST_TYPE_VIDEO, SRC_API,
                  user=s.user, from_scratch=False)

        assert rec.direct == [] and rec.delayed == []
        assert Domain.query.count() == 0
        db.session.refresh(s.post)
        assert s.post.url == '/relative/clip.mp4'


class _RecordingMakeImageSizes:
    """Records `make_image_sizes` calls instead of running the real pipeline.

    `:614` and `:616` differ ONLY in their size arguments (170/2000 against
    512/1200), so an assertion on the resulting `File` cannot tell them apart
    -- false-witness mechanism 1, since both arms set `post.image_id` the same
    way at `:608`. The arguments are the witness.

    `make_image_sizes` is imported at MODULE level
    (`app/shared/post.py:17`, `from app.activitypub.util import
    make_image_sizes, notify_about_post`), so
    `app.shared.post.make_image_sizes` is the name to patch. That is unlike
    `:447`'s `delete_from_s3`, whose import sits inside the function body and
    therefore has to be patched on its defining module -- see
    `TestS3VideoTeardown` above.

    Patching also removes the need for the bodiless-404 technique
    (tests/test_shared_post_edit.py:44-52): under eager Celery the real
    `make_image_sizes` EXECUTES, and would otherwise issue a GET these tests
    have no reason to serve -- and which `http_mock`'s `assert_all_called=True`
    would then require them to register.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


PAGE_URL = 'https://example.com/thing'

# The url the EXISTING post carries in TestImageArmEventBanner. See that
# class's docstring for why it is a loops.video url and not a pixelfed one.
SEEDED_OLD_URL = 'https://loops.video/v/old'


class TestImageArmEventBanner:
    """`:601`'s true arm, and `:612`'s fork inside it.

    THIS CLASS FOLLOWS FACT 229's CONVENTION, not this file's inverted one: a
    HEAD reporting `image/png` and NO GET route. `:601` true is the whole
    point, and `:601` is the one arm of the four at `:601`/`:619`/`:630`/`:641`
    that never calls `opengraph_parse` (`:621`, `:632` and `:642` are the other
    three), so a GET route here would go unreached and fail `http_mock`'s
    `assert_all_called=True` at teardown.

    Exactly ONE HEAD is issued per test, but THE REASON CHANGED when the pair
    was reseeded and this paragraph is the amended one. It used to read
    "`_seed()` leaves `post.url` None, so `:403` is false". That is no longer
    true: the first two tests pass `_seed(url=SEEDED_OLD_URL)` and `:403` IS
    entered. The conclusion survives for a different reason -- `SEEDED_OLD_URL`
    is a loops.video url, so the chain stops at `:406`/`:407`, and `:410`'s
    `is_image_url(post.url)` is an `elif` below that which never runs. The
    third and fourth tests still use a bare `_seed()` and reach the same place
    via the original route. Either way `:601`'s `is_image_url(url)` is the only
    caller that issues one. See the reseeding paragraph below for why the url
    has to be a loops.video one rather than a pixelfed one.

    THE FIRST TWO TESTS DIFFER IN THE `type` ARGUMENT AND IN NOTHING ELSE.
    (The class now holds FOUR; the third and fourth are deliberately outside
    that pair and are introduced by their own comment block below.) Both pass
    the same url, the same recorder, the same HEAD route and -- importantly --
    the SAME `event` dict, even though only the event test needs one (see that
    test's docstring for why it needs one at all). Giving the `event` dict to
    only one of them would make `type` and `event_data` vary in lockstep, and
    the pair could then not tell `:612`'s real predicate,
    `if type == POST_TYPE_EVENT:`, from one written on `event_data` --
    false-witness mechanism 5. It is inert in the control: `:696`'s FIRST
    conjunct (`type == POST_TYPE_EVENT`) is false there, so no `Event` row is
    built, and `:617` types the post IMAGE so `app/shared/tasks/pages.py:231`'s
    `elif post.type == POST_TYPE_EVENT:` is never taken either.

    THE PAIR SEEDS `post.url` WITH `SEEDED_OLD_URL`, AND THE CHOICE OF URL IS
    LOAD-BEARING. Task 9's mutation pass found that DELETING `:613` survived
    all 354 tests, because `_seed()` leaves `post.url` None and the event
    test's `assert s.post.url is None` was then the seeded value surviving --
    false-witness mechanism 1, and the paragraph in that test which denied it
    is corrected in place below. A non-None seeded url makes None a value only
    `:613` can have written.

    IT HAD TO BE A loops.video URL RATHER THAN A PIXELFED ONE. Any seeded
    `post.url` makes `:403` true, and the chain then decides what `post.type`
    becomes before `:612` is ever reached. Read out of the file rather than
    recalled::

        403	    if post.url:
        404	        if post.url.startswith('https://pixelfed.social/') or post.url.startswith('https://pixelfed.uno/'):
        405	            post.type = POST_TYPE_IMAGE
        406	        elif post.url.startswith('https://loops.video/'):
        407	            post.type = POST_TYPE_VIDEO
        408	        elif is_video_url(post.url):
        409	            post.type = POST_TYPE_VIDEO
        410	        elif is_image_url(post.url):
        411	            post.type = POST_TYPE_IMAGE

    A pixelfed url takes `:404`/`:405` and writes IMAGE -- which would have
    destroyed the CONTROL test's `post.type == POST_TYPE_IMAGE` witness for
    `:617`, trading one false witness for another. A loops.video url takes
    `:406`/`:407` and writes VIDEO, so IMAGE remains a value only `:617`
    produces, and `:613`'s None becomes real at the same time. `:404`, `:406`
    and `:407` are all pure string tests, so this costs NO extra HTTP route:
    `:410`'s `is_image_url` is an `elif` the chain never reaches.

    `:663`'s `if url and post.image:` IS REACHED BY BOTH TESTS, and `post.image`
    there is an implicit lazy load rather than anything the code assigns.
    `:607` commits (expiring the instance), `:608` writes `post.image_id`, and
    `:663` then resolves the relationship off that freshly-written FK -- no
    line ever sets `post.image` on this path. Both tests depend on it
    completing without error, so a change to that relationship's loader
    strategy would break them at a line none of their docstrings name.
    """

    def test_an_event_with_an_image_url_keeps_no_url_and_gets_a_banner(
            self, db_session, http_mock, monkeypatch):
        """`:612` true -> `:613`, `:614`. Arc 612->613; statements 613, 614.

        Two independent witnesses, because either alone is weak: `post.url`
        ends up None (only `:613` writes that; `:618` writes the url), and
        `make_image_sizes` is called with the banner sizes 170/2000 (only
        `:614` passes those; `:616` passes 512/1200).

        THE WHOLE ARGUMENT TUPLE IS PINNED, not just the sizes. `:614` and
        `:616` each pass five positional arguments, and all five are inside a
        mutation pass's reach; this recorder is the only thing in the file that
        can see any of them. `'posts'` is the storage directory and
        `post.community.low_quality` the last argument, asserted here against
        the community's actual value, which `make_community` leaves at
        `Community.low_quality`'s column default of False (app/models.py:576).
        That kills a fifth argument mutated to `True` or to any truthy
        constant; it cannot kill one mutated to the literal `False`, and the
        `low_quality=True` case that would is deliberately NOT added to this
        pair -- a second varying input is exactly what the class docstring
        explains this pair must not have.

        `post.url` being None IS NOW A REAL WITNESS OF `:613`, AND A CORRECTION
        IS RECORDED HERE. An earlier revision of this docstring claimed it was
        "NOT merely the seeded value surviving" on the strength of the ARC
        argument below. That arc argument is TRUE and is kept. What it does not
        establish is PROVENANCE for the STATEMENT, and Task 9's mutation pass
        measured the gap: with `_seed()`'s url=None, DELETING `:613` outright
        survived all 354 tests, because the assertion held on the seeded value.
        The pair now seeds `SEEDED_OLD_URL`, so None can only have come from
        `:613`. See the class docstring for why that url is a loops.video one.

        THE ARC ARGUMENT, unchanged and still correct: reaching `:612` at all
        requires `:565` true, and the `if`/`else` at `:612` writes `post.url`
        on BOTH arms (`:613` None, `:618` the url). The control below is the
        same-mechanism positive: identical input but a different `type`, and it
        comes back with the url set.

        THE `event` KEY IS REQUIRED, AND THAT WAS MEASURED RATHER THAN READ.
        Without it `input.get('event', None)` is None (`:285`), so `:696`'s
        `if type == POST_TYPE_EVENT and event_data:` is false and NO `Event`
        row is created -- while `:398` has already written
        `post.type = POST_TYPE_EVENT`. `from_scratch=True` then reaches `:741`'s
        `task_selector('make_post', ...)`, which under eager Celery runs
        `app/shared/tasks/pages.py:231-233` and dereferences
        `Event.query.filter_by(post_id=post.id).first().start` -- AttributeError
        on None, downstream of everything this test witnesses. The `event` dict
        has no effect on `:612`, which tests the `type` PARAMETER and not
        `event_data`; it only keeps the post a well-formed event so federation
        can serialise it.
        """
        rec = _RecordingMakeImageSizes()
        monkeypatch.setattr('app.shared.post.make_image_sizes', rec)
        http_mock.head(PAGE_URL).respond(200, headers={'Content-Type': 'image/png'})
        s = _seed(url=SEEDED_OLD_URL)

        edit_post(_api_input(url=PAGE_URL,
                             event={'start': '2030-01-01T09:00:00Z',
                                    'end': '2030-01-01T10:00:00Z'}),
                  s.post, POST_TYPE_EVENT, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.url is None
        assert s.post.image_id is not None
        assert len(rec.calls) == 1
        assert s.community.low_quality is False
        assert rec.calls[0] == ((s.post.image_id, 170, 2000, 'posts', False), {})

    def test_a_non_event_with_an_image_url_keeps_the_url_and_gets_a_thumbnail(
            self, db_session, http_mock, monkeypatch):
        """`:612` false -> `:616`, `:617`, `:618`. Arc 612->616.

        THE POSITIVE CONTROL for the test above. The ONLY input that varies is
        the `type` argument: the url, the recorder, the HEAD route and the
        `event` dict are all identical, deliberately -- see the class docstring
        for why the `event` dict has to be passed here too even though nothing
        reads it. Every witness comes back with the opposite value: the url is
        written rather than cleared, the sizes are 512/1200 rather than
        170/2000, and `post.type` becomes IMAGE.

        `POST_TYPE_IMAGE` at `:617` is a real witness rather than a default:
        `:398` wrote `POST_TYPE_LINK` here, and `Post.type`'s column default is
        `POST_TYPE_ARTICLE` (app/models.py:1715), so IMAGE is neither the
        seeded value nor the submitted one. The only other writers of IMAGE are
        `:405` and `:411`, both inside `:403`'s block. `:403` IS now entered --
        the pair seeds `SEEDED_OLD_URL` to give `:613` a witness -- but that
        url is a loops.video one, so the chain takes `:406`/`:407` and writes
        VIDEO. `:405` and `:411` are `if`/`elif` siblings the chain never
        reaches, so IMAGE still has exactly one possible source here. That is
        the whole reason the seeded url is not a pixelfed one; see the class
        docstring.

        The argument tuple is pinned in full for the same reason as in the test
        above, and with the same limit on what an unchanged `low_quality` can
        witness.
        """
        rec = _RecordingMakeImageSizes()
        monkeypatch.setattr('app.shared.post.make_image_sizes', rec)
        http_mock.head(PAGE_URL).respond(200, headers={'Content-Type': 'image/png'})
        s = _seed(url=SEEDED_OLD_URL)

        edit_post(_api_input(url=PAGE_URL,
                             event={'start': '2030-01-01T09:00:00Z',
                                    'end': '2030-01-01T10:00:00Z'}),
                  s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.url == PAGE_URL
        assert s.post.type == POST_TYPE_IMAGE
        assert len(rec.calls) == 1
        assert s.community.low_quality is False
        assert rec.calls[0] == ((s.post.image_id, 512, 1200, 'posts', False), {})

    # ------------------------------------------------------------------
    # The two tests below are the THIRD and FOURTH in this class and are
    # DELIBERATELY OUTSIDE THE PAIR ABOVE. They exist only to kill the fifth
    # argument of `:614`/`:616` mutated to the LITERAL `False`, which the pair
    # cannot kill because `make_community` leaves `Community.low_quality` at
    # its column default of False (app/models.py:555 `class Community`,
    # app/models.py:576 `low_quality = db.Column(db.Boolean, default=False)`).
    #
    # WHY A THIRD AND FOURTH TEST RATHER THAN A CHANGE TO THE PAIR. Task 4
    # showed that varying `low_quality` ACROSS the pair moves the hole instead
    # of closing it: pairing EVENT with True and LINK with False lets a
    # predicate written `if post.community.low_quality:` pass both, and the
    # anti-correlated pairing lets `if not post.community.low_quality:` pass
    # both. The pair's own invariant -- `type` is the ONLY input that varies --
    # is what makes it a witness for `:612`, so `low_quality` has to move in a
    # test where `type` is held against the pair's value instead.
    #
    # `:614` and `:616` are mutually exclusive arms of `:612`, so ONE extra
    # test cannot reach both call sites; each arm needs its own. Task 9's brief
    # authorised one; two are added, because closing `:616` and leaving `:614`
    # open would have left half the finding standing.

    def test_a_low_quality_community_reaches_the_event_banner_call(
            self, db_session, http_mock, monkeypatch):
        """`:614`'s FIFTH argument, and nothing else.

        Identical in every input to
        `test_an_event_with_an_image_url_keeps_no_url_and_gets_a_banner` except
        that the community's `low_quality` is True, so the ONLY assertion that
        can come back different is the last element of the recorded argument
        tuple. The sizes are still asserted as 170/2000 so this remains a test
        OF `:614` rather than of `:616`.

        `s.community.low_quality is True` is asserted before the tuple for the
        same reason the pair asserts it False: without it, a tuple ending True
        would be consistent with `make_community` having changed its default,
        and the witness would be reading the fixture rather than the call.
        """
        rec = _RecordingMakeImageSizes()
        monkeypatch.setattr('app.shared.post.make_image_sizes', rec)
        http_mock.head(PAGE_URL).respond(200, headers={'Content-Type': 'image/png'})
        s = _seed()
        s.community.low_quality = True
        db.session.commit()

        edit_post(_api_input(url=PAGE_URL,
                             event={'start': '2030-01-01T09:00:00Z',
                                    'end': '2030-01-01T10:00:00Z'}),
                  s.post, POST_TYPE_EVENT, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.community.low_quality is True
        assert len(rec.calls) == 1
        assert rec.calls[0] == ((s.post.image_id, 170, 2000, 'posts', True), {})

    def test_a_low_quality_community_reaches_the_thumbnail_call(
            self, db_session, http_mock, monkeypatch):
        """`:616`'s FIFTH argument, and nothing else.

        The same construction as the test above against the OTHER arm of
        `:612`: identical to
        `test_a_non_event_with_an_image_url_keeps_the_url_and_gets_a_thumbnail`
        except for the community's `low_quality`, with 512/1200 still asserted
        so it stays a test of `:616`.
        """
        rec = _RecordingMakeImageSizes()
        monkeypatch.setattr('app.shared.post.make_image_sizes', rec)
        http_mock.head(PAGE_URL).respond(200, headers={'Content-Type': 'image/png'})
        s = _seed()
        s.community.low_quality = True
        db.session.commit()

        edit_post(_api_input(url=PAGE_URL,
                             event={'start': '2030-01-01T09:00:00Z',
                                    'end': '2030-01-01T10:00:00Z'}),
                  s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.community.low_quality is True
        assert len(rec.calls) == 1
        assert rec.calls[0] == ((s.post.image_id, 512, 1200, 'posts', True), {})


class TestVideoHostingSiteArm:
    """`:660-661` -- the `elif` after `:565`, reached only when `:565` is FALSE
    while `url` is still truthy.

    `:565` is `if url and (from_scratch or url_changed):`, so closing it with a
    url present means `from_scratch=False` AND `url_changed` false. `:435` sets
    `url_changed` only when `url != post.url or uploaded_file`, so the post is
    seeded with the SAME url that is submitted, and no file is uploaded.

    THIS CLASS USES THE INVERTED CONVENTION -- a HEAD reporting a non-image
    type -- and for a reason the previous class does not share: `:403`'s
    `if post.url:` is true here (the post has a url), so `:410`'s
    `is_image_url(post.url)` runs and issues a HEAD. `text/html` keeps
    `:403-411` from retyping the post, leaving `:661` the only writer of
    `post.type` below `:398`.

    BOTH TESTS SUBMIT `POST_TYPE_LINK`, NOT `POST_TYPE_ARTICLE`. `:398` writes
    whatever `type` is passed, and `Post.type`'s column default is
    `POST_TYPE_ARTICLE` (app/models.py:1715) while `make_post` never sets
    `type` -- so a seeded post is ALREADY ARTICLE before `edit_post` runs, and
    an `== POST_TYPE_ARTICLE` assertion in the control below could not tell
    "`:661` was skipped" from "`edit_post` did nothing to `type` at all"
    (false-witness mechanism 1). LINK is not the default, so the control's
    assertion is a positive statement that `:398` ran and `:661` did not.
    """

    def test_a_youtube_url_that_did_not_change_still_retypes_the_post_as_video(
            self, db_session, http_mock):
        """`:660` true -> `:661`. Arc 660->661, statement 661.

        `type=POST_TYPE_LINK` is submitted, so `:398` writes LINK and only
        `:661` can produce VIDEO: `:407`/`:409` are shut out because the url is
        neither a loops.video url nor one with a video extension, and `:655`
        is inside the `:565` block this test deliberately closes.

        `is_video_hosting_site` (app/utils.py:316-329) matches on the
        'https://youtube.com' prefix (app/utils.py:319) and issues no request
        of its own, so the one registered HEAD is `:410`'s and is consumed.
        """
        youtube = 'https://youtube.com/watch?v=abc123'
        http_mock.head(youtube).respond(200, headers={'Content-Type': 'text/html'})
        s = _seed(url=youtube)

        edit_post(_api_input(url=youtube), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=False)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_VIDEO

    def test_a_plain_url_that_did_not_change_leaves_the_type_alone(
            self, db_session, http_mock):
        """`:660` false -> `:663`. THE POSITIVE CONTROL: identical except the
        host is not a video-hosting site, so the post keeps the LINK type
        `:398` wrote.

        The url ends '/watch-this' rather than '/watch' on purpose: it holds
        the word 'watch' without holding the substring 'videos/watch', which
        `app/utils.py:326-327` accepts as PeerTube. Otherwise this control
        would take `:660`'s TRUE arm -- false-witness mechanism 4.

        That PeerTube route needs no witness HERE. It reaches `:661` by the
        same arc the youtube test already closes and adds no line or arc in
        `app/shared/post.py`, and
        `tests/test_utils_strings.py:26-27`
        (`TestIsVideoHostingSite::test_peertube_is_matched_by_path_not_host`)
        already pins the substring check itself.
        """
        plain = 'https://example.com/watch-this'
        http_mock.head(plain).respond(200, headers={'Content-Type': 'text/html'})
        s = _seed(url=plain)

        edit_post(_api_input(url=plain), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=False)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_LINK


PIXELFED_URL = 'https://pixelfed.social/p/alice/1'


class TestPixelfedArm:
    """`:619-629` -- the pixelfed arm of the `:601`/`:619`/`:630`/`:641` chain.

    THE HARNESS IS INVERTED relative to tests/test_shared_post_upload.py. That
    file's rule (tests/README.md fact 229 point 3) is a HEAD reporting
    `image/png` and NO GET route, because an image content type makes `:601`
    true and `:601` never calls `opengraph_parse`. Reaching `:619` requires the
    opposite on both halves: a HEAD reporting a NON-image type so `:601` is
    false, and a GET route because `:621` WILL call `opengraph_parse` -- with
    ONE measured exception, the scheme-less test at the bottom, whose GET never
    leaves `app/utils.py` at all.

    A REGISTERED DIVERGENCE, PINNED HERE AND DELIBERATELY NOT FIXED. The two
    lines were read off the file rather than recalled::

        404	        if post.url.startswith('https://pixelfed.social/') or post.url.startswith('https://pixelfed.uno/'):
        619	        elif url.startswith('https://pixelfed.social') or url.startswith('pixelfed.uno'):

    `:404` matches `'https://pixelfed.social/'` and `'https://pixelfed.uno/'`
    -- both with a scheme and a trailing slash. `:619` matches
    `'https://pixelfed.social'` (no trailing slash) and `'pixelfed.uno'` (NO
    SCHEME AT ALL). The scheme-less disjunct is REACHABLE, not dead:
    `is_image_url('pixelfed.uno/p/bob/2')` is False, measured in this container
    twice over -- once with respx absent, where httpx itself raises
    `httpx.UnsupportedProtocol` (a subclass of `httpx.TransportError` ->
    `httpx.RequestError` -> `httpx.HTTPError`, printed from `__mro__`) so
    `mime_type_using_head`'s handler at app/utils.py:345 returns `''`; and once
    with respx present and that same exception installed as the route's
    side effect. Either way `is_image_url` falls through to extension sniffing
    and finds no image extension.

    The two sites also read DIFFERENT VALUES: `:404` tests `post.url`, the url
    the post already has, while `:619` tests `url`, the newly submitted one,
    which `:618`/`:628`/`:640`/`:652` have not yet written. So this is not one
    value checked twice with different strictness; it is two classifiers of the
    same kind of thing applied at different points, disagreeing. Whether
    scheme-less input should be accepted at all is a product question, and this
    round has no standing to answer it. Both branches are pinned as they behave
    today; the divergence is registered, not endorsed and not condemned.

    WHY RESPX HAS TO BE TOLD TO RAISE, for the scheme-less test only. This is a
    CORRECTION to the brief, which predicted respx would never see the request.

    RESPX REPLACES THE VERY METHOD THAT RAISES, and this paragraph is itself a
    correction: an earlier revision said respx patches "below the point where
    `httpx.Client._transport_for_url` would raise `UnsupportedProtocol`", which
    was recalled rather than read. `_transport_for_url` raises nothing. Read
    out of the installed httpx 0.28.1 / respx 0.23.1 in this container::

        httpx/_client.py
        760	    def _transport_for_url(self, url: URL) -> BaseTransport:
        ...
        765	        for pattern, transport in self._mounts.items():
        766	            if pattern.matches(url):
        767	                return self._transport if transport is None else transport
        768
        769	        return self._transport

    The raise is httpcore's, one layer further in::

        httpcore/_sync/connection_pool.py
        199	    def handle_request(self, request: Request) -> Response:
        ...
        205	        scheme = request.url.scheme.decode()
        206	        if scheme == "":
        207	            raise UnsupportedProtocol(
        208	                "Request URL is missing an 'http://' or 'https://' protocol."
        209	            )

    and httpx only RE-LABELS it on the way out::

        httpx/_transports/default.py
        88	        httpcore.UnsupportedProtocol: UnsupportedProtocol,
        ...
        249	        with map_httpcore_exceptions():
        250	            resp = self._pool.handle_request(req)

    respx's default mocker is `HTTPCoreMocker` (`respx/mocks.py:339`,
    `DEFAULT_MOCKER: str = HTTPCoreMocker.name`), and it patches::

        respx/mocks.py
        262	class HTTPCoreMocker(AbstractRequestMocker):
        263	    name = "httpcore"
        264	    targets = [
        265	        "httpcore._sync.connection.HTTPConnection",
        266	        "httpcore._sync.connection_pool.ConnectionPool",
        ...
        272	    target_methods = ["handle_request", "handle_async_request"]

    -- `ConnectionPool.handle_request`, i.e. the exact method holding lines
    205-209. So with a router active there is no longer anything to raise, and
    the HEAD *does* reach respx, as
    `<Request('HEAD', '/pixelfed.uno/p/bob/2')>`. The message this class
    injects is copied verbatim from `connection_pool.py:208` above, so the
    double is a transcription of production's own exception rather than an
    invention. Registering no route at all
    therefore raises `AllMockedAssertionError`, which is neither
    `httpx.HTTPError` nor `httpx.InvalidURL` and escapes app/utils.py:345.
    Registering a normal 200 does not work either: httpx's own cookie jar then
    hands the scheme-less url to `urllib.request.Request` and dies with
    `ValueError: unknown url type: '/pixelfed.uno/p/bob/2'`
    (httpx/_models.py:1106 and :1250), which is likewise not an
    `httpx.HTTPError`. The only faithful mock is the exception production
    actually produces, so the route's side effect IS
    `httpx.UnsupportedProtocol`.
    """

    def test_a_pixelfed_url_is_typed_as_an_image_and_keeps_its_url(
            self, db_session, http_mock):
        """`:619` true -> `:620`; `:622` true -> `:623`; `:624` true -> `:625`,
        `:626`, `:627`; then `:628`, `:629`.
        Arcs 619->620, 622->623, 624->625; statements 620-629.

        FOUR witnesses, because no one of them is unique to this arm:
          - `post.type` is IMAGE -- also what `:617` writes, so on its own it
            cannot tell `:620` from `:617`. The NEXT bullet is what does; this
            one is only a witness in company.
          - `post.body` ends with the 'Source: ' suffix -- `:629` is the ONLY
            line in the function that appends it, so this is the arm's
            signature, and it is what rules `:601`'s arm out.
          - a `File` exists whose `source_url` is the og:image, not the post
            url -- `:602`'s File would carry the post url instead.
          - the File's `alt_text` is `''`, NOT the og:title, and that is a
            CORRECTION to the brief, which offered alt_text as the fourth
            witness for `:625`. Measured: this test first asserted
            `alt_text == 'A photo'` and failed with `assert '' == 'A photo'`.
            `:663-666` (`if url and post.image:` / `file.alt_text =
            image_alt_text`) runs AFTER the arm and overwrites `:625`'s value
            unconditionally, with `''` because `_api_input` supplies no
            `image_alt_text` and `:262` defaults it. So alt_text witnesses
            `:666`, never `:625`, and `:625`'s `shorten_string(...)` argument
            is not observable in the final row at all.
            `test_a_pixelfed_url_falls_back_to_og_image_url` is the
            same-mechanism positive control for that claim: it passes a
            non-empty `image_alt_text` and gets it back, so the `''` here is
            `:666` writing rather than `:625` failing to.

        SURVIVORS IN THIS CLASS, AND THE TWO HALVES OF `:625`'s ALT_TEXT
        ARGUMENT MUST BE NAMED SEPARATELY. Because `:666` overwrites the
        column, a mutation of `:625`'s
        `alt_text=shorten_string(opengraph.get('og:title'), 295)` survives this
        test and every other test IN THIS CLASS. Outside it the two halves now
        part company:

          - THE `og:title` OPERAND IS KILLED, and this is an UPDATE to an
            earlier revision of this note, which said closing it "would need an
            assertion taken before `:663` runs, or a change to `app/`". Neither
            turned out to be necessary.
            `TestPollAndEventTail::test_an_unflushed_thumbnail_is_not_found_by_its_own_foreign_key`
            (at the bottom of this file) drives a BARE-DOMAIN pixelfed url, on
            which `:659`'s `calculate_cross_posts` returns at
            app/models.py:2362 before it can autoflush, so `post.image_id` is
            still None at `:664`, `:665` is false and `:666` never runs. That
            test asserts `alt_text == 'A photo'`, i.e. `:625`'s own value, so
            the og key `:625` reads IS observable there.
          - THE `295` WIDTH IS STILL AN EQUIVALENT MUTANT and Task 9 should NOT
            hunt a kill for it. The og:title on that path is 'A photo', seven
            characters, so `shorten_string` shortens nothing and any width
            above seven produces the same string. No test in this file feeds an
            og:title longer than 295 characters on a path where `:666` does not
            overwrite, which is what a kill would require.

        The `source_url=filename` half of the same line IS killed from inside
        this class: this test and
        `test_a_pixelfed_url_falls_back_to_og_image_url` pin it to two
        different og keys.
        """
        http_mock.head(PIXELFED_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, PIXELFED_URL,
                        og_image='https://cdn.example.com/shot.jpg',
                        og_title='A photo')
        s = _seed()

        edit_post(_api_input(url=PIXELFED_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_IMAGE
        assert s.post.url == PIXELFED_URL
        assert s.post.body.endswith('\n\nSource: ')
        file = db.session.get(File, s.post.image_id)
        assert file.source_url == 'https://cdn.example.com/shot.jpg'
        assert file.alt_text == ''  # `:666` overwrote `:625`'s 'A photo'

    def test_a_pixelfed_url_falls_back_to_og_image_url(self, db_session, http_mock):
        """`:622`'s SECOND disjunct alone, and `:623`'s `or` fallback.

        The page carries `og:image:url` and no `og:image`, so
        `opengraph.get('og:image', '') != ''` is False and the block is
        admitted by the second disjunct; `:623`'s
        `opengraph.get('og:image') or opengraph.get('og:image:url')` then
        returns None on its left operand and falls through to the right.
        `'og:image:url'` is one of `parse_page`'s `tags_to_search`
        (app/utils.py:3181), so it really does reach the dict.

        Without this test the two disjuncts move only in lockstep and a swap
        between them is undetectable -- false-witness mechanism 5.

        ALSO THE POSITIVE CONTROL for the `alt_text == ''` assertion in the
        test above. `og:title` is 'Fallback' here and `image_alt_text` is a
        different, distinctive string; the row comes back carrying the
        `image_alt_text`, which shows `:666` is what writes that column on this
        path and that the `''` above is a write rather than an absence
        (false-witness mechanism 3).
        """
        http_mock.head(PIXELFED_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, PIXELFED_URL,
                        og_image_url='https://cdn.example.com/fallback.jpg',
                        og_title='Fallback')
        s = _seed()

        edit_post(_api_input(url=PIXELFED_URL, image_alt_text='supplied by the caller'),
                  s.post, POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        file = db.session.get(File, s.post.image_id)
        assert file.source_url == 'https://cdn.example.com/fallback.jpg'
        assert file.alt_text == 'supplied by the caller'  # `:666`, not `:625`

    def test_a_pixelfed_url_with_an_unreadable_page_still_keeps_its_url(
            self, db_session, http_mock):
        """`:622` false -> `:628`. Arc 622->628.

        `_unreadable_page` makes `parse_page` return False at
        app/utils.py:3195-3196, so `opengraph` is falsy and the whole File
        block is skipped -- but `:620`, `:628` and `:629` still run.

        THE POSITIVE CONTROL for 'no File' is the first test in this class,
        which builds one through the same mechanism. `post.type`, `post.url`
        and the 'Source: ' suffix are asserted here too, so this is not a
        bare emptiness assertion (false-witness mechanism 3).

        THIS TEST AND `test_a_site_relative_og_image_is_not_turned_into_a_file`
        ASSERT THE SAME ABSENCE, and are told apart by which mutation kills
        them rather than by their assertions. Here the input makes `:622`
        false; there `:622` is true and `:624` is false. See that test's
        docstring for the pair of mutations Task 9 must confirm.
        """
        http_mock.head(PIXELFED_URL).respond(200, headers={'Content-Type': 'text/html'})
        _unreadable_page(http_mock, PIXELFED_URL)
        s = _seed()

        edit_post(_api_input(url=PIXELFED_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_IMAGE
        assert s.post.url == PIXELFED_URL
        assert s.post.body.endswith('\n\nSource: ')
        assert s.post.image_id is None
        assert File.query.count() == 0

    def test_a_readable_page_with_no_og_image_builds_no_file(
            self, db_session, http_mock):
        """`:622`'s SECOND conjunct is the ONLY thing that decides here.

        ADDED BY TASK 9's MUTATION PASS, which found the second conjunct
        unwitnessed in all three arms. Every other test in this class moves the
        two conjuncts in lockstep or moves only the first:

            unreadable page      opengraph is False   -> conjunct 1 decides
            og:image present     both true            -> neither decides alone
            og:image:url only    both true            -> neither decides alone

        so replacing `opengraph.get('og:image', '') != '' or
        opengraph.get('og:image:url', '') != ''` with `True` changed nothing
        that any test could see. Here `opengraph` is a NON-EMPTY dict --
        `{'og:title': 'Only a title'}` -- so the first conjunct is true and
        cannot decide, while both og:image keys are absent so the second is
        false. Under the mutant `:623` yields None and `:624`'s
        `filename.startswith` raises AttributeError.

        THE EMPTY-TAG PAGE WOULD NOT DO. `_opengraph_page` with no tags returns
        an EMPTY dict, which is falsy, so it decides at the first conjunct just
        as `_unreadable_page` does -- see that helper's docstring. A tag the
        code does not read is what makes the dict truthy without making the
        second conjunct true, and `og:title` is the natural one because
        `:625` would have consumed it had the block been entered.

        `post.type`, `post.url` and the 'Source: ' suffix are asserted so this
        is not a bare emptiness assertion (false-witness mechanism 3); `:620`,
        `:628` and `:629` all run on this path.
        """
        http_mock.head(PIXELFED_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, PIXELFED_URL, og_title='Only a title')
        s = _seed()

        edit_post(_api_input(url=PIXELFED_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_IMAGE
        assert s.post.url == PIXELFED_URL
        assert s.post.body.endswith('\n\nSource: ')
        assert s.post.image_id is None
        assert File.query.count() == 0

    def test_a_site_relative_og_image_is_not_turned_into_a_file(
            self, db_session, http_mock):
        """`:624` false -> `:628`. Arc 624->628.

        Here `opengraph` IS truthy and `:623` DID produce a filename -- the
        difference from the test above is that the filename starts with '/',
        so `:624`'s `not filename.startswith('/')` is false.

        THE TWO TESTS ASSERT THE SAME ABSENCE, deliberately, and are told apart
        by which mutation kills them rather than by their assertions: forcing
        `:624` true makes THIS test build a File (a kill, and the reason the
        og:image here is a well-formed relative path rather than junk), while
        forcing `:622` false makes the FIRST test lose one. Neither mutation
        touches the other test. Task 9 must confirm both.
        """
        http_mock.head(PIXELFED_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, PIXELFED_URL, og_image='/relative/shot.jpg',
                        og_title='Relative')
        s = _seed()

        edit_post(_api_input(url=PIXELFED_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.image_id is None
        assert File.query.count() == 0
        assert s.post.url == PIXELFED_URL

    def test_a_scheme_less_pixelfed_uno_url_takes_the_same_arm(
            self, db_session, http_mock):
        """`:619`'s SECOND disjunct, which carries no scheme at all.

        PINS A REGISTERED DIVERGENCE. `:404` would NOT match this string --
        it requires 'https://pixelfed.uno/' -- so the same input is classified
        differently depending on which of the two dispatches sees it. The test
        records today's behaviour; it does not endorse it. See the class
        docstring.

        THE ROUTING IS MEASURED, AND BOTH HALVES CORRECT THE BRIEF.

        The HEAD: with a respx router active the request DOES reach respx (as
        `HEAD /pixelfed.uno/p/bob/2`), so a route is required rather than
        forbidden -- but it must RAISE, because a mocked 200 kills httpx's
        cookie jar with `ValueError: unknown url type`. The side effect is the
        exception unmocked httpx raises for this very url, so `:601`'s
        `is_image_url` is False by the same mechanism as in production. See the
        class docstring for the two measurements.

        The GET: there is NO GET route, and that is not an oversight.
        `opengraph_parse` at `:621` DOES run, but `get_request`
        (app/utils.py:131-134) rejects the uri through
        `is_invalid_get_request_uri` (app/utils.py:5494-5501: `furl(uri).host`
        is empty for a scheme-less string, so it returns True) and raises
        `httpx.HTTPError` before any transport is reached. `opengraph_parse`'s
        `except Exception` (app/utils.py:3006-3007) swallows it and returns
        None. Measured: the router recorded 0 calls and logged
        'invalid get request pixelfed.uno/p/bob/2'. So `:622` is FALSE here and
        this test travels 622->628, the same arc as
        `test_a_pixelfed_url_with_an_unreadable_page_still_keeps_its_url` --
        by a different mechanism (a refused uri rather than a 404 page), which
        is why it cannot substitute for that test and does not try to.

        `http_mock`'s `assert_all_called=True` is load-bearing twice over: it
        proves the HEAD was issued, and, with only one route registered, a GET
        that ever did escape to the transport would raise
        `AllMockedAssertionError` instead of passing unnoticed.
        """
        bare = 'pixelfed.uno/p/bob/2'
        http_mock.head(url__regex=r'.*').mock(
            side_effect=httpx.UnsupportedProtocol(
                "Request URL is missing an 'http://' or 'https://' protocol."))
        s = _seed()

        edit_post(_api_input(url=bare), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_IMAGE
        assert s.post.url == bare
        assert s.post.body.endswith('\n\nSource: ')


LOOPS_URL = 'https://loops.video/v/clip9'


class TestLoopsArm:
    """`:630-640` -- the loops.video arm of the `:601`/`:619`/`:630`/`:641` chain.

    THE HARNESS IS THE INVERTED ONE, the same as TestPixelfedArm's: a HEAD
    reporting `text/html` so `is_image_url` is False and `:601` does not take
    the chain, plus a GET route because `:632` WILL call `opengraph_parse`.
    tests/README.md fact 229 point 3 records the opposite rule, and it is
    correct only for tests/test_shared_post_upload.py.

    `post.url` IS NOT A WITNESS HERE, and this is the class's central
    constraint. Read out of the file rather than recalled::

        640	            post.url = url
        652	            post.url = embed_url

    and `fixup_url` (app/utils.py:3311-3312) opens
    `thumbnail_url = embed_url = url`, diverging only for youtube domains and
    for a peertube url whose last 25 characters begin '/w/' (app/utils.py:3330).
    Measured on this class's url: `len('https://loops.video/v/clip9')` is 27,
    so the length test passes, but `url[-25:][:3]` is 'tps', not '/w/'. So
    `embed_url == url`, the two arms leave `post.url` IDENTICAL, and asserting
    on it discriminates nothing -- false-witness mechanism 1 in its subtlest
    form. Where these tests assert `post.url` at all it is annotated as a
    non-discriminator.

    TWO THINGS DO DISCRIMINATE, and every test leans on one or both:

    1. `:636`'s `.replace('.jpg', '.720p.mp4')`. `:637` stores
       `source_url=filename` AFTER the rewrite, so a `File.source_url` ending
       '.720p.mp4' can only have come from `:636`. Neither `:625` (pixelfed)
       nor `:646` (generic, via `url_to_thumbnail_file`) rewrites anything.

    2. `:631`'s unconditional `post.type = POST_TYPE_VIDEO`. The generic arm
       decides the type at `:654-657`, and for THIS url it would decide LINK.
       Read out of app/utils.py rather than recalled::

           294	def is_video_url(url: str) -> bool:
           295	    common_video_extensions = ['.mp4', '.webm']
           ...
           316	def is_video_hosting_site(url: str) -> bool:
           ...
           319	    video_hosting_sites = ['https://youtube.com', 'https://www.youtube.com', 'https://youtu.be',
           320	                           'https://www.vimeo.com', 'https://vimeo.com', 'https://streamable.com',
           321	                           'https://www.redgifs.com/watch/']
           ...
           326	    if 'videos/watch' in url:  # PeerTube
           327	        return True

       'https://loops.video/v/clip9' has no '.mp4'/'.webm' path extension, is
       on none of those six prefixes and contains no 'videos/watch', so
       `:654`'s four disjuncts are all false and `:657` would write
       POST_TYPE_LINK. VIDEO on this url is therefore the arm's signature.
       (The same reading is why `:660`'s `elif url and is_video_hosting_site(url)`
       is irrelevant here -- it is a sibling of the whole `:601` chain, and
       loops.video is not in its list either.)

    `:636` IS GLOBAL: `str.replace` with no `count` rewrites EVERY occurrence,
    and `test_every_jpg_in_a_loops_thumbnail_is_rewritten` at the bottom of
    this class measures that rather than asserting it in prose. It closes no
    arc; it earns its place by a unique mutant kill, which is the standing
    requirement for a test that adds no coverage. See its docstring for the
    one mutant of `:636` that NO test in this class kills.

    `:636` is also UNCONDITIONAL, but that fact is NOT testable from here and
    this class no longer pretends otherwise. A revision of this class carried
    a sixth test feeding a '.png' thumbnail and asserting it reached
    `File.source_url` unrewritten; it was REMOVED because it killed nothing.
    A filename with no '.jpg' yields the identical stored value whether `:636`
    runs, is guarded, is count-limited or is deleted outright -- an input that
    takes the same path under every variant, which is false-witness mechanism
    4 in pure form.
    """

    def test_a_loops_url_is_typed_as_video_and_rewrites_the_thumbnail_to_mp4(
            self, db_session, http_mock):
        """`:630` true -> `:631`; `:633` true -> `:634`; `:635` true -> `:636`,
        `:637`, `:638`, `:639`; then `:640`.
        Arcs 630->631, 633->634, 635->636; statements 631-640.

        TWO witnesses, and neither is this arm's on its own account alone:
          - `post.type` is VIDEO for a url that `:654`'s four disjuncts all
            reject, so the generic arm would have written LINK at `:657`. See
            the class docstring for the reading that establishes it.
          - a `File` whose `source_url` ends '.720p.mp4' although the og:image
            ends '.jpg'. `:636` is the ONLY line in `edit_post` that performs
            that substitution, so this pins `:636` and `:637` together.

        `post.url` IS DELIBERATELY NOT ASSERTED HERE. `:640` and `:652` write
        the same value for this url (class docstring), so it would be a
        false witness.

        A CORRECTION TO THE BRIEF, measured rather than reasoned. The brief's
        fourth assertion was `file.alt_text == 'A clip'`. That is wrong, for
        exactly the reason Task 5 found on the pixelfed arm. Read out of the
        file::

            663	    if url and post.image:
            664	        file = File.query.get(post.image_id)
            665	        if file:
            666	            file.alt_text = image_alt_text

        and, for the default::

            262	        image_alt_text = input['image_alt_text'] if 'image_alt_text' in input else ''

        `:663-666` runs AFTER the whole `:601` chain and overwrites `:637`'s
        alt_text UNCONDITIONALLY, with '' because `_api_input` supplies no
        `image_alt_text`. So alt_text on this row witnesses `:666`, never
        `:637`. `test_a_loops_url_falls_back_to_og_image_url` is the
        same-mechanism positive control: it passes a distinctive
        `image_alt_text` and gets it back, which shows the '' here is `:666`
        WRITING rather than `:637` failing to (false-witness mechanism 3).

        AN EXPECTED SURVIVOR, NOT A HOLE. Because `:666` overwrites the
        column, a mutation of `:637`'s `alt_text=shorten_string(
        opengraph.get('og:title'), 295)` -- the ARGUMENT, not the whole line --
        WILL SURVIVE this test and every other test in this class. Task 9's
        mutation pass should record it as expected rather than chase it. The
        `source_url=filename` half of `:637` IS killed: this test and
        `test_a_loops_url_falls_back_to_og_image_url` pin it to two different
        og keys. Closing the alt_text half would need an assertion taken
        before `:663` runs, or a change to `app/`, and neither is in scope.
        """
        http_mock.head(LOOPS_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, LOOPS_URL,
                        og_image='https://cdn.loops.example/thumb.jpg',
                        og_title='A clip')
        s = _seed()

        edit_post(_api_input(url=LOOPS_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_VIDEO
        file = db.session.get(File, s.post.image_id)
        assert file.source_url == 'https://cdn.loops.example/thumb.720p.mp4'
        assert file.alt_text == ''  # `:666` overwrote `:637`'s 'A clip'

    def test_a_loops_url_falls_back_to_og_image_url(self, db_session, http_mock):
        """`:633`'s SECOND disjunct alone, and `:634`'s `or` fallback.

        The page carries `og:image:url` and no `og:image`, so
        `opengraph.get('og:image', '') != ''` is False and the block is
        admitted only by the second disjunct; `:634`'s
        `opengraph.get('og:image') or opengraph.get('og:image:url')` then gets
        None from its left operand and falls through to the right.
        'og:image:url' is one of `parse_page`'s `tags_to_search`
        (app/utils.py:3181), so it really does reach the dict.

        Without this test the two disjuncts of `:633` move only in lockstep
        with the first, and a swap between them is undetectable --
        false-witness mechanism 5.

        ALSO THE POSITIVE CONTROL for the `alt_text == ''` assertion in the
        test above. `og:title` is 'Fallback' here while `image_alt_text` is a
        different, distinctive string, and the row comes back carrying the
        `image_alt_text` -- which shows `:666` is what writes that column on
        this path, and that the '' above is a write rather than an absence
        (false-witness mechanism 3).

        The '.720p.mp4' tail keeps `:636` witnessed on this path too, so the
        fallback is not merely reaching a File but reaching THIS arm's File.
        """
        http_mock.head(LOOPS_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, LOOPS_URL,
                        og_image_url='https://cdn.loops.example/alt.jpg',
                        og_title='Fallback')
        s = _seed()

        edit_post(_api_input(url=LOOPS_URL, image_alt_text='supplied by the caller'),
                  s.post, POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        file = db.session.get(File, s.post.image_id)
        assert file.source_url == 'https://cdn.loops.example/alt.720p.mp4'
        assert file.alt_text == 'supplied by the caller'  # `:666`, not `:637`

    def test_a_loops_url_with_an_unreadable_page_is_still_typed_as_video(
            self, db_session, http_mock):
        """`:633` false -> `:640`. Arc 633->640.

        `_unreadable_page` makes `parse_page` return False at
        app/utils.py:3195-3196, so `opengraph` is falsy and the whole File
        block `:634-639` is skipped -- but `:631` has ALREADY run, so the
        `post.type == POST_TYPE_VIDEO` assertion is a real positive witness of
        this arm (the generic arm would have written LINK for this url) rather
        than a bare absence. That is what keeps this out of false-witness
        mechanism 3; the positive control for 'a File can be built here at
        all' is the first test in this class, which builds one the same way.

        `post.url` is asserted only as a fact about `:640` having run; it is
        NOT a discriminator between this arm and the generic one, which writes
        the identical value at `:652`. See the class docstring.

        THIS TEST AND `test_a_site_relative_loops_thumbnail_is_not_turned_into_a_file`
        ASSERT THE SAME ABSENCE and are told apart by which mutation kills
        them, not by their assertions: here the input makes `:633` false;
        there `:633` is true and `:635` is false.
        """
        http_mock.head(LOOPS_URL).respond(200, headers={'Content-Type': 'text/html'})
        _unreadable_page(http_mock, LOOPS_URL)
        s = _seed()

        edit_post(_api_input(url=LOOPS_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_VIDEO
        assert s.post.url == LOOPS_URL  # `:640` ran; NOT an arm discriminator
        assert s.post.image_id is None
        assert File.query.count() == 0

    def test_a_site_relative_loops_thumbnail_is_not_turned_into_a_file(
            self, db_session, http_mock):
        """`:635` false -> `:640`. Arc 635->640.

        Here `opengraph` IS truthy and `:634` DID produce a filename -- the
        only thing stopping the File is the '/' prefix, which makes `:635`'s
        `not filename.startswith('/')` false. The og:image is a well-formed
        relative path ending '.jpg' on purpose: forcing `:635` true would then
        build a File whose `source_url` is '/thumbs/clip9.720p.mp4', a clean
        kill.

        TOLD APART FROM THE TEST ABOVE BY MUTATION, exactly as in
        TestPixelfedArm: forcing `:635` true makes THIS test grow a File,
        while forcing `:633` false makes the UNREADABLE-PAGE test lose
        nothing it had (it has none) but makes the FIRST test lose one.
        Neither mutation touches the other test. Task 9 must confirm both.

        `post.type` is still VIDEO because `:631` precedes the File block, so
        this is not a bare emptiness assertion either.
        """
        http_mock.head(LOOPS_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, LOOPS_URL, og_image='/thumbs/clip9.jpg',
                        og_title='Relative')
        s = _seed()

        edit_post(_api_input(url=LOOPS_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_VIDEO
        assert s.post.image_id is None
        assert File.query.count() == 0

    def test_every_jpg_in_a_loops_thumbnail_is_rewritten(self, db_session, http_mock):
        """`:636` IS GLOBAL: `str.replace` with no count argument replaces
        EVERY occurrence, including one in a directory segment rather than the
        extension.

        Same arcs as the first test (630->631, 633->634, 635->636); it closes
        no arc and no statement, and a test that closes neither earns its place
        only by a UNIQUE MUTANT KILL. This one has it.
        `.replace('.jpg', '.720p.mp4', 1)` is a viable mutant of `:636` that
        the first test CANNOT kill -- its filename holds a single '.jpg' -- and
        this test kills it, because the surviving second '.jpg' shows up in
        `File.source_url`.

        A SECOND EXPECTED SURVIVOR, recorded here because this is the only test
        in the class where the shape of the input could ever have caught it,
        and because it must survive the deletion of any report file. MEASURED,
        not reasoned: the four variants of `:636` were run over every og:image
        this class feeds, in this container::

            input             orig / guard              count=1            deleted
            thumb.jpg         thumb.720p.mp4 (same)     thumb.720p.mp4     thumb.jpg
            alt.jpg           alt.720p.mp4   (same)     alt.720p.mp4       alt.jpg
            /thumbs/clip9.jpg    -- blocked by `:635`, never reaches `:636` --
            a.jpg/b.jpg       a.720p.mp4/b.720p.mp4     a.720p.mp4/b.jpg   a.jpg/b.jpg

            guard   -> killed by: NOTHING (survives)
            count=1 -> killed by: this test
            deleted -> killed by: the first test, the fallback test, this test

        So a GUARD-ADDING mutant -- `if filename.endswith('.jpg'):` wrapped
        around `:636` -- SURVIVES EVERY TEST IN THIS CLASS. Every filename that
        clears `:635` here ends in '.jpg', including this test's
        'a.jpg/b.jpg', so the guard is always true and the mutant is
        behaviourally identical. Killing it needs a filename containing '.jpg'
        WITHOUT ending in it, such as 'a.jpg/b.png'. Task 9's mutation pass
        should record that mutant as EXPECTED SURVIVING rather than hunt a kill
        this round does not provide.

        (An earlier revision of this class claimed the first test killed the
        guard mutant and carried a sixth test, a '.png' thumbnail, to pin
        `:636`'s unconditionality. Both were wrong: the first test's input ends
        '.jpg' so the guard passes, and the '.png' test killed no mutant at all
        because a filename with no '.jpg' is invariant under all four variants.
        The test was removed and the claim is corrected here.)
        """
        http_mock.head(LOOPS_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, LOOPS_URL,
                        og_image='https://cdn.loops.example/a.jpg/b.jpg')
        s = _seed()

        edit_post(_api_input(url=LOOPS_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        file = db.session.get(File, s.post.image_id)
        assert file.source_url == \
            'https://cdn.loops.example/a.720p.mp4/b.720p.mp4'

    def test_a_jpg_that_is_not_the_extension_is_still_rewritten(
            self, db_session, http_mock):
        """`:636` IS UNCONDITIONAL, and this is the input that can show it.

        ADDED BY TASK 9's MUTATION PASS, and it closes exactly the survivor the
        test above registers: a GUARD-ADDING mutant of `:636` --
        `filename.replace('.jpg', '.720p.mp4') if filename.endswith('.jpg')
        else filename` -- survived every test in this class, because every
        filename that clears `:635` here ends in '.jpg' and the guard is
        therefore always true.

        'a.jpg/b.png' holds a '.jpg' WITHOUT ending in one, which is the shape
        the test above names as the one that would kill it. Original and
        guarded now disagree::

            original   -> https://cdn.loops.example/a.720p.mp4/b.png
            guarded    -> https://cdn.loops.example/a.jpg/b.png

        It is NOT the '.png' test that was removed from this class. That one
        fed a filename with NO '.jpg' at all and was invariant under all four
        variants of `:636` -- false-witness mechanism 4. This filename contains
        a '.jpg' that `:636` must rewrite, so the stored value differs.

        `post.type == POST_TYPE_VIDEO` is asserted alongside, because for this
        url the generic arm would have written LINK (see the class docstring):
        the assertion keeps the test pinned to `:630`'s arm rather than merely
        to a string.
        """
        http_mock.head(LOOPS_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, LOOPS_URL,
                        og_image='https://cdn.loops.example/a.jpg/b.png')
        s = _seed()

        edit_post(_api_input(url=LOOPS_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_VIDEO
        file = db.session.get(File, s.post.image_id)
        assert file.source_url == 'https://cdn.loops.example/a.720p.mp4/b.png'

    def test_a_readable_loops_page_with_no_og_image_builds_no_file(
            self, db_session, http_mock):
        """`:633`'s SECOND conjunct is the ONLY thing that decides here.

        ADDED BY TASK 9's MUTATION PASS. The same construction, and the same
        finding, as
        `TestPixelfedArm.test_a_readable_page_with_no_og_image_builds_no_file`
        -- see that docstring for why an empty-tag page would not do. Replacing
        `:633`'s second conjunct with `True` changed nothing any test in this
        class could see; with a truthy `opengraph` carrying no og:image, `:634`
        yields None and `:635`'s `filename.startswith` raises AttributeError
        under the mutant.

        `post.type == POST_TYPE_VIDEO` is the positive witness that this arm
        ran at all (`:631` precedes the File block), so the two absence
        assertions are not standing alone.
        """
        http_mock.head(LOOPS_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, LOOPS_URL, og_title='Only a title')
        s = _seed()

        edit_post(_api_input(url=LOOPS_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_VIDEO
        assert s.post.image_id is None
        assert File.query.count() == 0


GENERIC_URL = 'https://news.example.com/article'
THUMB_URL = 'https://cdn.example.com/lead.png'


def _png_bytes():
    """Genuine PNG bytes, small.

    `url_to_thumbnail_file` opens what it downloads with `Image.open`
    (app/utils.py:3085) and re-encodes it twice (`:3101`, `:3111`), so a
    placeholder string would raise `PIL.UnidentifiedImageError` out of
    `edit_post` -- `url_to_thumbnail_file` catches nothing around that block --
    rather than producing a File.
    """
    buf = BytesIO()
    Image.new('RGB', (8, 8), (7, 8, 9)).save(buf, format='PNG')
    return buf.getvalue()


def _written_media(tmp_path):
    """Every regular file `url_to_thumbnail_file` left under `tmp_path`.

    `chdir_upload` has moved the working directory to `tmp_path`, so
    app/utils.py:3065's relative `'app/static/media/posts/' + ...` lands here
    instead of in the repository.
    """
    root = tmp_path / 'app' / 'static' / 'media' / 'posts'
    return sorted(p for p in root.rglob('*') if p.is_file()) if root.exists() else []


class TestGenericOpengraphArm:
    """`:641-657` -- the `else` arm of the `:601`/`:619`/`:630`/`:641` chain,
    and the only one that FETCHES the thumbnail rather than merely recording
    its url.

    THE HARNESS IS THE INVERTED ONE, as in TestPixelfedArm and TestLoopsArm: a
    HEAD reporting `text/html` so `is_image_url` is False and `:601` does not
    take the chain, plus a GET route because `:642` WILL call
    `opengraph_parse`. tests/README.md fact 229 point 3 records the opposite
    rule and is correct only for tests/test_shared_post_upload.py.

    `chdir_upload` IS REQUIRED HERE EVEN THOUGH NOTHING IS UPLOADED. `:646`'s
    `url_to_thumbnail_file` writes the downloaded bytes and both re-encoded
    thumbnails to a RELATIVE directory. Read out of the file rather than
    recalled::

        3061	            new_filename = gibberish(15)
        3062	            if store_files_in_s3():
        3063	                directory = 'app/static/tmp'
        3064	            else:
        3065	                directory = 'app/static/media/posts/' + new_filename[0:2] + '/' + new_filename[2:4]
        3066	            ensure_directory_exists(directory)
        3067	            temp_file_path = os.path.join(directory, new_filename + file_extension)

    `store_files_in_s3()` (app/utils.py:4317-4319) is False under `TestConfig`
    -- measured in this container, along with `current_app.debug` False and
    `MEDIA_IMAGE_MEDIUM_FORMAT` 'WEBP' -- so `:3065` is the live branch and the
    path is relative to the working directory, which without the fixture is the
    bind-mounted repository root. Every file lands under `tmp_path` instead, and
    `test_a_generic_url_downloads_the_opengraph_thumbnail` asserts they are
    there rather than trusting the redirect.

    THREE GETs ARE IN PLAY ACROSS THIS CLASS, not one, and `http_mock`'s
    `assert_all_called=True` (tests/conftest.py:342) makes the count part of
    every witness:

      - the HEAD on the submitted url, from `:601`'s `is_image_url`;
      - the GET on the submitted url, from `:642`'s `opengraph_parse`;
      - the GET on the OG:IMAGE url, from `:646`'s `url_to_thumbnail_file`
        (app/utils.py:3015), which goes through `httpx_client.get` DIRECTLY
        rather than through `get_request`.

    A test that does not reach the third must not register it, and a test that
    does must -- which is what tells
    `test_a_site_relative_og_image_skips_the_download` (never reaches it) apart
    from `test_a_thumbnail_that_is_not_an_image_yields_no_file` (reaches it and
    is refused there).

    `is_invalid_get_request_uri` (app/utils.py:5493-5536) does NOT block either
    host. Measured in this container under `TestConfig`::

        invalid_uri https://cdn.example.com/lead.png False
        invalid_uri https://news.example.com/article False

    -- `furl` finds a host and an https scheme, and `socket.getaddrinfo` either
    resolves to a global address or fails, and a resolution failure returns
    False by `:5521-5522`'s deliberate fail-open. So `:3011`'s early return is
    not what any test here relies on.

    THIS ARM'S FILE IS BUILT DIFFERENTLY, AND THAT IS THE DISCRIMINATOR --
    with a CORRECTION to the brief, read off app/utils.py rather than recalled::

        3155	            return File(file_path=thumbnail_512_url, thumbnail_width=thumbnail_width, width=thumbnail_512_width,
        3156	                        height=thumbnail_512_height,
        3157	                        thumbnail_height=thumbnail_height, thumbnail_path=thumbnail_170_url,
        3158	                        source_url=filename)

    The brief predicted `file.source_url is None` on this arm. IT IS NOT:
    `:3158` sets `source_url=filename`, the same column `:625` and `:637` set,
    so source_url alone CANNOT tell this arm from the other two (false-witness
    mechanism 1). What can is `file_path`, `thumbnail_path`, `width`, `height`,
    `thumbnail_width` and `thumbnail_height`, ALL of which `:625` and `:637`
    leave NULL because their `File(...)` calls pass neither. `file_path` is
    asserted in the first test and is this class's signature.

    MUTATION VERIFICATION, RUN RATHER THAN ARGUED. Four of the eight tests here
    close no arc and no statement, and the standing rule is that such a test
    earns its place only by a UNIQUE MUTANT KILL confirmed by running the
    mutant. All five candidate mutants -- `:644`'s `or` fallback removed, and
    each of `:654`'s four disjuncts removed in turn -- were applied to
    `app/shared/post.py` one at a time and run against the whole of this file
    (41 tests) and then against every other tests/test_shared_post_*.py file
    (308 tests). Measured, for each of the five::

        this file:      1 failed, 40 passed   (the failure being the test that claims it)
        the other six:  308 passed

    So each of the five is killed by exactly one test in 349, and 'no other test
    in the suite' below is a measurement rather than a hope. These five rows are
    summaries without pasted pytest output, so the mutation pass should treat
    them as LEADS TO VERIFY rather than as banked results.

    THE RESTORE CHECK IS `git diff --quiet -- app/`, NOT `wc -l`. Every mutant
    here was a SINGLE-LINE REPLACEMENT, which leaves `wc -l app/shared/post.py`
    reading 1193 whether the mutant is present or not -- so the line-count
    tripwire this campaign relies on is INOPERATIVE against exactly the kind of
    edit a mutation run makes. It was left inoperative for about 26 minutes
    during this task, with production code mutated across turn boundaries, and
    nothing would have caught it. Restore from a pristine copy immediately after
    each run, and gate on::

        git diff --quiet -- app/ && echo CLEAN

    before stopping, reporting or committing. `wc -l` is a useful second signal
    and not a substitute.
    """

    def test_a_generic_url_downloads_the_opengraph_thumbnail(
            self, db_session, http_mock, chdir_upload):
        """`:643` true -> `:644`; `:645` true -> `:646`; `:647` true -> `:648`,
        `:649`, `:650`; then `:652`, `:654` false -> `:657`.
        Arcs 643->644, 645->646, 647->648; statements 644-650.

        FOUR witnesses, and the first two are the ones that are unique to this
        arm:

          - `file.file_path` is a real path ending '_512.webp'. Only
            app/utils.py:3155 populates that column on any of the four arms of
            the `:601` chain (class docstring), and only `:646` calls it. The
            '_512' comes from app/utils.py:3110 and the '.webp' from `:3096`
            via `MEDIA_IMAGE_MEDIUM_FORMAT`, measured 'WEBP' in this container.
          - THREE files exist under `chdir_upload`. app/utils.py:3069-3070
            saves the downloaded PNG, `:3101` saves the 170px WEBP over a
            DIFFERENT path (`:3097` re-points `temp_file_path` to the .webp
            name, leaving the .png behind), and `:3111` saves the 512px WEBP.
            Neither `:619`'s nor `:630`'s arm writes anything -- they only
            record a url -- so disk output separates this arm from those two.
            It does NOT separate it from `:601`'s, which writes through
            `make_image_sizes` at `:614`/`:616`; that arm is ruled out instead
            by the `if`/`elif` chain, which cannot run two arms in one call,
            and by `post.type` being LINK rather than IMAGE.
            This assertion is also what proves `chdir_upload` redirected the
            writes rather than merely being present: the files are found under
            `tmp_path`, and the task report records that the repository's own
            app/static/media held the same zero files afterwards as before.
          - `post.type` is LINK, written by `:657`, because `:654`'s four
            disjuncts are all false for this url -- see the video tests below
            for the measurements.
          - `file.source_url` is the OG:IMAGE url, not the post url. `:602`'s
            File would carry the post url instead. NOT an arm discriminator
            against `:625`/`:637`, which set the same column; see the class
            docstring.

        `post.url` is asserted as a fact about `:652` having run. It is NOT a
        discriminator: `fixup_url` returns `(url, url)` for this host, so
        `:640`'s `post.url = url` writes the identical value (module docstring).

        A CORRECTION TO THE BRIEF, MEASURED. The brief's fourth assertion was
        `file.alt_text == 'A headline'`. It is `''`, for exactly the reason
        Tasks 5 and 6 found on the pixelfed and loops arms, and the reason
        survives the difference in how this arm's File is built. Read out of the
        file::

            647	                    if file:
            648	                        file.alt_text = shorten_string(opengraph.get('og:title'), 295)
            649	                        post.image = file
            650	                        db.session.add(file)
            ...
            663	    if url and post.image:
            664	        file = File.query.get(post.image_id)
            665	        if file:
            666	            file.alt_text = image_alt_text

        and, for the default::

            262	        image_alt_text = input['image_alt_text'] if 'image_alt_text' in input else ''

        `:649` sets `post.image`, so `:663` is true; `File.query.get` autoflushes,
        which is what gives `post.image_id` a value at `:664`; and `:666`
        overwrites `:648`'s value unconditionally with `''`. That `:648` writes
        the attribute on an already-constructed object rather than through a
        `File(...)` keyword makes no difference -- both reach the same column
        before `:666` runs. So alt_text on this row witnesses `:666`, never
        `:648`. `test_a_generic_url_falls_back_to_og_image_url` is the
        same-mechanism positive control (false-witness mechanism 3): it passes a
        distinctive `image_alt_text` and gets it back.

        AN EXPECTED SURVIVOR, NOT A HOLE. Because `:666` overwrites the column,
        a mutation of `:648`'s ARGUMENT -- `shorten_string(opengraph.get(
        'og:title'), 295)` -- survives every test in this class, exactly as on
        the other two arms: no assertion anywhere in this file is taken before
        `:663` runs, so no assertion can see what `:648` wrote. Task 9's
        mutation pass should record that ARGUMENT mutation as expected rather
        than chase it. Closing it would need either an assertion taken before
        `:663` or a change to `app/`, and neither is in this round's scope.
        (This says nothing about mutations that remove `:648` as a whole, whose
        fate depends on the mutation scheme used and is not predicted here.)
        """
        http_mock.head(GENERIC_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, GENERIC_URL, og_image=THUMB_URL,
                        og_title='A headline')
        http_mock.get(THUMB_URL).respond(200, headers={'Content-Type': 'image/png'},
                                         content=_png_bytes())
        s = _seed()

        edit_post(_api_input(url=GENERIC_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.url == GENERIC_URL  # `:652`; NOT an arm discriminator
        assert s.post.type == POST_TYPE_LINK  # `:657`
        file = db.session.get(File, s.post.image_id)
        assert file.file_path.endswith('_512.webp')  # app/utils.py:3155, this arm only
        assert file.thumbnail_path is not None  # app/utils.py:3157, this arm only
        assert file.source_url == THUMB_URL  # NOT None, app/utils.py:3158
        assert file.alt_text == ''  # `:666` overwrote `:648`'s 'A headline'
        assert len(_written_media(chdir_upload)) == 3  # png + 170 webp + 512 webp

    def test_a_generic_url_falls_back_to_og_image_url(
            self, db_session, http_mock, chdir_upload):
        """`:643`'s SECOND disjunct alone, and `:644`'s `or` fallback.

        The page carries `og:image:url` and no `og:image`, so
        `opengraph.get('og:image', '') != ''` is False and the block is
        admitted only by the second disjunct; `:644`'s
        `opengraph.get('og:image') or opengraph.get('og:image:url')` then gets
        None from its left operand and falls through to the right.
        'og:image:url' is one of `parse_page`'s `tags_to_search`
        (app/utils.py:3181), so it really does reach the dict.

        CLOSES NO NEW ARC -- 643->644 is already closed by the test above -- so
        it earns its place by a UNIQUE MUTANT KILL, verified by running the
        mutant rather than argued. Dropping `:644`'s fallback to
        `filename = opengraph.get('og:image')` leaves `filename` None here and
        `:645`'s `filename.startswith('/')` raises AttributeError, while the
        test above is untouched because its page carries 'og:image'. Measured in
        this container over the whole 41-test file: '1 failed, 40 passed, 1
        error', the failure and the error both being THIS test -- the error is
        respx's teardown noticing that the thumbnail GET registered below was
        never reached, because the AttributeError fires first, which is a second
        independent signal of the same kill.

        Without this test the two disjuncts of `:643` move only in
        lockstep and a swap between them is undetectable (false-witness
        mechanism 5).

        ALSO THE POSITIVE CONTROL for the `alt_text == ''` assertion above.
        `og:title` is 'Fallback' here while `image_alt_text` is a different,
        distinctive string, and the row comes back carrying the
        `image_alt_text` -- which shows `:666` is what writes that column on
        this path and that the `''` above is a write rather than an absence
        (false-witness mechanism 3).
        """
        http_mock.head(GENERIC_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, GENERIC_URL, og_image_url=THUMB_URL,
                        og_title='Fallback')
        http_mock.get(THUMB_URL).respond(200, headers={'Content-Type': 'image/png'},
                                         content=_png_bytes())
        s = _seed()

        edit_post(_api_input(url=GENERIC_URL, image_alt_text='supplied by the caller'),
                  s.post, POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        file = db.session.get(File, s.post.image_id)
        assert file.source_url == THUMB_URL
        assert file.file_path.endswith('_512.webp')  # still THIS arm's File
        assert file.alt_text == 'supplied by the caller'  # `:666`, not `:648`

    def test_a_site_relative_og_image_skips_the_download(
            self, db_session, http_mock, chdir_upload):
        """`:645` false -> `:652`. Arc 645->652.

        `opengraph` IS truthy and `:644` DID produce a filename; the only thing
        stopping the File is the '/' prefix, which makes `:645`'s
        `not filename.startswith('/')` false.

        NO GET IS REGISTERED FOR THE THUMBNAIL, and that absence is itself part
        of the witness. `url_to_thumbnail_file` is never called, so a registered
        route would go unreached and `assert_all_called=True` would fail the
        test at teardown; had `:645` been inverted, the call WOULD happen and
        respx would raise `AllMockedAssertionError` on the unmatched request.
        The test fails loudly either way round, which is what separates it from
        `test_a_thumbnail_that_is_not_an_image_yields_no_file` below, where the
        GET is reached and registered.

        NOTHING IS WRITTEN TO DISK either, and that is the same claim in a
        second currency: app/utils.py:3069-3070 is downstream of the call that
        never happens. The positive control for both assertions is the first
        test in this class, which writes three files through the same mechanism
        (false-witness mechanism 3).
        """
        http_mock.head(GENERIC_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, GENERIC_URL, og_image='/assets/lead.png')
        s = _seed()

        edit_post(_api_input(url=GENERIC_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.image_id is None
        assert File.query.count() == 0
        assert _written_media(chdir_upload) == []
        assert s.post.url == GENERIC_URL  # `:652` ran

    def test_a_thumbnail_that_is_not_an_image_yields_no_file(
            self, db_session, http_mock, chdir_upload):
        """`:647` false -> `:652`. Arc 647->652.

        `url_to_thumbnail_file` returns None when the response's content type
        does not start with 'image'. Read out of the file rather than
        recalled::

            3019	    if response.status_code == 200:
            3020	        content_type = response.headers.get('content-type')
            3021	        if content_type and content_type.startswith('image'):

        -- a 'text/plain' body takes neither the `:3021` branch nor any
        `return`, so the function falls off its end and yields None. That is the
        cheapest of the five None paths (`:3011` needs a uri the guard rejects,
        `:3016` needs a transport exception, `:3019` needs a non-200, `:3056`
        needs an unsanitizable SVG), and it needs no extra config.

        THE GET IS REACHED HERE, unlike in the test above, and IS registered.
        `assert_all_called=True` is what proves it ran: this test and that one
        assert the same absence and are told apart by the request count and by
        which mutation kills them, not by their assertions. Forcing `:647` true
        crashes THIS test on `None.alt_text`; forcing `:645` true makes the test
        above attempt an unmocked GET. Neither touches the other.

        NOTHING REACHES DISK: app/utils.py:3069 is inside the `:3021` block that
        this content type never enters.
        """
        http_mock.head(GENERIC_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, GENERIC_URL, og_image=THUMB_URL)
        http_mock.get(THUMB_URL).respond(200, headers={'Content-Type': 'text/plain'},
                                         text='not an image')
        s = _seed()

        edit_post(_api_input(url=GENERIC_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.image_id is None
        assert File.query.count() == 0
        assert _written_media(chdir_upload) == []

    def test_a_readable_generic_page_with_no_og_image_downloads_nothing(
            self, db_session, http_mock, chdir_upload):
        """`:643`'s SECOND conjunct is the ONLY thing that decides here.

        ADDED BY TASK 9's MUTATION PASS. The third of the set -- see
        `TestPixelfedArm.test_a_readable_page_with_no_og_image_builds_no_file`
        for the construction and for why an empty-tag page would not do.
        Replacing `:643`'s second conjunct with `True` changed nothing any test
        could see; here `opengraph` is truthy and carries no og:image, so
        `:644` yields None and `:645`'s `filename.startswith` raises
        AttributeError under the mutant.

        NO GET IS REGISTERED FOR A THUMBNAIL, and that absence is part of the
        witness in the same way as in
        `test_a_site_relative_og_image_skips_the_download`: `:646` is never
        reached, so a registered route would go unreached and
        `assert_all_called=True` would fail this test at teardown.

        `post.url` and `post.type` are asserted because `:652` and `:657` run
        on this path regardless of the File block, which keeps this off a bare
        emptiness assertion. LINK is the right expectation: this url satisfies
        none of `:654`'s four disjuncts (see
        `TestVideoHostingSiteArm` and the `is_video_url` reading in
        `TestLoopsArm`'s docstring).

        THE SUBMITTED TYPE IS `POST_TYPE_ARTICLE`, AND THAT IS DELIBERATE.
        Every other test in this class submits `POST_TYPE_LINK`, which `:398`'s
        `post.type = type` has already written by the time `:657` runs -- so
        `assert s.post.type == POST_TYPE_LINK` was the submitted value
        surviving, false-witness mechanism 1, and Task 9's mutation pass
        measured it: DELETING `:657` outright survived all 354 tests. Submitting
        ARTICLE makes LINK a value only `:657` can have written, since `:403` is
        false here (`_seed` leaves `post.url` None) and no other line in the
        generic arm writes it.
        """
        http_mock.head(GENERIC_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, GENERIC_URL, og_title='Only a title')
        s = _seed()

        edit_post(_api_input(url=GENERIC_URL), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_LINK
        assert s.post.url == GENERIC_URL
        assert s.post.image_id is None
        assert File.query.count() == 0
        assert _written_media(chdir_upload) == []

    # ------------------------------------------------------------------
    # `:654`'s four disjuncts. Read out of the file rather than recalled::
    #
    #     654	            if is_video_url(url) or url.endswith('.mp4') or url.endswith('.webm') or is_video_hosting_site(embed_url):
    #     655	                post.type = POST_TYPE_VIDEO
    #     656	            else:
    #     657	                post.type = POST_TYPE_LINK
    #
    # COVERAGE SCORES THIS AS ONE ARC PAIR, so the four operands are
    # indistinguishable to it and only mutation can separate them. Each test
    # below feeds an input making EXACTLY ONE disjunct true, measured in this
    # container before the tests were written::
    #
    #   url                                    d1 is_video_url  d2 .mp4  d3 .webm  d4 hosting
    #   'https://cdn.example.com/MOVIE.MP4'    True            False    False     False
    #   'https://x.example/clip#b.mp4'         False           True     False     False
    #   'https://y.example/clip#b.webm'        False           False    True      False
    #   'https://vimeo.com/12345'              False           False    False     True
    #   'https://cdn.example.com/movie.mp4'    True            True     False     False   <- LOCKSTEP, not used
    #   'https://cdn.example.com/clip.webm'    True            False    True      False   <- LOCKSTEP, not used
    #   'https://cdn.example.com/clip.mov'     False           False    False     False   <- see D476 below
    #
    # The two lockstep rows are why the brief's proposed 'movie.mp4' input is
    # NOT used: with d1 and d2 both true, deleting either one alone changes
    # nothing and the mutant survives (false-witness mechanism 5).
    #
    # d1 AND d2 DO NOT SUBSUME EACH OTHER, and the table above is the proof in
    # both directions. `is_video_url` (app/utils.py:294-313) LOWERCASES and
    # tests `urlparse(url).path`; `url.endswith('.mp4')` tests the RAW string
    # case-sensitively, including any query or fragment. So an uppercase
    # extension makes only d1 true, and a fragment -- which `urlparse` keeps
    # OUT of `.path` -- makes only d2 true. (A trailing query, e.g.
    # 'movie.mp4?v=2', also gives d1 alone: measured True/False.)
    #
    # D476, THIRD SITE. `:654` admits '.mp4' and '.webm' and NOT '.mov',
    # exactly as `is_video_url` does (app/utils.py:295), while `edit_post`'s own
    # upload block accepts '.mov'. Read out of the file rather than recalled --
    # and this paste is itself a CORRECTION, of a first draft of this comment
    # that cited `:465` for the wrong line::
    #
    #     463	        allowed_extensions = ['.gif', '.jpg', '.jpeg', '.png', '.webp', '.heic', '.mpo', '.avif', '.svg']
    #     464	        if type == POST_TYPE_VIDEO and can_upload_video():
    #     465	            allowed_extensions.extend(['.mp4', '.webm', '.mov'])
    #     466	        file_ext = os.path.splitext(uploaded_file.filename)[1]
    #     467	        if file_ext.lower() not in allowed_extensions:
    #
    # `:463`'s list does NOT hold '.mov'; `:465` adds it, and only when the
    # submitted type is POST_TYPE_VIDEO and `can_upload_video()` is true. The
    # gate itself is `:467`. So '.mov' is a permitted upload extension that
    # `:654` then refuses to recognise as video -- measured: all four disjuncts
    # False for 'https://cdn.example.com/clip.mov', so `:657` writes
    # POST_TYPE_LINK.
    #
    # WHICH SUBMISSION ACTUALLY REACHES THIS SITE is worth stating exactly,
    # because D476's registered site is UPSTREAM of it. An UPLOADED '.mov'
    # never gets here: `:513`'s `not is_video_url(final_place)` is true for
    # '.mov' (the same missing extension), so `:514`'s `Image.open` meets a
    # QuickTime container and raises `UnidentifiedImageError` out of
    # `edit_post` -- that crash IS D476 as registered. The path that reaches
    # `:654` is a PLAIN URL SUBMISSION of a '.mov' with no uploaded_file, where
    # nothing decodes the bytes and the only consequence is the wrong
    # `post.type`. Same missing equivalence class, second consequence.
    #
    # RECORDED AGAINST D476, NOT FIXED: a missing equivalence class is
    # invisible to both coverage and mutation, which is what made D476 worth
    # registering, and fixing it would change app/ which this round may not do.
    # No test here feeds a '.mov'; one would pin today's wrong answer as
    # correct, and a test whose whole content is a defect is worse than the
    # note.
    #
    # d4 IS ISOLABLE BY VALUE BUT ITS ARGUMENT IS NOT. `is_video_hosting_site`
    # is the only operand reading `embed_url` rather than `url`, and
    # `fixup_url` (app/utils.py:3311-3312) opens `thumbnail_url = embed_url =
    # url` and diverges only for youtube domains and for a peertube url whose
    # last 25 characters begin '/w/' (app/utils.py:3330) -- neither of which any
    # url in this file is. So `embed_url == url` throughout, the vimeo test
    # below DOES kill a mutant that deletes the operand, but a mutant that
    # merely swaps its ARGUMENT to `is_video_hosting_site(url)` is EQUIVALENT
    # here and cannot be killed from this file. Task 9 should record that one as
    # equivalent rather than hunt a kill for it.
    # ------------------------------------------------------------------

    def _drive_video(self, http_mock, url):
        """HEAD text/html + an unreadable page, then submit `url` as a LINK.

        `_unreadable_page` makes `parse_page` return False
        (app/utils.py:3195-3196) so `:643` is false and no File is built -- the
        thumbnail is irrelevant to what these tests witness, and skipping it
        keeps the og:image GET out of the request count.
        """
        http_mock.head(url).respond(200, headers={'Content-Type': 'text/html'})
        _unreadable_page(http_mock, url)
        s = _seed()
        edit_post(_api_input(url=url), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)
        db.session.refresh(s.post)
        return s.post

    def test_an_uppercase_video_extension_is_typed_as_video(
            self, db_session, http_mock, chdir_upload):
        """`:654`'s FIRST disjunct alone -> `:655`.

        CLOSES NO ARC AND NO STATEMENT. An earlier revision of this docstring
        claimed it closed `655` and `654->655` and called the brief wrong for
        saying otherwise. THAT CLAIM WAS RETRACTED; the brief was right.

        The mistake was one of MEASUREMENT SCOPE, and it is worth stating
        plainly because the correct fact was already in this very class. The
        baseline was taken FILE-SCOPED -- `./run_tests.sh
        tests/test_shared_post_url.py --cov=app.shared.post` -- over 33 tests.
        The round's authoritative baseline is SUITE-SCOPED, taken at `544a7eb1`
        over every tests/test_shared_post_*.py file, and in it neither `655` nor
        `654->655` is missing:
        docs/superpowers/plans/2026-09-12-coverage-post-e2-39.md:85 lists the
        missing statements in this region as `644 645 646 647 648 649 650 661`,
        with no 655, and `:95` lists the missing arcs with no `654->655`;
        docs/superpowers/specs/2026-09-12-coverage-post-e2-39-design.md:81 says
        so in prose. A statement covered by another FILE is not missing from
        the module, however it looks when that file is measured alone.

        What already covers it is `tests/test_shared_post_edit.py:815-825`
        (`test_web_branch_takes_the_video_url_for_a_video_post`): its HEAD
        answers `video/mp4`, which is not an image type, so `:601` is false and
        the `:641` else arm is taken; its GET answers `html=''`, so `:643` is
        false; and `is_video_url('https://example.com/clip.mp4')` is true, so
        `:654` reaches `:655`. The block comment above this method already said
        that file reaches `655` -- the two statements were never reconciled,
        which is the actual failure here, not the file-scoped number.

        THE TEST STAYS, because what it witnesses is not coverage. `:654` is a
        four-disjunct compound scored as ONE arc pair, so the covering test
        above cannot distinguish the operands: its url makes d1 AND d2 true at
        once. This one makes d1 true ALONE, which is what lets a mutant that
        deletes d1 be killed.

        'MOVIE.MP4' is uppercase on purpose. `is_video_url` lowercases the
        parsed path (app/utils.py:312) so d1 is True, while `url.endswith(
        '.mp4')` is case-sensitive so d2 is False, and d3/d4 are False --
        exactly one disjunct carries the branch. `POST_TYPE_VIDEO` is a positive
        witness rather than a default: `:398` wrote LINK from the submitted
        `type`, and `:661` is unreachable because `:565` is true here.
        """
        post = self._drive_video(http_mock, 'https://cdn.example.com/MOVIE.MP4')
        assert post.type == POST_TYPE_VIDEO

    def test_a_raw_mp4_suffix_outside_the_path_is_typed_as_video(
            self, db_session, http_mock, chdir_upload):
        """`:654`'s SECOND disjunct alone -> `:655`.

        CLOSES NO ARC AND NO STATEMENT -- the test above closes 654->655 -- so
        it earns its place by a UNIQUE MUTANT KILL, verified by running the
        mutant. Deleting `url.endswith('.mp4') or` from `:654` fails THIS test
        and no other in the suite; the uppercase test is untouched because its
        d2 is already False.

        The fragment is what separates the operands: `urlparse` puts '#b.mp4'
        in `.fragment`, leaving `.path` as '/clip', so `is_video_url` is False
        while the raw string still ends '.mp4'. Measured before writing.
        """
        post = self._drive_video(http_mock, 'https://x.example/clip#b.mp4')
        assert post.type == POST_TYPE_VIDEO

    def test_a_raw_webm_suffix_outside_the_path_is_typed_as_video(
            self, db_session, http_mock, chdir_upload):
        """`:654`'s THIRD disjunct alone -> `:655`.

        CLOSES NO ARC AND NO STATEMENT; earns its place by a unique mutant
        kill, verified by running the mutant. Deleting `url.endswith('.webm')
        or` from `:654` fails THIS test and no other in the suite -- no other
        test in this file feeds a url whose RAW string ends '.webm' while its
        parsed path does not.

        Same fragment mechanism as the test above, and the same reason it is
        not redundant with it: d2 and d3 are separate operands, and an input
        making both false-but-one would leave a swap between them undetectable
        (false-witness mechanism 5).
        """
        post = self._drive_video(http_mock, 'https://y.example/clip#b.webm')
        assert post.type == POST_TYPE_VIDEO

    def test_a_vimeo_url_is_typed_as_video_by_the_hosting_site_disjunct(
            self, db_session, http_mock, chdir_upload):
        """`:654`'s FOURTH disjunct alone -> `:655`.

        CLOSES NO ARC AND NO STATEMENT; earns its place by a unique mutant
        kill, verified by running the mutant. Deleting `or
        is_video_hosting_site(embed_url)` from `:654` fails THIS test and no
        other in the suite. It is the only test anywhere in this file that
        reaches `is_video_hosting_site` from `:654` rather than from `:660`:
        `TestVideoHostingSiteArm` drives `:660` with `from_scratch=False`, which
        closes `:565` and makes `:654` unreachable in the same call.

        'https://vimeo.com' is one of the six prefixes at app/utils.py:319-321,
        and the url holds no '.mp4'/'.webm' in any form, so d1, d2 and d3 are
        all False -- measured before writing.

        THE OPERAND'S ARGUMENT IS NOT WITNESSED, and this is deliberate rather
        than an omission: `embed_url == url` for this host (see the block
        comment above), so swapping `is_video_hosting_site(embed_url)` for
        `is_video_hosting_site(url)` is an EQUIVALENT mutant from this file.
        """
        post = self._drive_video(http_mock, 'https://vimeo.com/12345')
        assert post.type == POST_TYPE_VIDEO


BARE_PIXELFED_URL = 'https://pixelfed.social'


def _source_of(warning):
    """The stripped SOURCE LINE a recorded warning is attributed to.

    A warning carries `filename` and `lineno`; asserting on the NUMBER hard-codes
    a production line and rots the moment anything above it moves. This reads the
    line back instead, so the assertion pins WHICH STATEMENT raised the warning by
    what that statement says. `checkcache` first, because `linecache` is a
    process-wide cache that coverage and the warnings machinery have already
    populated from this same file.
    """
    linecache.checkcache(warning.filename)
    return linecache.getline(warning.filename, warning.lineno).strip()


class TestPollAndEventTail:
    """`:662-703` -- five arms whose LINES all run and whose branches do not.

    This region contributed ZERO missing statements and five missing arcs to
    sub-project 39's baseline, so nothing here is about reaching new code; it
    is entirely about reaching the other side of decisions the rest of the
    suite only ever takes one way. Measured suite-scoped at 7e320fb1 over all
    seven `tests/test_shared_post_*.py` files (349 passed), the whole of
    `app/shared/post.py` was `missing_lines []` and
    `missing_branches [(665, 668), (673, 682), (675, 674), (683, 686),
    (699, 703)]`. A FILE-SCOPED run over this file alone reports ~20
    statements missing in the `670-694` window; they are not missing, they are
    covered by `tests/test_shared_post_edit.py`, and a file-scoped run cannot
    see that. Nothing in this class was concluded from one.

    `:673`'s FALSE ARM IS UNREACHABLE and `app/shared/post.py:673` carries a
    `# pragma: no branch` saying so and pointing here. THE PROOF, read out of
    the file rather than recalled -- `poll_data` is assigned at exactly four
    lines (`grep -n poll_data app/shared/post.py` gives :300, :314, :349,
    :357 as the only assignments), it is a LOCAL of `edit_post` and not a
    parameter, so no caller and no direct test call can hand `:673` a dict
    without the key. The API branch::

        299	        # Parse poll data from API
        300	        poll_data = input.get('poll', None)
        301	        if poll_data:
        302	            # Extract all poll fields
        303	            parsed_poll = {
        304	                'mode': poll_data.get('mode', 'single'),
        305	                'local_only': poll_data.get('local_only', False),
        306	                'choices': poll_data.get('choices', [])
        307	            }
        ...
        314	            poll_data = parsed_poll

    -- a caller-supplied `poll` that is TRUTHY is replaced wholesale by
    `parsed_poll`, which always carries `'choices'` (`:306`); a `poll` that is
    FALSY leaves `poll_data` falsy and `:669`'s `and poll_data` closes the
    whole block before `:673` is reached. There is no third outcome: a truthy
    non-mapping dies at `:304`'s `.get` with AttributeError, upstream of
    `:673`. The web branch::

        343	        if type == POST_TYPE_POLL:
        ...
        349	            poll_data = {
        350	                'mode': input.mode.data,
        351	                'local_only': input.local_only.data,
        352	                'choices': poll_choices
        353	            }
        354	            if input.finish_in:
        355	                poll_data['end_poll'] = end_poll_date(input.finish_in.data)
        356	        else:
        357	            poll_data = None

    -- `'choices'` at `:352` unconditionally when `type` is POLL, and `None`
    otherwise, which again closes `:669`. And `:315` is a bare `else:`, so any
    `src` that is not SRC_API takes the web branch; there is no third entry
    point.

    REGISTERED, NOT DELETED. Removing a guard because no CURRENT caller can
    trip it is a behaviour change this round has no standing to make, and
    `:673` describes a `poll_data` shape a future caller could easily produce.
    The pragma is the same disposition this repository already uses at
    `app/shared/tasks/pages.py:270`, `:312`, `:333` and
    `app/shared/tasks/follows.py:188`.
    """

    def test_an_unflushed_thumbnail_is_not_found_by_its_own_foreign_key(
            self, db_session, http_mock):
        """`:665` FALSE -> `:668`. Arc 665->668.

        THE BRIEF'S MECHANISM FOR THIS ARC WAS WRONG TWICE, and what follows
        is measured. The brief guessed `File.query.get(None)` on an unflushed
        `post.image_id`; the controller's amendment then reported Task 7's
        finding that `File.query.get`'s own autoflush gives `post.image_id` a
        value, so `:665` is TRUE on the opengraph arms, and told this task to
        find the real mechanism. BOTH are right about their own case, and the
        reconciliation is the url's PATH:

          - `post.image = file` (`:626`) is a RELATIONSHIP write. `post.image`
            is truthy the instant it is assigned, but `post.image_id` is only
            synced at FLUSH. The pair is `Post.image_id` at app/models.py:1705
            and `Post.image` at app/models.py:1764 -- re-derived with numbered
            output, because `Community` carries a NEAR-IDENTICAL pair at
            app/models.py:559 and :638 (`class Community` opens at :555,
            `class Post` at :1700) and an earlier revision of this docstring
            cited the Community lines by mistake. The two differ even in
            loader strategy: `Post.image` is `lazy='joined'`, `Community.image`
            is not.
          - `:659` `post.calculate_cross_posts(url_changed=url_changed)` is
            the only thing between `:626` and `:663` that can emit SQL, and
            SQL is what autoflushes. For a url WITH a path it queries, the
            autoflush fires, `post.image_id` is set, and `:665` is true --
            that is Task 7's case, and it is the test immediately below.
          - For a url that is a BARE DOMAIN it returns first, at
            app/models.py:2362 `if self.url.count('/') < 3 or ...: return`,
            having touched only already-loaded attributes -- `self.url` and
            `self.cross_posts`, and `cross_posts` is an ARRAY COLUMN
            (app/models.py:1745), not a relationship, so reading it is not a
            lazy load. NO SQL, NO autoflush, so `post.image_id` is still None
            at `:664`.

        `File.query.get(None)` then returns None rather than raising -- probed
        in this container: `PROBE File.query.get(None) -> None`, with
        `SAWarning: fully NULL primary key identity cannot load any object.`

        THAT WARNING IS THIS TEST'S PRIMARY WITNESS. It is raised by
        SQLAlchemy only when a `get()` is handed an all-NULL primary key, and
        it is attributed to the calling line, so it says `:664` RAN and was
        handed None. Measured: `PROBE hits [('/app/app/shared/post.py', 664,
        'fully NULL primary key identity cannot load any object...')]`. `:438`
        is the function's only other `File.query.get(post.image_id)` and
        `from_scratch=True` closes the whole `:421-459` block, so `:664` is
        the only candidate; the assertion pins the line by SOURCE TEXT rather
        than by number anyway, and `:664`'s `file = ...` and `:438`'s
        `remove_file = ...` are distinguished by an exact `.strip()` compare
        (a substring test would NOT separate them -- `:664`'s whole line is a
        suffix of `:438`'s).

        WHAT THE WARNING BUYS, STATED PRECISELY, because an earlier revision
        of this docstring overclaimed that it was "the only assertion that
        separates `:665` FALSE from `:663` FALSE". That is untrue of STATE:
        both arms leave the alt_text at `:625`'s value, but only a true `:663`
        leaves `post.image_id` pointing at a row, so
        `assert s.post.image_id is not None` already separates them on state.
        What the warning adds is DIRECTNESS -- it is the only assertion here
        that observes `:664` executing and what it was given, rather than
        inferring it from state the commit at `:734` produced afterwards. That
        is a claim about the MUTATION sense rather than about this test's
        verdict: a mutant that changes what `:664` is handed while leaving the
        same final row is visible to the warning and invisible to both state
        assertions.

        The alt_text is the second witness, and alone it would be a false
        witness: 'A photo' is what `:625` wrote, and `post.image` being falsy
        at `:663` would leave it just as untouched (mechanism 1). Paired with
        the warning it says `:666` did not run; paired with the test below --
        same page, same og key, same `image_alt_text`, differing ONLY in
        whether the url has a path -- it says `:666` is a write and not an
        absence (mechanism 3).
        """
        http_mock.head(BARE_PIXELFED_URL).respond(
            200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, BARE_PIXELFED_URL,
                        og_image='https://cdn.example.com/shot.jpg',
                        og_title='A photo')
        s = _seed()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            edit_post(_api_input(url=BARE_PIXELFED_URL,
                                 image_alt_text='supplied by the caller'),
                      s.post, POST_TYPE_LINK, SRC_API, user=s.user,
                      from_scratch=True)

        null_pk = [w for w in caught
                   if 'fully NULL primary key' in str(w.message)
                   and w.filename.endswith('app/shared/post.py')
                   and _source_of(w) == 'file = File.query.get(post.image_id)']
        assert len(null_pk) == 1

        db.session.refresh(s.post)
        assert s.post.image_id is not None  # `:663`'s `post.image` WAS truthy
        file = db.session.get(File, s.post.image_id)
        assert file.source_url == 'https://cdn.example.com/shot.jpg'
        assert file.alt_text == 'A photo'  # `:625`'s value; `:666` never ran

    def test_a_thumbnail_flushed_by_the_cross_post_query_does_get_the_alt_text(
            self, db_session, http_mock):
        """`:665` TRUE -> `:666`. THE POSITIVE CONTROL for the test above.

        CLOSES NO ARC -- 665->666 is already covered suite-scoped. It exists
        because the test above asserts that something did NOT happen, and an
        assertion of that shape is worth nothing without a same-mechanism
        positive control (mechanism 3).

        It differs from the test above in EXACTLY ONE input: the url carries a
        path, so `:659`'s `calculate_cross_posts` gets past
        app/models.py:2362's bare-domain return and queries, its autoflush
        assigns `post.image_id`, `:664` finds the row and `:666` overwrites
        `:625`'s 'A photo' with the caller's text. Everything else -- the og
        tags, the `image_alt_text`, the post type, the seed -- is identical,
        which is what makes the alt_text difference attributable to `:665`
        and nothing else.

        The absence of the NULL-primary-key warning is asserted too: on this
        path `:664` is handed a real id, so the warning the test above
        requires must NOT appear here. Two independent witnesses moving in
        opposite directions across one input change (mechanism 5).
        """
        http_mock.head(PIXELFED_URL).respond(
            200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, PIXELFED_URL,
                        og_image='https://cdn.example.com/shot.jpg',
                        og_title='A photo')
        s = _seed()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            edit_post(_api_input(url=PIXELFED_URL,
                                 image_alt_text='supplied by the caller'),
                      s.post, POST_TYPE_LINK, SRC_API, user=s.user,
                      from_scratch=True)

        assert not [w for w in caught
                    if 'fully NULL primary key' in str(w.message)
                    and w.filename.endswith('app/shared/post.py')]

        db.session.refresh(s.post)
        file = db.session.get(File, s.post.image_id)
        assert file.source_url == 'https://cdn.example.com/shot.jpg'
        assert file.alt_text == 'supplied by the caller'  # `:666` overwrote

    def test_a_blank_choice_is_skipped_and_the_next_one_is_still_added(
            self, db_session):
        """`:675` FALSE -> back to `:674`. Arc 675->674, the LOOP-BACK.

        A CORRECTION TO THE BRIEF, measured rather than reasoned. The brief
        asserts that "a single bad choice gives the loop no next iteration to
        return to", so the blank entry must be followed by another. That is
        FALSE: a single blank choice closes 675->674 on its own, because the
        `for` at `:674` re-executes to raise StopIteration and coverage sees
        that line event either way. Measured by running a one-blank-choice
        variant alone under `--cov=app.shared.post --cov-branch`:

            (665, 668) MISSING
            (673, 682) MISSING
            (675, 674) TAKEN      <-- one blank choice, no second entry
            (683, 686) MISSING
            (699, 703) MISSING

        The second choice is kept anyway, because it is a strictly stronger
        WITNESS even though it is not needed for the arc. With one choice the
        only assertion available is that no row was written, which is also
        what a `:674` loop that never ran would leave (mechanism 3 and
        mechanism 1 together). 'kept' can only have been added by an iteration
        that ran AFTER the skip, so the pair pins both halves: the blank was
        skipped, and the skip did not abort the loop.

        `'   '` is non-empty, so `'choice_text' in choice` is TRUE and it is
        `:675`'s SECOND conjunct, `choice['choice_text'].strip()`, that
        decides -- the two conjuncts are not moved in lockstep here.

        THE KEYLESS FIRST ENTRY WAS ADDED BY TASK 9's MUTATION PASS, which
        found `:675`'s FIRST conjunct unwitnessed: replacing
        `'choice_text' in choice` with `True` survived the whole
        tests/test_shared_post_* suite, because every choice dict any test fed
        carried the key and the conjunct could never decide. `{'sort_order': 1}`
        has no 'choice_text' at all, so under the mutant `choice['choice_text']`
        raises KeyError while the original skips the entry. It is placed FIRST
        so that 'kept' still proves the loop ran on past it, exactly as the
        blank entry's own witness works.

        'kept' IS SUBMITTED AS ' kept ' FOR THE SAME REASON, and closes a
        second hole the pass found: `:678`'s `choice['choice_text'].strip()`
        with the `.strip()` REMOVED also survived the whole suite, because the
        only whitespace any test fed was the all-blank `'   '`, which `:675`
        skips before `:678` can see it. A value that is both padded and
        non-blank is the one shape that reaches `:678` and can tell the two
        apart.

        No `http_mock`: `_seed` leaves `post.url` None so `:403` is false, and
        the input's `url` is None so `:565`, `:660` and `:663` are all false.
        This call makes no outbound request at all, and a registered route
        would fail `assert_all_called=True` at teardown.
        """
        s = _seed()

        edit_post(_api_input(poll={'choices': [
            {'sort_order': 1},                          # no 'choice_text' key
            {'choice_text': '   ', 'sort_order': 2},
            {'choice_text': ' kept ', 'sort_order': 3},
        ]}), s.post, POST_TYPE_POLL, SRC_API, user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        texts = {c.choice_text for c in
                 PollChoice.query.filter_by(post_id=s.post.id).all()}
        assert texts == {'kept'}

    def test_editing_a_post_that_already_has_a_poll_reuses_the_existing_row(
            self, db_session):
        """`:683` FALSE -> `:686`. Arc 683->686.

        The create path always takes `:683`'s true arm, so this needs a post
        that already carries a `Poll`.

        THE BRIEF'S WITNESS DOES NOT EXIST AND ITS LOGIC WOULD NOT WORK. It
        proposes `polls[0].id == existing_id`, but `Poll` HAS NO `id`
        (app/models.py:3781, `post_id = db.Column(db.Integer,
        db.ForeignKey('post.id'), primary_key=True)`) -- `post_id` IS the
        primary key. That also means a freshly created row would carry the
        SAME key, so no identity comparison on the key can tell reuse from
        recreation; and `len(polls) == 1` is guaranteed by the primary key
        rather than by the branch.

        The witness that does work is a column `:686-690` DOES NOT WRITE.
        `:687` is `if 'end_poll' in poll_data and poll_data['end_poll']:` and
        the input below carries no `end_poll`, so `:688` is skipped and
        `end_poll` is untouched by the whole block. The seeded row's
        `end_poll` therefore survives only if `:683` took its FALSE arm; a row
        built by `:684`'s `Poll(post_id=post.id)` would have `end_poll` None.
        Paired with `mode == 'multiple'`, which shows `:686` really did write
        to the row it found rather than to some other one, that is the
        identity-plus-write pair. Asserting the mode alone would pass against
        a freshly created row (mechanism 1).

        THE SEEDED `post.url` CLOSES `:670`, and is the same device this file
        uses at `:657`. `:669` requires `type == POST_TYPE_POLL`, so `:398`'s
        `post.type = type` has ALWAYS already written POLL by the time `:670`
        runs -- which made `assert post.type == POST_TYPE_POLL` the submitted
        value surviving, and Task 9 measured it: deleting `:670` outright
        survived all 354 tests. Seeding a pixelfed url makes `:403` true and
        `:404`/`:405` write `POST_TYPE_IMAGE` in between, so POLL at the end
        can only have come from `:670`. `:404` is a pure `startswith`, so this
        costs no HTTP route, and the input's `url` is still None so `:565`,
        `:660` and `:663` are all false exactly as before.
        """
        s = _seed(url='https://pixelfed.social/p/old/1')
        db.session.add(Poll(post_id=s.post.id, mode='single',
                            end_poll=datetime(2031, 3, 4, 5, 6, 7)))
        db.session.commit()

        edit_post(_api_input(poll={'mode': 'multiple', 'choices': [
            {'choice_text': 'a', 'sort_order': 1}]}),
            s.post, POST_TYPE_POLL, SRC_API, user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_POLL  # only `:670` writes this here
        polls = Poll.query.filter_by(post_id=s.post.id).all()
        assert len(polls) == 1
        assert polls[0].mode == 'multiple'  # `:686` wrote to the row it found
        assert polls[0].end_poll == datetime(2031, 3, 4, 5, 6, 7)

    def test_editing_a_post_that_already_has_an_event_reuses_the_existing_row(
            self, db_session):
        """`:699` FALSE -> `:703`. Arc 699->703.

        The event twin of the test above, and `Event` has the same shape:
        app/models.py:3840 makes `post_id` the primary key and there is no
        `id` column, so the brief's `events[0].id == existing_id` is again
        unavailable and again would not have distinguished anything.

        The surviving column here is `start`. `:703` is `if 'start' in
        event_data:` and the input below carries none, so `:704` is skipped
        and `start` is untouched; a row built by `:700`'s
        `Event(post_id=post.id)` would have `start` None. `timezone` is
        written unconditionally at `:708`, so asserting it shows `:708` wrote
        to the row that `:698` found.

        THE SEEDED `post.url` CLOSES `:697`, the exact twin of `:670` in the
        poll test above and of `:657` in `TestGenericOpengraphArm`: `:696`
        requires `type == POST_TYPE_EVENT`, so `:398` has already written
        EVENT and deleting `:697` outright survived all 354 tests. The pixelfed
        url makes `:404`/`:405` write IMAGE in between, so EVENT at the end has
        exactly one possible source. See the poll test for why this costs no
        HTTP route.
        """
        s = _seed(url='https://pixelfed.social/p/old/1')
        db.session.add(Event(post_id=s.post.id,
                             start=datetime(2032, 7, 8, 9, 10, 11)))
        db.session.commit()

        edit_post(_api_input(event={'timezone': 'Europe/Berlin',
                                    'max_attendees': 5}),
                  s.post, POST_TYPE_EVENT, SRC_API, user=s.user,
                  from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_EVENT  # only `:697` writes this here
        events = Event.query.filter_by(post_id=s.post.id).all()
        assert len(events) == 1
        assert events[0].timezone == 'Europe/Berlin'  # `:708` wrote the row
        assert events[0].max_attendees == 5
        assert events[0].start == datetime(2032, 7, 8, 9, 10, 11)

    def test_re_editing_a_poll_clears_the_old_choices_and_their_votes(
            self, db_session):
        """`:431` and `:432`, the raw-SQL deletes in the `not from_scratch`
        block.

        ADDED BY TASK 9's FIX ROUND. Deleting either statement survived all 354
        tests, and the comment above them says why that matters more than the
        coverage number does::

            429	        # Remove any poll votes that currently exists
            430	        # Partially because it's easier to code but also to stop malicious alterations to polls after people have already voted
            431	        db.session.execute(text('DELETE FROM "poll_choice_vote" WHERE post_id = :post_id'), {'post_id': post.id})
            432	        db.session.execute(text('DELETE FROM "poll_choice" WHERE post_id = :post_id'), {'post_id': post.id})

        -- so a silent regression here reintroduces exactly the alteration the
        lines exist to prevent. This is the only test in the round's three
        regions that passes `from_scratch=False`; every other one switches
        `:421-459` off entirely, which is why the block's statements went
        unwitnessed.

        `:432` IS KILLED BY VALUE. With it deleted the old choice survives
        alongside the new one, so the text set comes back as
        `{'stale', 'fresh'}` rather than `{'fresh'}`.

        `:431` IS KILLED BY THE EXCEPTION, AND THE CAMPAIGN'S CRASH-KILL RULE
        IS SATISFIED RATHER THAN SET ASIDE. A crash kill counts only when a
        viable NON-CRASHING variant of the same fault also dies, and one
        exists. Read out of the file rather than recalled::

            3832	class PollChoiceVote(db.Model):
            3833	    choice_id = db.Column(db.Integer, db.ForeignKey('poll_choice.id'), primary_key=True)
            3834	    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
            3835	    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), index=True)

        `post_id` at `:3835` is an INDEPENDENT foreign key onto `post.id`, not
        something derived from `choice_id`. `:431` deletes votes by `post_id`
        and `:432` deletes choices by `post_id`, so a vote carrying THIS post's
        `post_id` while its `choice_id` points at a choice owned by a DIFFERENT
        post survives `:432` without violating anything -- and with `:431`
        deleted it would make this test's `count() == 0` read 1, a plain value
        failure with no exception involved. An earlier revision of this
        docstring claimed no such input existed; it was wrong, and the
        correction strengthens the closure rather than weakening it.

        The vote this test actually seeds points at THIS post's own choice,
        which is the shape production produces, so here the surviving vote
        meets `:432`'s delete and the constraint fires first. The assertion is
        a value assertion either way, with `count() == 1` before the edit as
        its positive control, so the test does not read as a bare crash probe.

        NO `http_mock`: the input's `url` is None and `_seed` leaves
        `post.url` None, so `:435`'s `url != post.url or uploaded_file` is
        false, the teardown at `:437-451` is skipped, and `:565`, `:660` and
        `:663` are all false. This call makes no outbound request.
        """
        s = _seed()
        db.session.add(Poll(post_id=s.post.id, mode='single'))
        db.session.commit()
        stale = make_poll_choice(s.post, 'stale', sort_order=1)
        voter = make_user(s.instance, 'voter', local=True)
        db.session.add(PollChoiceVote(choice_id=stale.id, user_id=voter.id,
                                      post_id=s.post.id))
        db.session.commit()
        assert PollChoiceVote.query.filter_by(post_id=s.post.id).count() == 1

        edit_post(_api_input(poll={'mode': 'single', 'choices': [
            {'choice_text': 'fresh', 'sort_order': 1}]}),
            s.post, POST_TYPE_POLL, SRC_API, user=s.user, from_scratch=False)

        texts = {c.choice_text for c in
                 PollChoice.query.filter_by(post_id=s.post.id).all()}
        assert texts == {'fresh'}                                    # `:432`
        assert PollChoiceVote.query.filter_by(post_id=s.post.id).count() == 0

    # ------------------------------------------------------------------
    # `:669` and `:696` are two-conjunct gates whose operands MOVED IN LOCKSTEP
    # in every test written before fix round 2: the `type` argument and the
    # data dict were always supplied together, so neither conjunct ever decided
    # anything on its own and replacing either with `True` changed nothing
    # observable. The three tests below break the lockstep in all three
    # available directions. This is false-witness mechanism 5, the same one
    # TestImageArmEventBanner's docstring describes for `:612`.
    #
    # TWO OF THE THREE NEED FEDERATION SUPPRESSED, and the suppressor is an
    # in-production one rather than a test-only contrivance. Read out of the
    # file rather than recalled::
    #
    #     736	    if post.status < POST_STATUS_PUBLISHED or post.community.local_only or post.community.private:
    #     737	        federate = False
    #
    # `Community.local_only` is app/models.py:610 ("only users on this
    # instance can post. no federation."), default False. Without it, a post
    # typed POLL or EVENT that carries NO matching row reaches
    # app/shared/tasks/pages.py under eager Celery, where `:222`'s
    # `Poll.query...first()` and `:231`'s `Event.query...first()` both come
    # back None and are dereferenced at `:224`/`:233` -- an AttributeError
    # downstream of everything these tests witness, and exactly the crash
    # TestImageArmEventBanner's docstring records. Suppressing federation
    # removes it without touching anything in the three regions under test.
    #
    # THE FIRST TEST NEEDS NO SUPPRESSOR AT ALL, and that asymmetry is the
    # point: it leaves `post.type` at ARTICLE, so `pages.py:222` and `:231` are
    # both false and neither dereference happens.

    def test_poll_data_without_the_poll_type_builds_no_poll(self, db_session):
        """`:669`'s FIRST conjunct is the only thing that decides here.

        `type` is ARTICLE while `poll` IS supplied, so `type == POST_TYPE_POLL`
        is false and `poll_data` is truthy. Replacing the first conjunct with
        `True` makes the mutant build a `Poll` and a `PollChoice` that the
        original does not, and `:670` then retypes the post -- so all three
        assertions below move together under the mutant and none is a bare
        absence.

        NO FEDERATION SUPPRESSOR IS NEEDED, unlike its two siblings below.
        `post.type` stays ARTICLE on the original path, so
        `app/shared/tasks/pages.py:222`'s `if post.type == POST_TYPE_POLL:` and
        `:231`'s `elif post.type == POST_TYPE_EVENT:` are both false and
        nothing is dereferenced. Under the mutant `:670` writes POLL, but by
        then `:684` has created the `Poll` row `:223` looks for, so the mutant
        dies on the assertion rather than on an exception.

        ARTICLE RATHER THAN LINK, AND THAT WAS MEASURED. A first revision of
        this test submitted `POST_TYPE_LINK` and failed -- not on its
        assertions but inside federation, at a line neither conjunct of `:669`
        has anything to do with::

            318	    if post.type == POST_TYPE_LINK or post.type == POST_TYPE_VIDEO:
            319	        note['content'] += '<p><a href=' + post.url + '>' + post.title + '</a></p>'

        `TypeError: can only concatenate str (not "NoneType") to str`, because
        a LINK post with no url reaches `:319`. ARTICLE takes `:320`'s
        `elif post.type != POST_TYPE_POLL:` instead, which reads only the
        title. Registered here as a third in-production dereference of the same
        family as `:224` and `:233`; it is not this round's to fix.

        `post.type == POST_TYPE_ARTICLE` is BOTH the submitted value and
        `Post.type`'s column default (app/models.py:1715), so it is not a
        witness that `:398` ran -- but it IS a discriminator for this mutant,
        which writes POLL over it at `:670`. The `Poll` and `PollChoice` counts
        are the primary witnesses.

        No `http_mock`: the input's `url` is None and `_seed` leaves `post.url`
        None, so `:403`, `:565`, `:660` and `:663` are all false and the call
        makes no outbound request.
        """
        s = _seed()

        edit_post(_api_input(poll={'mode': 'single', 'choices': [
            {'choice_text': 'a', 'sort_order': 1}]}),
            s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_ARTICLE    # `:670` never ran
        assert Poll.query.filter_by(post_id=s.post.id).count() == 0
        assert PollChoice.query.filter_by(post_id=s.post.id).count() == 0

    def test_the_poll_type_without_poll_data_builds_no_poll(self, db_session):
        """`:669`'s SECOND conjunct is the only thing that decides here.

        The mirror of the test above: `type` IS POLL while no `poll` key is
        submitted, so `:300`'s `input.get('poll', None)` leaves `poll_data`
        None. Replacing the second conjunct with `True` sends control into the
        block with `poll_data` None and `:673`'s `'choices' in poll_data`
        raises `TypeError`.

        THAT CRASH IS THE FAULT'S MEANING, and the rule is satisfied rather
        than waived: the conjunct's only job is to keep a `None` out of the
        subscripting that follows, so there is no wrong-value form of the same
        fault to look for. The assertion below is still a value assertion about
        the ORIGINAL path -- POLL type, no poll data, no `Poll` row -- rather
        than a crash probe, and `test_a_blank_choice_is_skipped...` is the
        positive control showing a `Poll` can be built here at all.

        `local_only` suppresses federation; see the comment block above for why
        that is required here and not in the test above.
        """
        s = _seed()
        s.community.local_only = True
        db.session.commit()

        edit_post(_api_input(), s.post, POST_TYPE_POLL, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_POLL       # `:398` wrote this, not `:670`
        assert Poll.query.filter_by(post_id=s.post.id).count() == 0

    def test_the_event_type_without_event_data_builds_no_event(self, db_session):
        """`:696`'s SECOND conjunct is the only thing that decides here.

        The event twin of the test above. `type` IS EVENT while no `event` key
        is submitted, so `:285`'s `input.get('event', None)` leaves
        `event_data` None; replacing the second conjunct with `True` reaches
        `:703`'s `'start' in event_data` and raises `TypeError`, for the same
        reason and with the same justification.

        `:696`'s FIRST conjunct is already killed by
        `TestImageArmEventBanner`'s pair, which holds `event_data` fixed and
        moves `type` -- so between that pair and this test both operands of
        `:696` now decide something on their own.

        `local_only` suppresses federation, which here is not an optimisation
        but a requirement: this is precisely the input
        `TestImageArmEventBanner`'s docstring records as dereferencing None at
        `app/shared/tasks/pages.py:231-233`.
        """
        s = _seed()
        s.community.local_only = True
        db.session.commit()

        edit_post(_api_input(), s.post, POST_TYPE_EVENT, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_EVENT      # `:398` wrote this, not `:697`
        assert Event.query.filter_by(post_id=s.post.id).count() == 0
