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

from app import db
from app.constants import (
    POST_STATUS_SCHEDULED, POST_TYPE_ARTICLE, POST_TYPE_EVENT,
    POST_TYPE_IMAGE, POST_TYPE_LINK, POST_TYPE_POLL, POST_TYPE_VIDEO,
    ROLE_ADMIN, SRC_API, SRC_WEB,
)
from app.models import Domain, Event, File, Poll, PollChoice, Role
from app.shared.post import edit_post
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
