"""D1398: `get_setting(name, default)` where two callers pass different defaults.

`get_setting` returns the **caller's** default when no row exists:

    def get_setting(name, default=None):
        setting = db.session.query(Settings).filter_by(name=name).first()
        if setting is None:
            return default

and no migration or seed inserts most of these rows. So on an instance that has never
saved the page in question, the default IS the behaviour -- and two callers with
different defaults are two different behaviours for one setting.

`captcha_enabled` had three callers and two answers:

    app/auth/forms.py:41            get_setting('captcha_enabled', True)   enforces it
    app/activitypub/util.py:4396    get_setting('captcha_enabled', True)   advertises it
    app/admin/routes.py:407         get_setting('captcha_enabled', False)  pre-fills the box

Measured on a fresh database:

    PROBE  rows for captcha_enabled:              0
           RegistrationForm has captcha field:    True
           GET /admin/misc rendered the box as:   False

So the registration captcha was required, and the admin page said it was not. That
alone is a lie on a settings screen; what makes it a defect is the next save. The Misc
page writes every one of its settings on submit, so an admin changing something
unrelated posts the unticked box back, `set_setting('captcha_enabled', False)` runs,
and the captcha is genuinely off -- having been shown as off the whole time. Round
203's failure mode (a pre-fill disagreeing with reality resets the setting on the next
save) arriving through a mismatched default rather than a missing line.

THE SWEEP. Every `get_setting` call in `app/` with a literal name, grouped by name:
31 names, of which 7 had callers whose defaults differed textually. Six are benign and
each was read rather than assumed:

  * `use_allowlist` -- `False` at four sites, omitted at five. `None` is falsy and
    every consumer uses it in a boolean test, so the two agree in behaviour;
  * `admin_ids` -- omitted at `app/request_hooks.py:97`, which tests
    `if g.admin_ids is None:` on the very next line and computes the list itself;
  * `actor_blocked_words`, `actor_bio_blocked_words` -- omitted at their consumers,
    which open with `if blocked_words and blocked_words.strip() != ''`;
  * `announcement`, `announcement_html` -- omitted at the readers, which render the
    value only when it is truthy.

`captcha_enabled` was the only one where the two defaults meant two different things,
and the only one where one of the callers was an admin pre-fill.

The rule is asserted over the source below, with those six as a named allowlist
carrying its reason, so a future `True` against `False` fails in the suite.
"""
import ast
import pathlib
from collections import defaultdict

import pytest
from flask import g

from app import db
from app.models import Settings, Site
from app.utils import get_setting

# The four source-level tests need no database; the behavioural class does, and the
# `site` fixture is what puts Site id 1 there.
pytestmark = pytest.mark.usefixtures('site')

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Names whose callers differ only between a falsy literal and an omitted default, where
# every consumer resolves the two the same way. Each entry is a promise that the
# difference was read, not waved through -- see this module's docstring.
BENIGN_DISAGREEMENTS = {
    'use_allowlist',            # None is falsy; every consumer is a boolean test
    'admin_ids',                # request_hooks tests `is None` and computes it itself
    'actor_blocked_words',      # consumer opens `if blocked_words and ...`
    'actor_bio_blocked_words',  # the same
    'announcement',             # rendered only when truthy
    'announcement_html',        # the same; its default is a nested get_setting
}


def _get_setting_calls():
    """{name: [(file, line, default_source)]} for every literal-named call in app/."""
    calls = defaultdict(list)
    for path in sorted((ROOT / 'app').glob('**/*.py')):
        rel = str(path.relative_to(ROOT))
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == 'get_setting'):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            default = ast.unparse(node.args[1]) if len(node.args) > 1 else '<omitted>'
            calls[node.args[0].value].append((rel, node.lineno, default))
    return calls


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------


def test_no_setting_has_callers_whose_defaults_disagree():
    """The sweep, pinned. A name outside the allowlist whose callers pass different
    defaults is a setting with two behaviours on a fresh instance.

    The allowlist is deliberately by NAME and not by "one of them is falsy": that
    would pass `True` against `''`, and it would have passed `captcha_enabled` the
    moment someone wrote `0` instead of `False`.
    """
    calls = _get_setting_calls()
    disagreeing = {name for name, sites in calls.items()
                   if len({default for _, _, default in sites}) > 1}

    assert disagreeing - BENIGN_DISAGREEMENTS == set()


def test_the_allowlist_holds_no_name_that_has_stopped_disagreeing():
    """The other direction. An allowlist entry that no longer disagrees is a note
    about code that has moved on, and it would hide a fresh disagreement on that same
    name."""
    calls = _get_setting_calls()
    still_disagreeing = {name for name, sites in calls.items()
                         if len({default for _, _, default in sites}) > 1}

    assert BENIGN_DISAGREEMENTS - still_disagreeing == set()


def test_every_allowlisted_name_differs_only_by_an_omitted_default():
    """What makes the six benign: one group of callers passes a falsy literal and the
    rest pass nothing, so `get_setting` answers None where the others answer their
    literal. A name where two callers passed two DIFFERENT literals would not belong
    here, and this row is what says so.
    """
    calls = _get_setting_calls()
    for name in sorted(BENIGN_DISAGREEMENTS):
        defaults = {default for _, _, default in calls[name]}
        literals = defaults - {'<omitted>'}
        assert '<omitted>' in defaults, name
        assert all(literal in ("False", "''", '[]', "get_setting('announcement')")
                   for literal in literals), (name, literals)


def test_the_captcha_setting_reads_the_same_default_everywhere():
    """D1398 by name, so the repair cannot be undone quietly. Three callers, one
    answer, and the answer is `True`: the captcha is required until an admin turns it
    off, not off until one turns it on."""
    calls = _get_setting_calls()
    defaults = {default for _, _, default in calls['captcha_enabled']}

    assert defaults == {'True'}
    assert len(calls['captcha_enabled']) == 3


# --------------------------------------------------------------------------
# The behaviour, on a database with no rows for it
# --------------------------------------------------------------------------


@pytest.fixture
def env(app, api_baseline):
    """An admin who stays an admin across TWO requests.

    Every test below makes a GET and then a POST, and the permission check runs on
    each. A user granted the permission through `grant_permission` in the fixture is
    refused on the second request -- `g.admin_ids` is per-request and the route reads
    it -- which arrives as a 200 that never reached the route at all and reads exactly
    like a validation failure. `api_baseline.user1` is id 1, whom the admin decorators
    accept, and it is the shape tests/test_admin_community_edit.py uses for the same
    reason.
    """
    from types import SimpleNamespace

    g.admin_ids = [api_baseline.user1.id]
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(api_baseline.user1.id)
        session['_fresh'] = True

    # Minted ONCE and reused. CSRF is on for this app -- a POST without a token is a
    # bare 400 before the route runs (fact 749) -- and `generate_csrf` inside a fresh
    # `test_request_context` is `KeyError: 'csrf_token'` the second time a test calls
    # it, so a helper that mints per call works once and then fails.
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf
    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw

    return SimpleNamespace(app=app, client=client, admin=api_baseline.user1,
                           token=token)


def _misc_form(env):
    from unittest.mock import patch
    captured = {}

    def fake_render(template, **kwargs):
        captured.update(kwargs)
        return 'rendered'

    with patch('app.admin.routes.render_template', side_effect=fake_render):
        response = env.client.get('/admin/misc')
    assert response.status_code == 200
    return captured['form']


class TestOnAnInstanceThatHasNeverSavedTheSetting:
    def test_there_is_no_row_at_all(self, env):
        """The premise, asserted rather than assumed: if a migration ever seeds this
        row, the defaults stop being the behaviour and the rest of this class is
        measuring something else."""
        assert Settings.query.filter_by(name='captcha_enabled').count() == 0

    def test_the_captcha_is_required(self, env):
        """`RegistrationForm.__init__` deletes the field when the setting is off, so
        the field being present is the enforcement.

        `_fields`, NOT `hasattr`. `delattr(self, 'captcha')` pops the name from
        WTForms' `_fields` dict, but the CLASS still carries its `UnboundField`, so
        `hasattr(form, 'captcha')` is True either way -- the first draft of this class
        used it and the "captcha is on" row passed while the "captcha is off" row
        could not.
        """
        from app.auth.forms import RegistrationForm

        with env.app.test_request_context('/'):
            assert 'captcha' in RegistrationForm()._fields

    def test_the_admin_page_says_the_same(self, env):
        """The defect. This rendered False while the row above rendered True."""
        assert _misc_form(env).captcha_enabled.data is True

    def test_the_nodeinfo_document_says_the_same(self, env):
        """The third caller, which tells other servers whether a captcha is
        required."""
        assert get_setting('captcha_enabled', True) is True

    def test_saving_the_page_unchanged_leaves_the_captcha_on(self, env):
        """What the mismatch cost, end to end: read the page, post it back with the
        box as it was rendered, and the captcha must survive.

        Before the repair the box arrived unticked, so this post wrote
        `set_setting('captcha_enabled', False)` and the registration form lost its
        captcha field -- an admin turning off an anti-abuse control by saving a page
        they had changed nothing else on.
        """
        from app.auth.forms import RegistrationForm
        form = _misc_form(env)
        assert form.captcha_enabled.data is True

        _save_misc(env, captcha_enabled='y')

        assert get_setting('captcha_enabled', True) is True
        with env.app.test_request_context('/'):
            assert 'captcha' in RegistrationForm()._fields

    def test_an_admin_can_still_turn_it_off_on_purpose(self, env):
        """The control. A repair that ignored the box would pass every row above and
        break the setting."""
        from app.auth.forms import RegistrationForm

        _save_misc(env, captcha_enabled=None)

        assert get_setting('captcha_enabled', True) is False
        with env.app.test_request_context('/'):
            assert 'captcha' not in RegistrationForm()._fields

    def test_turning_it_off_and_back_on_round_trips(self, env):
        """And the row, once written, is what both callers read -- the defaults stop
        mattering the moment an admin saves."""
        _save_misc(env, captcha_enabled=None)
        assert _misc_form(env).captcha_enabled.data is False

        _save_misc(env, captcha_enabled='y')

        assert _misc_form(env).captcha_enabled.data is True
        assert get_setting('captcha_enabled', False) is True


def _save_misc(env, **overrides):
    """POST /admin/misc with the fields its form validates with.

    Only the settings this file is about are named; everything else takes the value
    the GET handed back, so the save is as close as a test can get to an admin
    pressing Save without changing anything.
    """
    form = _misc_form(env)
    # `language_id` is required and its choices come from the Language table.
    # `default_theme` is pre-filled `''` when `Site.default_theme` is NULL, and `''`
    # is NOT one of `theme_list()`'s choices -- a real browser posts the first option
    # of the select instead, which is `piefed`, so that is what this sends. Posting
    # the pre-filled `''` verbatim is a shape no browser produces and it fails
    # validation. Both learned from `form.errors`, since a failed validation
    # re-renders with a 200 and reads as a refusal.
    from app.models import Language
    english = Language.query.filter(Language.code == 'en').first()
    if english is None:
        english = Language(code='en', name='English')
        db.session.add(english)
        db.session.commit()

    data = {'csrf_token': env.token, 'language_id': str(english.id),
            'default_theme': 'piefed'}
    for field in form:
        if field.name in ('csrf_token', 'submit', 'language_id', 'default_theme'):
            continue
        value = field.data
        if isinstance(value, bool):
            if value:
                data[field.name] = 'y'
        elif value is None:
            continue
        else:
            data[field.name] = str(value)
    for name, value in overrides.items():
        if value is None:
            data.pop(name, None)
        else:
            data[name] = value
    from unittest.mock import patch
    captured = {}

    def fake_render(template, **kwargs):
        captured.update(kwargs)
        return 'rendered'

    # `admin_misc` does NOT redirect on success: it flashes and falls through to its
    # own `render_template`, so a saved page and a refused one are both 200. The
    # errors are the only thing that tells them apart, which is why a failure here
    # names them.
    with patch('app.admin.routes.render_template', side_effect=fake_render):
        response = env.client.post('/admin/misc', data=data)
    assert response.status_code == 200
    saved = captured.get('form')
    assert saved is not None and saved.errors == {}, \
        f'save was refused: {saved.errors if saved is not None else "no render"}'
    return response
