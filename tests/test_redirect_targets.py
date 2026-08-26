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
        """This route already had `if return_to.startswith('http'): abort(401)`,
        which is why the absolute spellings 401 rather than redirect. It did
        NOT stop `///evil.example` or `/\\evil.example`; those reached
        redirect() intact."""
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


# --------------------------------------------------------------------------
# The completeness pin.
#
# Important 2 of the final review was not only a bug. It was a CLAIM -- "one
# origin check for every user-influenced redirect target" -- that nothing
# established, and a grep disproved it. This test is that grep, run on every
# suite run, with the exemptions written down and reasoned rather than assumed.
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

# A read is fine when the check is applied to it on the same line, or on the
# line immediately after (`x = request.args.get('next')` /
# `if not is_safe_redirect_target(x):`). Matches is_safe_redirect_target too.
GUARDED = re.compile(r'safe_redirect_target\(')

# Every read that is NOT guarded, with the reason it does not need to be.
# `relative path: {distinctive substring of the line: reason}`. A new unguarded
# read anywhere under app/ fails the test until it is either routed through
# safe_redirect_target or added here with a reason.
DOCUMENTED_EXEMPTIONS = {
    'app/utils.py': {
        "for candidate in (request.args.get('next'), request.form.get('referrer')":
            "referrer()'s own body. It calls is_safe_redirect_target on each "
            "candidate on the very next line -- this IS the check, not a way "
            "round it. tests/test_safe_redirect_target.py covers it.",
    },
    'app/main/routes.py': {
        "next = request.args.get('next')":
            "/anoobis does not call redirect(). It renders anoobis.html, whose "
            "script sets location.href once the proof-of-work finishes, and it "
            "applies its own furl-based host check first. Left alone "
            "deliberately: routing it through safe_redirect_target would turn a "
            "raise-on-hostile-host into a silent fallback, which is a behaviour "
            "change on an unauthenticated anti-bot route rather than a security "
            "fix. Its separate defect -- furl('http://[') raises ValueError, so "
            "?next=http://[ is a 500 -- is reported in the final review and left "
            "for its own change.",
    },
    'app/post/routes.py': {
        "referrer=request.form.get('referrer')))":
            "not a redirect target here: the value is passed as a query "
            "parameter to url_for('post.post_block_image_purge_posts'), and the "
            "route that receives it checks it before redirecting.",
        "referrer=request.args.get('referrer'))":
            "render_template keyword. It becomes a hidden field in "
            "post_block_image_purge_posts.html, Jinja-escaped; it is checked "
            "when it comes back and reaches redirect().",
    },
    'app/admin/routes.py': {
        "form=form, referrer=request.args.get('referrer'))":
            "render_template keyword, same template and same reasoning as the "
            "app/post/routes.py entry above.",
    },
    'app/user/routes.py': {
        "return_to = request.args.get('return_to')":
            "user_preview: this value is only passed to render_template. The "
            "follow/unfollow links it builds land on routes that check it "
            "before redirecting.",
    },
}


def _relative(path):
    return str(path.relative_to(APP_ROOT.parent))


def test_every_user_supplied_redirect_target_is_checked_or_documented():
    unguarded = []
    for path in sorted(APP_ROOT.rglob('*.py')):
        rel = _relative(path)
        exemptions = DOCUMENTED_EXEMPTIONS.get(rel, {})
        lines = path.read_text().splitlines()
        for index, line in enumerate(lines):
            if not USER_SUPPLIED_TARGET.search(line):
                continue
            window = line + '\n' + (lines[index + 1] if index + 1 < len(lines) else '')
            if GUARDED.search(window):
                continue
            if any(fragment in line for fragment in exemptions):
                continue
            unguarded.append(f'{rel}:{index + 1}: {line.strip()}')
    assert not unguarded, (
        'These read a user-supplied redirect target without '
        'safe_redirect_target(). Route each through it, or add the line to '
        'DOCUMENTED_EXEMPTIONS in this file with the reason:\n  '
        + '\n  '.join(unguarded))


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


def test_the_exemption_list_does_not_rot():
    """An exemption naming a line that no longer exists is a stale claim. Fail
    on it rather than let the list drift out of step with the code."""
    stale = []
    for rel, exemptions in DOCUMENTED_EXEMPTIONS.items():
        text = (APP_ROOT.parent / rel).read_text()
        for fragment, reason in exemptions.items():
            if fragment not in text:
                stale.append(f'{rel}: {fragment!r}')
            assert reason.strip(), f'{rel}: {fragment!r} has an empty reason'
    assert not stale, ('DOCUMENTED_EXEMPTIONS names lines that no longer exist:\n  '
                       + '\n  '.join(stale))
