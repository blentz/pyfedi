r"""Every user-influenced redirect target goes through `is_safe_redirect_target`.

`tests/test_safe_redirect_target.py` pins the check itself and
`tests/test_redirect_back.py` pins `back()`. This file pins the OTHER family of
sites: the ones that take a single user-supplied target out of the request --
`?next=`, `?redirect=`, `?return_to=`, a posted `referrer` field -- and fall back
to a per-site default when it is not usable. `app.utils.safe_redirect_target` is
the one-line form of that decision, and the scan at the bottom of this file is
what stops a new site being added without it.

WHY THESE SITES EXISTED UNGUARDED. Three of them -- `redirect_next_page`,
`determine_next_page` and `app/shared/auth.py:log_user_in` -- guarded with
`urlsplit(next_page).netloc != ''`. That test rejects `//evil.example` but
ACCEPTS `///evil.example`, `/\evil.example` and `\\evil.example`, because
urlsplit reports no netloc for any of them -- while a browser, per the WHATWG URL
Standard's relative-slash state, reads the second `/` or `\` as the start of an
authority and navigates off-origin. Those three are on the LOGIN flow, so the
payoff was a phishing page reached from a URL beginning with this instance's own
hostname. The `?redirect=` / `?return_to=` / posted-`referrer` sites had no check
at all.

Those three spellings are OLD_GUARD_BYPASSES below, and they are included in
OFF_ORIGIN, which every site in this file is tested against.

WHAT THIS FILE DOES AND DOES NOT PROVE. The route tests below drive six sites
end to end. The other nineteen are proved differently and deliberately: each is a
single call to `safe_redirect_target`, the helper tests pin what that call does
to all seven OFF_ORIGIN spellings, and
`test_every_user_supplied_redirect_target_is_checked_or_documented` proves by
scanning the source that every site makes the call. That is a weaker guarantee
than an end-to-end test per site and it is stated here rather than implied.
"""

import re
from pathlib import Path
from typing import NamedTuple

import pytest
from flask import session
from flask_login import login_user
from flask_wtf.csrf import generate_csrf

from app import db
from app.auth.util import determine_next_page, redirect_next_page
from app.constants import SRC_WEB
from app.models import CommunityFlair, Instance, InstanceBlock
from app.shared.auth import log_user_in
from app.utils import safe_redirect_target
from tests.factories import make_community, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


# Off-origin (or non-navigable) to a browser, every one.
OFF_ORIGIN = [
    '//evil.example/x',
    '///evil.example/x',
    '/\\evil.example/x',
    '\\\\evil.example/x',
    'https://evil.example/x',
    'https://evil.example/?x=test.piefed.local',
    'javascript:alert(1)',
]

# The three the final review demonstrated the old `urlsplit(...).netloc` guard
# ACCEPTED. Called out separately so a regression in exactly the reported case is
# unmistakable in the failure output.
OLD_GUARD_BYPASSES = ['///evil.example/x', '/\\evil.example/x', '\\\\evil.example/x']

# url_for('main.index') under the test app.
INDEX = '/home'

ON_ORIGIN = [
    '/somewhere',
    '/somewhere?x=1#f',
    'https://test.piefed.local/somewhere',
]


def local_user(name):
    """A local user, with the local Instance row (id 1) its FK needs.

    make_user(None, ...) sets instance_id=1 unconditionally; the `site` fixture
    creates the Site row, not the Instance row, so the first caller has to make
    it.
    """
    if not Instance.query.get(1):
        make_instance('test.piefed.local', software='piefed')
    return make_user(None, name, local=True)


def login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def csrf(app, client):
    """A CSRF token/session pair the app will accept.

    app.utils.login_required calls flask_wtf's validate_csrf directly on every
    POST, which ignores WTF_CSRF_ENABLED -- so a POST route test needs a real
    token even though the test config disables CSRF for forms. Same helper as
    tests/test_redirect_back.py.
    """
    with app.test_request_context():
        token = generate_csrf()
        raw = session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw
    return token


def from_a_fresh_ip(n):
    """A distinct REMOTE_ADDR per request.

    /auth/login carries a real Flask-Limiter limit ("30 per day;10 per 5
    minutes") backed by the test Redis, and that Redis is not torn down between
    tests or between runs. Bucketing every request under its own client IP keeps
    a parametrised route test from rate-limiting itself, and from rate-limiting
    the next run of the suite.
    """
    return {'REMOTE_ADDR': f'198.51.100.{n % 254 + 1}'}


def location(response):
    return response.headers.get('Location', '')


def assert_not_off_site(response):
    assert 'evil.example' not in location(response), location(response)
    assert not location(response).startswith('javascript:'), location(response)


class TestSafeRedirectTargetPicksTheFallback:
    """The shared helper. Nineteen of the twenty-five sites are one call to it."""

    @pytest.mark.parametrize('candidate', OFF_ORIGIN)
    def test_an_off_origin_candidate_is_replaced_by_the_default(self, app, candidate):
        with app.test_request_context('/'):
            assert safe_redirect_target(candidate, '/fallback') == '/fallback'

    @pytest.mark.parametrize('candidate', OLD_GUARD_BYPASSES)
    def test_the_spellings_the_old_guard_accepted_are_replaced(self, app, candidate):
        with app.test_request_context('/'):
            assert safe_redirect_target(candidate, '/fallback') == '/fallback'

    @pytest.mark.parametrize('candidate', ON_ORIGIN)
    def test_an_on_origin_candidate_is_used(self, app, candidate):
        with app.test_request_context('/'):
            assert safe_redirect_target(candidate, '/fallback') == candidate

    @pytest.mark.parametrize('candidate', [None, '', '   '])
    def test_a_missing_candidate_is_replaced_by_the_default(self, app, candidate):
        with app.test_request_context('/'):
            assert safe_redirect_target(candidate, '/fallback') == '/fallback'

    def test_a_non_string_candidate_is_replaced_by_the_default(self, app):
        """`request.args.get` hands back None when the parameter is absent and a
        form field can hand back a list. Neither may reach redirect()."""
        with app.test_request_context('/'):
            assert safe_redirect_target(['/x'], '/fallback') == '/fallback'


class TestAuthNextPageIsChecked:
    """app/auth/util.py `redirect_next_page` and `determine_next_page` -- the
    two login-flow sites that carried the bypassable guard."""

    @pytest.mark.parametrize('candidate', OFF_ORIGIN)
    def test_redirect_next_page_refuses_an_off_origin_next(self, app, candidate):
        with app.test_request_context('/auth/login', query_string={'next': candidate}):
            response = redirect_next_page()
        assert response.headers['Location'] == INDEX

    @pytest.mark.parametrize('candidate', ON_ORIGIN)
    def test_redirect_next_page_honours_an_on_origin_next(self, app, candidate):
        with app.test_request_context('/auth/login', query_string={'next': candidate}):
            response = redirect_next_page()
        assert response.headers['Location'] == candidate

    @pytest.mark.parametrize('candidate', OFF_ORIGIN)
    def test_determine_next_page_refuses_an_off_origin_next(self, app, db_session, candidate):
        user = local_user('nextuser')
        user.finished_onboarding = True
        db.session.commit()
        with app.test_request_context('/auth/login', query_string={'next': candidate}):
            login_user(user)
            assert determine_next_page() == INDEX

    @pytest.mark.parametrize('candidate', ON_ORIGIN)
    def test_determine_next_page_honours_an_on_origin_next(self, app, db_session, candidate):
        user = local_user('nextuser')
        user.finished_onboarding = True
        db.session.commit()
        with app.test_request_context('/auth/login', query_string={'next': candidate}):
            login_user(user)
            assert determine_next_page() == candidate


class TestLoginRouteEndToEnd:
    """The same guard where a browser meets it, not only at the function."""

    @pytest.mark.parametrize('n,candidate', list(enumerate(OFF_ORIGIN)))
    def test_an_authenticated_user_is_not_sent_off_site(self, app, db_session, n, candidate):
        user = local_user('loginuser')
        client = app.test_client()
        login(client, user)
        response = client.get('/auth/login', query_string={'next': candidate},
                              environ_base=from_a_fresh_ip(n))
        assert response.status_code == 302
        assert_not_off_site(response)

    def test_an_authenticated_user_is_sent_to_an_on_origin_next(self, app, db_session):
        user = local_user('loginuser')
        client = app.test_client()
        login(client, user)
        response = client.get('/auth/login', query_string={'next': '/somewhere'},
                              environ_base=from_a_fresh_ip(200))
        assert response.headers['Location'] == '/somewhere'


class TestSharedAuthNextPageIsChecked:
    """app/shared/auth.py `log_user_in(..., SRC_WEB)`, the third site that had
    the bypassable guard. Only the API path reaches this function today, and
    only with SRC_API -- but the SRC_WEB arm is live code, so it is driven
    directly with the duck-typed form it reads."""

    class Field:
        def __init__(self, data):
            self.data = data

    class Form:
        def __init__(self, user_name, password):
            self.user_name = TestSharedAuthNextPageIsChecked.Field(user_name)
            self.password = TestSharedAuthNextPageIsChecked.Field(password)
            self.low_bandwidth_mode = TestSharedAuthNextPageIsChecked.Field(False)

    @pytest.mark.parametrize('candidate', OFF_ORIGIN)
    def test_an_off_origin_next_is_refused(self, app, db_session, candidate):
        user = local_user('shareduser')
        user.set_password('correct horse battery')
        db.session.commit()
        with app.test_request_context('/auth/login', query_string={'next': candidate}):
            response = log_user_in(self.Form('shareduser', 'correct horse battery'), SRC_WEB)
        assert_not_off_site(response)

    def test_an_on_origin_next_is_honoured(self, app, db_session):
        user = local_user('shareduser')
        user.set_password('correct horse battery')
        db.session.commit()
        with app.test_request_context('/auth/login', query_string={'next': '/somewhere'}):
            response = log_user_in(self.Form('shareduser', 'correct horse battery'), SRC_WEB)
        assert response.headers['Location'] == '/somewhere'


class TestRedirectParameterRoutes:
    """`?redirect=` and `?return_to=`, handed straight to redirect() before this
    change. All are behind @login_required, which reduces the payoff but does not
    remove it: a logged-in user following a crafted link still lands on the
    attacker's page from a URL beginning with this instance's hostname."""

    @pytest.mark.parametrize('candidate', OFF_ORIGIN)
    def test_user_community_unblock_refuses_an_off_origin_redirect(self, app, db_session, candidate):
        user = local_user('blockuser')
        community = make_community('somecommunity')
        client = app.test_client()
        login(client, user)
        token = csrf(app, client)
        response = client.post(f'/user/community/{community.id}/unblock',
                               query_string={'redirect': candidate},
                               data={'csrf_token': token})
        assert response.status_code == 302
        assert_not_off_site(response)

    def test_user_community_unblock_honours_an_on_origin_redirect(self, app, db_session):
        user = local_user('blockuser')
        community = make_community('somecommunity')
        client = app.test_client()
        login(client, user)
        token = csrf(app, client)
        response = client.post(f'/user/community/{community.id}/unblock',
                               query_string={'redirect': '/somewhere'},
                               data={'csrf_token': token})
        assert response.headers['Location'] == '/somewhere'

    @pytest.mark.parametrize('candidate', OFF_ORIGIN)
    def test_user_flair_unblock_refuses_an_off_origin_redirect(self, app, db_session, candidate):
        user = local_user('flairuser')
        community = make_community('flaircommunity')
        flair = CommunityFlair(community_id=community.id, flair='a-flair')
        db.session.add(flair)
        db.session.commit()
        client = app.test_client()
        login(client, user)
        token = csrf(app, client)
        response = client.post(f'/user/flair/{flair.id}/unblock',
                               query_string={'redirect': candidate},
                               data={'csrf_token': token})
        assert response.status_code == 302
        assert_not_off_site(response)

    @pytest.mark.parametrize('candidate', OFF_ORIGIN)
    def test_instance_unblock_refuses_an_off_origin_redirect(self, app, db_session, candidate):
        user = local_user('instuser')
        instance = make_instance('remote.example')
        db.session.add(InstanceBlock(user_id=user.id, instance_id=instance.id))
        db.session.commit()
        client = app.test_client()
        login(client, user)
        token = csrf(app, client)
        response = client.post(f'/instance/{instance.id}/unblock',
                               query_string={'redirect': candidate},
                               data={'csrf_token': token})
        assert response.status_code == 302
        assert_not_off_site(response)

    @pytest.mark.parametrize('candidate', OFF_ORIGIN)
    def test_user_follow_refuses_an_off_origin_return_to(self, app, db_session, candidate):
        """The absolute spellings 401 here rather than redirecting -- see
        TestReturnToAbsoluteUrlsAre401 below for why, and for why the ORDER of
        the two guards matters. The three relative-looking off-origin spellings
        (`///evil.example`, `/\\evil.example`, `\\\\evil.example`) sail past
        `startswith('http')` and are stopped by the origin check instead."""
        user = local_user('me')
        local_user('them')
        client = app.test_client()
        login(client, user)
        token = csrf(app, client)
        response = client.post('/u/them/follow', query_string={'return_to': candidate},
                               data={'csrf_token': token})
        assert_not_off_site(response)

    def test_user_follow_honours_an_on_origin_return_to(self, app, db_session):
        user = local_user('me')
        local_user('them')
        client = app.test_client()
        login(client, user)
        token = csrf(app, client)
        response = client.post('/u/them/follow', query_string={'return_to': '/somewhere'},
                               data={'csrf_token': token})
        assert response.headers['Location'] == '/somewhere'

    @pytest.mark.parametrize('candidate', OFF_ORIGIN)
    def test_user_unfollow_refuses_an_off_origin_return_to(self, app, db_session, candidate):
        user = local_user('me')
        local_user('them')
        client = app.test_client()
        login(client, user)
        token = csrf(app, client)
        response = client.post('/u/them/unfollow', query_string={'return_to': candidate},
                               data={'csrf_token': token})
        assert_not_off_site(response)



class TestReturnToAbsoluteUrlsAre401:
    """`?return_to=` on follow / unfollow / bot_challenge: ORDER OF GUARDS.

    These routes carry a pre-existing rule -- an ABSOLUTE url in `?return_to=`
    is refused with 401, not redirected to. The origin check this branch added
    has to run AFTER it, and the first version of that change ran it BEFORE.
    That inverted the guard, silently:

      - an off-origin absolute (`https://evil.example/x`) was replaced by the
        fallback and quietly redirected, so the 401 never fired for the case it
        was written for;
      - a legitimate same-origin absolute (`https://test.piefed.local/x`) passed
        the origin check, still started with `http`, and got the 401 instead.

    Nothing noticed, because every test asserted only "does not go off-site" --
    which a silent fallback satisfies. These assert the STATUS CODE, so the
    ordering cannot invert again without failing.
    """

    ROUTES = ['/u/them/follow', '/u/them/unfollow']

    def _post(self, app, route, return_to):
        user = local_user('me')
        local_user('them')
        client = app.test_client()
        login(client, user)
        token = csrf(app, client)
        return client.post(route, query_string={'return_to': return_to},
                           data={'csrf_token': token})

    @pytest.mark.parametrize('route', ROUTES)
    def test_an_off_origin_absolute_is_401_not_a_silent_fallback(self, app, db_session, route):
        response = self._post(app, route, 'https://evil.example/x')
        assert response.status_code == 401, (
            f'{route} answered {response.status_code} -> '
            f'{response.headers.get("Location")!r}. An absolute ?return_to= must be '
            f'refused outright; a 302 here means the origin check ran BEFORE the '
            f'startswith("http") guard and swallowed it.')

    @pytest.mark.parametrize('route', ROUTES)
    def test_a_same_origin_absolute_is_also_401(self, app, db_session, route):
        """The pre-existing rule is about ABSOLUTE urls, not about origin: it
        refuses our own host too, and always did. Pinned so the fix cannot
        quietly turn it into an origin test."""
        response = self._post(app, route, 'https://test.piefed.local/x')
        assert response.status_code == 401

    @pytest.mark.parametrize('route', ROUTES)
    def test_a_relative_off_origin_spelling_falls_back_rather_than_401(self, app, db_session, route):
        """`///evil.example` does not start with `http`, so the 401 guard never
        sees it. The origin check is what stops it, and a fallback is the right
        answer for it."""
        response = self._post(app, route, '///evil.example/x')
        assert response.status_code == 302
        assert response.headers['Location'] == '/u/them'


# --------------------------------------------------------------------------
# The completeness pin.
#
# Important 2 of the final review was not only a bug. It was a CLAIM -- "one
# origin check for every user-influenced redirect target" -- that nothing
# established, and a grep disproved it. This is that grep, run on every suite
# run, with the exemptions written down and reasoned rather than assumed.
#
# It reads source text, which is unusual for a test. It is here because the
# property being asserted IS a property of the source: that no site reads one of
# these request values as a redirect target without the check. A behavioural
# test can only ever cover the sites someone remembered to write one for, which
# is precisely the failure mode this finding was about.
# --------------------------------------------------------------------------

APP_ROOT = Path(__file__).resolve().parent.parent / 'app'

# Expressions that read a user-supplied redirect target out of the request.
USER_SUPPLIED_TARGET = re.compile(
    r"""request\.args\.get\(\s*['"](?:next|redirect|return_to|referrer)['"]"""
    r"""|request\.form\.get\(\s*['"](?:next|redirect|return_to|referrer)['"]"""
    r"""|request\.args\[\s*['"](?:next|redirect|return_to|referrer)['"]\s*\]"""
    # The posted `referrer` field, but only where it is BEING REDIRECTED TO --
    # `form.referrer.data = referrer(...)` is the site FILLING the field in for
    # the next request, which is the opposite direction and already checked.
    r"""|redirect\([^)]*form\.referrer\.data"""
)

# Matches is_safe_redirect_target too, which is the deliberate form at the sites
# whose fallback must stay lazy.
GUARDED = re.compile(r'safe_redirect_target\(')

# How far below the read the guard may sit. Three lines covers both shapes in
# the tree -- the guard on the read's own line, and
#
#     candidate = request.args.get('return_to', ...)      <- the read
#     if candidate.startswith('http'):
#         abort(401)
#     return safe_redirect_target(candidate, ...)         <- the guard, +3
#
# and is deliberately tight. A guard further from its read is harder to see is
# missing, which is this test's entire job.
GUARD_WINDOW = 3


class Exempt(NamedTuple):
    """One documented read that is not a redirect target.

    `line` is the EXACT stripped source line, NOT a substring. A substring match
    exempts any future line that happens to contain it, which is how an earlier
    version of this list let a brand-new unguarded
    `return_to = request.args.get('return_to')` into app/user/routes.py without
    complaint. `occurrences` is how many times that exact line appears in the
    file, so a SECOND copy of an exempt line is drift rather than a free pass.
    """
    line: str
    occurrences: int
    reason: str


# WHAT THIS LIST AND THE SCAN AROUND IT DO AND DO NOT CATCH. Read this before
# trusting either.
#
# CAUGHT:
#   - a new read of `?next=` / `?redirect=` / `?return_to=` / a posted or query
#     `referrer` anywhere under app/, with no guard within GUARD_WINDOW lines --
#     INCLUDING in a file that already has exemptions, because an exemption
#     matches a whole stripped line, not a substring
#     (test_a_new_unguarded_read_appended_to_an_exempt_file_is_caught);
#   - a second copy of an already-exempt line
#     (test_a_second_copy_of_an_exempt_line_is_caught);
#   - an exemption whose line no longer exists, or whose count has drifted;
#   - a guard placed further than GUARD_WINDOW from its read;
#   - reintroducing the bypassable `urlsplit(x).netloc` guard.
#
# NOT CAUGHT. These are real holes, not hypotheticals:
#   - SEMANTIC ROT. If /anoobis started calling `redirect(next)` while its
#     exempt line stayed exactly as written, this scan would still pass. An
#     exemption records a claim about what the code AROUND the read does, and
#     nothing here re-checks that claim. The reasons below are for a human to
#     re-read when touching those functions -- that is what they are for.
#   - a redirect target arriving under a fifth parameter name, or read
#     dynamically (`request.args.get(name)`).
#   - a value that IS guarded and then ignored, or guarded and then mutated.
#   - anything outside `app/**/*.py`: templates, plugins, static JS.
#
# A safety net whose limits are written down is worth having. One that overstates
# itself is the defect this whole finding was about, so the limits live here,
# next to the thing they qualify, and not only in a report.
DOCUMENTED_EXEMPTIONS = {
    'app/main/routes.py': [
        Exempt(
            line="next = request.args.get('next')",
            occurrences=1,
            reason=("/anoobis does not call redirect(). It renders anoobis.html, "
                    "whose script sets location.href once the proof-of-work "
                    "finishes, and it applies its own furl-based host check "
                    "first. Left alone deliberately: routing it through "
                    "safe_redirect_target would turn a raise-on-hostile-host "
                    "into a silent fallback, which is a behaviour change on an "
                    "unauthenticated anti-bot route rather than a security fix. "
                    "Its separate defect -- furl('http://[') raises ValueError, "
                    "so ?next=http://[ is a 500 -- is reported in the final "
                    "review and left for its own change. RE-CHECK THIS REASON "
                    "if anoobis() ever gains a redirect(): the scan cannot."),
        ),
    ],
    'app/post/routes.py': [
        Exempt(
            line="referrer=request.form.get('referrer')))",
            occurrences=1,
            reason=("not a redirect target here: the value is handed to "
                    "url_for('post.post_block_image_purge_posts') as a query "
                    "parameter, and that route checks it before redirecting."),
        ),
        Exempt(
            line="referrer=request.args.get('referrer'))",
            occurrences=1,
            reason=("render_template keyword. It becomes a hidden field in "
                    "post_block_image_purge_posts.html, Jinja-escaped, and is "
                    "checked when it comes back and reaches redirect()."),
        ),
    ],
    'app/admin/routes.py': [
        Exempt(
            line="form=form, referrer=request.args.get('referrer'))",
            occurrences=1,
            reason=("render_template keyword, same template and same reasoning "
                    "as the app/post/routes.py entry above."),
        ),
    ],
    'app/user/routes.py': [
        Exempt(
            line="return_to = request.args.get('return_to')",
            occurrences=1,
            reason=("user_preview: passed only to render_template. The "
                    "follow/unfollow links it builds land on routes that run "
                    "the value through return_to_or_401 before redirecting."),
        ),
    ],
}


def _relative(path):
    return str(path.relative_to(APP_ROOT.parent))


def unguarded_reads(source, rel_path):
    """Every user-supplied redirect-target read in `source` with no guard.

    Takes TEXT rather than a path so the tests below can prove what the scan
    catches by feeding it a modified copy of a real file, without writing to the
    working tree.
    """
    exempt_lines = {entry.line for entry in DOCUMENTED_EXEMPTIONS.get(rel_path, [])}
    found = []
    lines = source.splitlines()
    for index, line in enumerate(lines):
        if not USER_SUPPLIED_TARGET.search(line):
            continue
        window = '\n'.join(lines[index:index + GUARD_WINDOW + 1])
        if GUARDED.search(window):
            continue
        if line.strip() in exempt_lines:
            continue
        found.append(f'{rel_path}:{index + 1}: {line.strip()}')
    return found


def exemption_drift(sources=None):
    """Exemptions whose line is missing, or no longer appears exactly N times.

    `sources` maps a relative path to text, for the tests that check a
    hypothetical file. It defaults to what is on disk.
    """
    drift = []
    for rel, entries in DOCUMENTED_EXEMPTIONS.items():
        if sources is not None and rel not in sources:
            continue
        text = sources[rel] if sources is not None else (APP_ROOT.parent / rel).read_text()
        stripped = [line.strip() for line in text.splitlines()]
        for entry in entries:
            actual = stripped.count(entry.line)
            if actual != entry.occurrences:
                drift.append(
                    f'{rel}: {entry.line!r} appears {actual}x, declared {entry.occurrences}x')
    return drift


def test_every_user_supplied_redirect_target_is_checked_or_documented():
    unguarded = []
    for path in sorted(APP_ROOT.rglob('*.py')):
        unguarded.extend(unguarded_reads(path.read_text(), _relative(path)))
    assert not unguarded, (
        'These read a user-supplied redirect target without '
        'safe_redirect_target(). Route each through it, or add it to '
        'DOCUMENTED_EXEMPTIONS in this file with the reason:\n  '
        + '\n  '.join(unguarded))


def test_a_new_unguarded_read_appended_to_an_exempt_file_is_caught():
    """The hole the re-review found, closed and pinned.

    app/user/routes.py carries an exemption. While exemptions were SUBSTRINGS,
    this appended route matched the exempt fragment
    `return_to = request.args.get('return_to')` and sailed through -- a brand
    new open redirect, in an already-exempt file, reported as clean.
    """
    rel = 'app/user/routes.py'
    clean = (APP_ROOT.parent / rel).read_text()
    assert unguarded_reads(clean, rel) == []

    hostile = (
        "\n\n@bp.route('/u/<actor>/brand_new', methods=['POST'])\n"
        "def brand_new(actor):\n"
        "    return_to = request.args.get('return_to', f'/u/{actor}').strip()\n"
        "    return redirect(return_to)\n"
    )
    found = unguarded_reads(clean + hostile, rel)
    assert len(found) == 1, found
    assert "request.args.get('return_to'" in found[0]


def test_a_second_copy_of_an_exempt_line_is_caught():
    """An exemption covers ONE occurrence. A second copy of that exact line is a
    second site, and the declared count is what notices -- the line-text match
    alone would wave it through, which is why the count exists."""
    rel = 'app/user/routes.py'
    clean = (APP_ROOT.parent / rel).read_text()
    duplicated = (clean + "\n\ndef another(actor):\n"
                          "    return_to = request.args.get('return_to')\n"
                          "    return redirect(return_to)\n")

    # The scan exempts by line text, so it does NOT catch this one...
    assert unguarded_reads(duplicated, rel) == []
    # ...the occurrence count does.
    drift = exemption_drift({rel: duplicated})
    assert len(drift) == 1, drift
    assert 'appears 2x, declared 1x' in drift[0]


def test_a_guard_further_than_the_window_is_caught():
    """GUARD_WINDOW is a real limit, asserted rather than assumed."""
    source = ("def f(actor):\n"
              "    candidate = request.args.get('return_to')\n"
              "    a = 1\n"
              "    b = 2\n"
              "    c = 3\n"
              "    d = 4\n"
              "    return safe_redirect_target(candidate, '/')\n")
    assert len(unguarded_reads(source, 'app/nowhere.py')) == 1

    within = ("def f(actor):\n"
              "    candidate = request.args.get('return_to')\n"
              "    if candidate.startswith('http'):\n"
              "        abort(401)\n"
              "    return safe_redirect_target(candidate, '/')\n")
    assert unguarded_reads(within, 'app/nowhere.py') == []


def test_the_exemption_list_does_not_rot():
    """An exemption naming a line that no longer exists -- or that now names two
    sites instead of one -- is a stale claim. Fail on it."""
    for rel, entries in DOCUMENTED_EXEMPTIONS.items():
        for entry in entries:
            assert entry.reason.strip(), f'{rel}: {entry.line!r} has an empty reason'
            assert entry.occurrences >= 1
    drift = exemption_drift()
    assert not drift, ('DOCUMENTED_EXEMPTIONS has drifted from the code:\n  '
                       + '\n  '.join(drift))


def test_the_bypassable_netloc_guard_is_gone():
    """`urlsplit(x).netloc != ''` was the guard on all three `?next=` sites, and
    it accepted three off-origin spellings. Nothing may reintroduce it."""
    offenders = []
    for path in sorted(APP_ROOT.rglob('*.py')):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if re.search(r'urlsplit\([^)]*\)\.netloc\s*[!=]=', line):
                offenders.append(f'{_relative(path)}:{number}: {line.strip()}')
    assert not offenders, (
        'urlsplit(...).netloc as a redirect guard accepts ///evil.example, '
        '/\\evil.example and \\\\evil.example. Use safe_redirect_target:\n  '
        + '\n  '.join(offenders))
