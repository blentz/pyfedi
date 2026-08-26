"""The admin-configurable redirect policy.

`app.utils.is_safe_redirect_target` used to be same-origin-only. An admin can now
widen WHICH HOSTS it will accept, via the `redirect_policy` setting:

  same_origin       this server only. THE DEFAULT, and what an upgrade keeps.
  trusted_servers   plus instances an admin marked `trusted`.
  federated_servers plus every instance we federate with.
  all_referrers     any host at all. Dangerous, and labelled so in the admin UI.

THE DISTINCTION THESE TESTS EXIST TO PIN: policy widens which hosts are
acceptable. It must never relax how a URL is PARSED, nor how its host is
determined. Every parsing defence -- the http(s) scheme allowlist, the control
character rejection, the unparseable-URL rejection, the protocol-relative and
backslash-authority rejections, and resolving the real host past any userinfo --
stays active in all four modes, `all_referrers` included.

`TestParsingDefencesHoldInEveryMode` and `TestTheRealHostIsWhatIsMatched` are the
guards for that. If a later change makes the policy branch skip the parsing
block, those fail.
"""

import pytest
from flask import session
from flask_wtf.csrf import generate_csrf

from app import cache, db
from app.admin.forms import SiteMiscForm
from app.models import BannedInstances, Instance, Language, Settings, User
from app.utils import (
    REDIRECT_POLICY_ALL_REFERRERS,
    REDIRECT_POLICY_FEDERATED_SERVERS,
    REDIRECT_POLICY_SAME_ORIGIN,
    REDIRECT_POLICY_SETTING,
    REDIRECT_POLICY_TRUSTED_SERVERS,
    back,
    get_setting,
    instance_redirect_allowed,
    is_safe_redirect_target,
    set_setting,
)
from tests.factories import make_instance

pytestmark = pytest.mark.usefixtures('site')

# SERVER_NAME under test is 'test.piefed.local' (.env.test).

ALL_FOUR_MODES = [
    REDIRECT_POLICY_SAME_ORIGIN,
    REDIRECT_POLICY_TRUSTED_SERVERS,
    REDIRECT_POLICY_FEDERATED_SERVERS,
    REDIRECT_POLICY_ALL_REFERRERS,
]


@pytest.fixture
def policy(db_session):
    """Store a redirect policy for one test, then put the old value back.

    The `app` fixture is session-scoped and `get_setting` is memoized, so a
    leaked policy would silently change how every later test's redirects behave.
    Restoration runs in a `finally`, and the memoized read is invalidated either
    way.
    """
    original = get_setting(REDIRECT_POLICY_SETTING, None)

    def set_policy(value):
        set_setting(REDIRECT_POLICY_SETTING, value)

    try:
        yield set_policy
    finally:
        if original is None:
            db.session.query(Settings).filter_by(name=REDIRECT_POLICY_SETTING).delete()
            db.session.commit()
        else:
            set_setting(REDIRECT_POLICY_SETTING, original)
        cache.delete_memoized(get_setting)


@pytest.fixture
def peers(db_session):
    """One instance of each kind the policy has to tell apart.

    `unknown.example` is deliberately absent -- it is the host no mode below
    `all_referrers` may accept.
    """
    trusted = make_instance('trusted.example')
    trusted.trusted = True
    make_instance('federated.example')
    make_instance('banned.example')
    db.session.add(BannedInstances(domain='banned.example'))
    gone = make_instance('gone.example')
    gone.gone_forever = True
    banned_and_trusted = make_instance('banned-but-trusted.example')
    banned_and_trusted.trusted = True
    db.session.add(BannedInstances(domain='banned-but-trusted.example'))
    gone_and_trusted = make_instance('gone-but-trusted.example')
    gone_and_trusted.trusted = True
    gone_and_trusted.gone_forever = True
    db.session.commit()


class TestTheDefaultIsSameOrigin:
    """The upgrade-safety guard. An existing install has no `redirect_policy`
    row, and must keep today's same-origin behaviour without an admin touching
    anything."""

    def test_with_no_setting_stored_a_known_instance_is_still_rejected(self, app, peers):
        assert db.session.query(Settings).filter_by(name=REDIRECT_POLICY_SETTING).first() is None
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://federated.example/x') is False

    def test_with_no_setting_stored_a_trusted_instance_is_still_rejected(self, app, peers):
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://trusted.example/x') is False

    def test_with_no_setting_stored_our_own_host_is_still_accepted(self, app, peers):
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://test.piefed.local/x') is True

    def test_with_no_setting_stored_a_relative_url_is_still_accepted(self, app, peers):
        with app.test_request_context('/'):
            assert is_safe_redirect_target('/foo') is True

    def test_an_unrecognised_stored_value_falls_back_to_same_origin(self, app, peers, policy):
        """Fail closed. A typo, a hand-edited row or a value from a future
        version must not widen anything."""
        policy('let-anyone-in-please')
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://federated.example/x') is False
            assert is_safe_redirect_target('https://test.piefed.local/x') is True


class TestSameOriginMode:
    def test_our_own_host_passes(self, app, peers, policy):
        policy(REDIRECT_POLICY_SAME_ORIGIN)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://test.piefed.local/x') is True

    def test_a_trusted_instance_does_not_pass(self, app, peers, policy):
        policy(REDIRECT_POLICY_SAME_ORIGIN)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://trusted.example/x') is False


class TestTrustedServersMode:
    def test_a_trusted_instance_passes(self, app, peers, policy):
        policy(REDIRECT_POLICY_TRUSTED_SERVERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://trusted.example/x') is True

    def test_a_known_but_untrusted_instance_does_not_pass(self, app, peers, policy):
        policy(REDIRECT_POLICY_TRUSTED_SERVERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://federated.example/x') is False

    def test_an_unknown_host_does_not_pass(self, app, peers, policy):
        policy(REDIRECT_POLICY_TRUSTED_SERVERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://unknown.example/x') is False

    def test_our_own_host_still_passes(self, app, peers, policy):
        policy(REDIRECT_POLICY_TRUSTED_SERVERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://test.piefed.local/x') is True
            assert is_safe_redirect_target('/foo') is True

    def test_a_trusted_instance_that_is_also_banned_does_not_pass(self, app, peers, policy):
        """Contradictory state, resolved the safe way: a ban is a later, more
        deliberate admin act than the trusted flag, so it wins."""
        policy(REDIRECT_POLICY_TRUSTED_SERVERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://banned-but-trusted.example/x') is False

    def test_a_trusted_instance_that_is_gone_forever_does_not_pass(self, app, peers, policy):
        policy(REDIRECT_POLICY_TRUSTED_SERVERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://gone-but-trusted.example/x') is False

    def test_the_host_is_matched_case_insensitively(self, app, peers, policy):
        policy(REDIRECT_POLICY_TRUSTED_SERVERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://TRUSTED.Example/x') is True


class TestFederatedServersMode:
    def test_a_known_instance_passes(self, app, peers, policy):
        policy(REDIRECT_POLICY_FEDERATED_SERVERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://federated.example/x') is True

    def test_a_trusted_instance_also_passes(self, app, peers, policy):
        policy(REDIRECT_POLICY_FEDERATED_SERVERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://trusted.example/x') is True

    def test_an_unknown_host_does_not_pass(self, app, peers, policy):
        policy(REDIRECT_POLICY_FEDERATED_SERVERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://unknown.example/x') is False

    def test_a_banned_instance_does_not_pass(self, app, peers, policy):
        """We would not send an activity there; we do not send a person there
        either."""
        policy(REDIRECT_POLICY_FEDERATED_SERVERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://banned.example/x') is False

    def test_a_gone_forever_instance_does_not_pass(self, app, peers, policy):
        """gone_forever means ~12 days unreachable. The row no longer tells us
        who answers on that domain -- a lapsed domain can be re-registered by
        anyone -- so its identity is no longer evidence of anything."""
        policy(REDIRECT_POLICY_FEDERATED_SERVERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://gone.example/x') is False

    def test_a_dormant_instance_still_passes(self, app, peers, policy):
        """Dormant is a transient 5-day send-failure state that clears itself.
        It is not a statement about who owns the domain, so it does not
        disqualify a redirect target the way gone_forever does."""
        dormant = make_instance('dormant.example')
        dormant.dormant = True
        db.session.commit()
        policy(REDIRECT_POLICY_FEDERATED_SERVERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://dormant.example/x') is True

    def test_our_own_host_still_passes(self, app, peers, policy):
        policy(REDIRECT_POLICY_FEDERATED_SERVERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://test.piefed.local/x') is True
            assert is_safe_redirect_target('/foo') is True


class TestAllReferrersMode:
    def test_a_host_we_have_never_heard_of_passes(self, app, peers, policy):
        policy(REDIRECT_POLICY_ALL_REFERRERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://unknown.example/x') is True

    def test_a_banned_instance_passes_too(self, app, peers, policy):
        """`all_referrers` means all referrers. This is the mode's danger, and
        the admin UI says so."""
        policy(REDIRECT_POLICY_ALL_REFERRERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://banned.example/x') is True

    def test_our_own_host_still_passes(self, app, peers, policy):
        policy(REDIRECT_POLICY_ALL_REFERRERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://test.piefed.local/x') is True
            assert is_safe_redirect_target('/foo') is True

    def test_something_that_is_not_a_host_at_all_still_does_not_pass(self, app, peers, policy):
        """No host is not "any host"."""
        policy(REDIRECT_POLICY_ALL_REFERRERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://') is False


class TestParsingDefencesHoldInEveryMode:
    """The guards against policy accidentally relaxing parsing.

    Every case here is rejected for a reason that has nothing to do with which
    host it names, so widening the host rule must not touch it.
    """

    @pytest.mark.parametrize('mode', ALL_FOUR_MODES)
    @pytest.mark.parametrize('url', [
        'javascript:alert(1)',
        'JavaScript:alert(1)',
        'javascript:alert(document.domain)',
        'javascript://trusted.example/%0aalert(1)',
    ])
    def test_javascript_is_never_a_redirect_target(self, app, peers, policy, mode, url):
        policy(mode)
        with app.test_request_context('/'):
            assert is_safe_redirect_target(url) is False

    @pytest.mark.parametrize('mode', ALL_FOUR_MODES)
    @pytest.mark.parametrize('url', [
        'data:text/html,<script>alert(1)</script>',
        'file:///etc/passwd',
        'ftp://trusted.example/x',
        'vbscript:msgbox(1)',
    ])
    def test_no_other_scheme_becomes_a_redirect_target_either(self, app, peers, policy, mode, url):
        policy(mode)
        with app.test_request_context('/'):
            assert is_safe_redirect_target(url) is False

    @pytest.mark.parametrize('mode', ALL_FOUR_MODES)
    @pytest.mark.parametrize('url', [
        '/foo\x00bar',
        'https://trusted.example/\x00',
        'https://trusted.example/a\x1fb',
        'https://trusted.example/\x7f',
        'https://trusted\x00.example/x',
        'https://trusted\x1f.example/x',
    ])
    def test_a_control_character_url_is_rejected(self, app, peers, policy, mode, url):
        policy(mode)
        with app.test_request_context('/'):
            assert is_safe_redirect_target(url) is False

    @pytest.mark.parametrize('mode', ALL_FOUR_MODES)
    @pytest.mark.parametrize('char', ['\x1c', '\x1d', '\x1e', '\x1f'])
    def test_a_trailing_separator_character_is_stripped_rather_than_rejected(self, app, peers,
                                                                            policy, mode, char):
        """Honest limit of the control-character rule, pinned so nobody reads it
        as stronger than it is.

        Python's `str.strip()` counts the four ASCII separator characters
        \\x1c-\\x1f as whitespace, so a LEADING or TRAILING one is removed before
        the control-character test ever sees it. That is safe, and this test is
        the proof of why: stripping the ends cannot change the host, so the URL
        is then decided on exactly the host it always named -- rejected under
        `same_origin`, accepted under `all_referrers`. An EMBEDDED separator
        (the case above, which COULD change a host) is still rejected in every
        mode.
        """
        policy(mode)
        with app.test_request_context('/'):
            expected = mode != REDIRECT_POLICY_SAME_ORIGIN
            assert is_safe_redirect_target(f'https://trusted.example/x{char}') is expected

    @pytest.mark.parametrize('mode', ALL_FOUR_MODES)
    @pytest.mark.parametrize('url', [
        'http://[::1',
        'https://trusted.example:notaport/x',
        'https://trusted.example:99999/x',
    ])
    def test_an_unparseable_url_is_rejected_and_does_not_raise(self, app, peers, policy, mode, url):
        policy(mode)
        with app.test_request_context('/'):
            assert is_safe_redirect_target(url) is False

    @pytest.mark.parametrize('mode', ALL_FOUR_MODES)
    @pytest.mark.parametrize('url', [
        '//trusted.example/x',
        '///trusted.example/x',
        '/\\trusted.example',
        '\\\\trusted.example',
    ])
    def test_an_authority_with_no_scheme_is_rejected(self, app, peers, policy, mode, url):
        """A scheme-relative or backslash authority is a parsing trick, not a
        host the admin chose to allow. It stays rejected everywhere, including
        under `all_referrers` -- widening WHICH hosts are allowed is not a
        licence to start honouring a shape we have always refused to parse as a
        redirect."""
        policy(mode)
        with app.test_request_context('/'):
            assert is_safe_redirect_target(url) is False

    @pytest.mark.parametrize('mode', ALL_FOUR_MODES)
    @pytest.mark.parametrize('url', ['', '   ', None, 42])
    def test_nothing_and_non_strings_are_rejected(self, app, peers, policy, mode, url):
        policy(mode)
        with app.test_request_context('/'):
            assert is_safe_redirect_target(url) is False


class TestTheRealHostIsWhatIsMatched:
    """Userinfo smuggling cannot disguise one host as another in ANY mode.

    `https://a.example@b.example/` is a URL for b.example: everything before the
    `@` is userinfo the browser discards. The pair of tests below is the proof
    that this function agrees with the browser -- the same URL is rejected when
    the ALLOWED host is in the userinfo, and accepted when the allowed host is
    the real one.
    """

    def test_a_trusted_host_in_the_userinfo_does_not_make_the_url_trusted(self, app, peers, policy):
        policy(REDIRECT_POLICY_TRUSTED_SERVERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://trusted.example@unknown.example/x') is False

    def test_but_a_trusted_host_in_the_authority_does(self, app, peers, policy):
        """The mirror image of the test above: same shape, sides swapped. The
        host that decides is the one after the `@`."""
        policy(REDIRECT_POLICY_TRUSTED_SERVERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://unknown.example@trusted.example/x') is True

    def test_a_known_host_in_the_userinfo_does_not_make_the_url_federated(self, app, peers, policy):
        policy(REDIRECT_POLICY_FEDERATED_SERVERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://federated.example@unknown.example/x') is False

    def test_our_own_host_in_the_userinfo_does_not_make_the_url_same_origin(self, app, peers, policy):
        policy(REDIRECT_POLICY_SAME_ORIGIN)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://test.piefed.local@evil.example/x') is False

    def test_under_all_referrers_it_passes_for_the_host_rule_not_the_smuggling(self, app, peers, policy):
        """`https://our.host@evil.example/` IS accepted under `all_referrers`.

        The reason matters. It is accepted because its real host is
        `evil.example` and this mode accepts every host -- not because the
        userinfo fooled anything. The proof is that the plain URL for the same
        real host is accepted identically, and that the two URLs below, which
        differ only in the userinfo, both follow their REAL host: the first is
        accepted because `evil.example` is allowed here, and under the trusted
        mode above the identical URL was rejected because `evil.example` is not
        an instance."""
        policy(REDIRECT_POLICY_ALL_REFERRERS)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://test.piefed.local@evil.example/x') is True
            assert is_safe_redirect_target('https://evil.example/x') is True


class TestBackHonoursThePolicyEndToEnd:
    """`back()` is one of the two functions the check feeds. This proves the
    setting reaches a real `Location:` header rather than only the predicate."""

    def test_a_federated_referer_is_followed_under_federated_mode(self, app, peers, policy):
        policy(REDIRECT_POLICY_FEDERATED_SERVERS)
        with app.test_request_context('/somewhere', headers={'Referer': 'https://federated.example/x'}):
            assert back('/fallback').headers['Location'] == 'https://federated.example/x'

    def test_the_same_referer_is_not_followed_under_the_default(self, app, peers):
        with app.test_request_context('/somewhere', headers={'Referer': 'https://federated.example/x'}):
            assert back('/fallback').headers['Location'] == '/fallback'

    def test_a_javascript_referer_is_never_followed(self, app, peers, policy):
        policy(REDIRECT_POLICY_ALL_REFERRERS)
        with app.test_request_context('/somewhere', headers={'Referer': 'javascript:alert(1)'}):
            assert back('/fallback').headers['Location'] == '/fallback'


@pytest.fixture
def csrf_enabled(app):
    """admin/misc.html renders `form.csrf_token()`, and that field only exists
    when WTF_CSRF_ENABLED is on -- TestConfig turns it off, so the template
    raises UndefinedError without this. The `app` fixture is session-scoped, so
    the old value goes back in a `finally`.
    """
    original = app.config['WTF_CSRF_ENABLED']
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        yield
    finally:
        app.config['WTF_CSRF_ENABLED'] = original


class TestAdminFormRoundTrip:
    """A real POST to /admin/misc, then a real GET, through the actual form."""

    def test_saving_the_policy_persists_it_and_it_loads_back(self, app, policy, csrf_enabled):
        instance = make_instance('test.piefed.local', software='piefed')
        admin = User(user_name='admin', email='admin@test.piefed.local', instance_id=instance.id,
                     ap_profile_id='https://test.piefed.local/u/admin', verified=True, banned=False)
        db.session.add(admin)
        db.session.add(Language(id=2, code='en', name='English'))
        db.session.commit()
        # user_access() grants user id 1 every permission, so this is the admin.
        assert admin.id == 1

        client = app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(admin.id)
            sess['_fresh'] = True

        # app.utils.login_required calls validate_csrf directly on every POST,
        # which ignores WTF_CSRF_ENABLED -- so this needs a real token even
        # though the test config disables CSRF for forms.
        with app.test_request_context():
            token = generate_csrf()
            raw = session['csrf_token']
        with client.session_transaction() as sess:
            sess['csrf_token'] = raw

        # No 'submit' key: SiteMiscForm and CloseInstanceForm both name their
        # submit field 'submit', and sending it makes the route try the
        # close-instance branch first.
        post = client.post('/admin/misc', data={
            'csrf_token': token,
            'redirect_policy': REDIRECT_POLICY_TRUSTED_SERVERS,
            'allow_video_file_uploads': 'no',
            'default_filter': 'popular',
            'default_theme': 'piefed',
            'registration_mode': 'Open',
            'language_id': '2',
            'read_posts_cutoff': '180',
            'nsfw_country_restriction': '',
            'auto_decline_countries': '',
            'application_question': '',
            'auto_decline_referrers': '',
            'ban_check_servers': '',
            'registration_approved_email': '',
            'additional_css': '',
            'additional_js': '',
        }, follow_redirects=False)
        assert post.status_code == 200

        assert get_setting(REDIRECT_POLICY_SETTING) == REDIRECT_POLICY_TRUSTED_SERVERS

        # And it comes back selected on the next page load.
        page = client.get('/admin/misc')
        assert page.status_code == 200
        body = page.get_data(as_text=True)
        assert f'<option selected value="{REDIRECT_POLICY_TRUSTED_SERVERS}">' in body

    def test_the_dangerous_option_says_so_in_the_rendered_field(self, app, db_session):
        """The risk has to be visible to the admin choosing it, in the markup
        the browser shows -- not only in a comment in the source."""
        with app.test_request_context('/'):
            markup = str(SiteMiscForm().redirect_policy)
        assert f'value="{REDIRECT_POLICY_ALL_REFERRERS}"' in markup
        assert 'DANGEROUS' in markup


class TestTheHelperIsMemoized:
    def test_the_instance_lookup_goes_through_the_cache(self, app, peers, policy):
        """The host lookup is on the redirect hot path, so it must be memoized
        the way instance_banned / instance_online are. cache.delete_memoized
        raises if handed a function that is not."""
        cache.delete_memoized(instance_redirect_allowed)
        with app.test_request_context('/'):
            assert instance_redirect_allowed('trusted.example', True) is True
            assert instance_redirect_allowed('federated.example', True) is False
            assert instance_redirect_allowed('federated.example', False) is True
            assert instance_redirect_allowed('unknown.example', False) is False


class TestNoInstanceRowsAtAll:
    """A brand-new install federates with nobody. Widening the policy must not
    then accept everything."""

    @pytest.mark.parametrize('mode', [REDIRECT_POLICY_TRUSTED_SERVERS,
                                      REDIRECT_POLICY_FEDERATED_SERVERS])
    def test_nothing_off_site_passes(self, app, db_session, policy, mode):
        assert db.session.query(Instance).count() == 0
        policy(mode)
        with app.test_request_context('/'):
            assert is_safe_redirect_target('https://anywhere.example/x') is False
            assert is_safe_redirect_target('/foo') is True
