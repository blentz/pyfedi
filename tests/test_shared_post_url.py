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

from datetime import datetime, timedelta
from io import BytesIO

from PIL import Image
from sqlalchemy import text

from app import db
from app.constants import (
    POST_STATUS_SCHEDULED, POST_TYPE_ARTICLE, POST_TYPE_EVENT,
    POST_TYPE_IMAGE, POST_TYPE_LINK, POST_TYPE_POLL, POST_TYPE_VIDEO,
    ROLE_ADMIN, SRC_API, SRC_WEB,
)
from app.models import Domain, Event, File, Poll, PollChoice, Role
from app.shared.post import edit_post
from app.utils import store_files_in_s3
from tests.factories import make_community_member, make_user
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
        other write to `image_id` is `:441`'s NULL, which satisfies it.

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
