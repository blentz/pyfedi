"""D1396: `request.remote_addr in current_app.config['SKIP_RATE_LIMIT_IPS']`.

`is_trusted_request` (app/api/alpha/routes.py) decides who is exempt from the two
rate limits the alpha API puts on credentials:

    /api/alpha/user/login                 20/hour
    /api/alpha/user/verify_credentials     6/hour

and it decided it with `in` against a config value that is a list only while
nothing has set it:

    SKIP_RATE_LIMIT_IPS = os.environ.get('SKIP_RATE_LIMIT_IPS') or ['127.0.0.1']

`os.environ.get` returns a **string**, so an operator who sets the variable -- and
the name invites it; no sample env file mentions it, so there is nothing to copy
the right shape from -- turns `in` from a membership test into a SUBSTRING test.
Measured, with `debug` off:

    PROBE  configured                remote          exempt
           ['127.0.0.1']  (list)     127.0.0.1       True    <- correct
           ['127.0.0.1']  (list)     27.0.0.1        False   <- correct
           '10.0.0.5'                10.0.0.5        True
           '10.0.0.5'                0.0.0.5         True    <- not configured
           '10.0.0.5'                0.0.5           True    <- not an address
           '192.168.1.10'            92.168.1.1      True    <- not configured
           '192.168.1.10'            2.168.1.1       True    <- not configured
           '10.0.0.5,10.0.0.6'       5,10.0.0.6      True
           '10.0.0.5'                9.9.9.9         False

`92.168.1.1` and `2.168.1.1` are routable public addresses. What the exemption
buys their owner is unmetered password guessing against every account on the
instance, for as long as the operator's own address stays configured.

THE FIX IS AT BOTH ENDS OF ONE FUNCTION. `config.ip_list` accepts the
comma-separated string an environment variable carries and the list a Python
config sets, and returns a list either way. `config.py` parses the environment
through it, and `is_trusted_request` normalises whatever it is handed through the
same function -- so a config set some other way (a test, a subclass, a deploy
script writing Python) cannot bring the substring test back. One rule, asserted
here over both.

WHY `debug` IS NOT PART OF THE DEFECT. `if current_app.debug: return True` exempts
everything, which is what a development instance wants. It is also why the two
limits never fire in the suite, and why every row below sets `debug` False
explicitly: with it left alone, a test asserting "this caller is exempt" passes
whatever the rest of the function does.
"""
from pathlib import Path

import pytest

from config import Config, ip_list

# What an operator would plausibly put in the variable, and the callers each
# value wrongly trusted. Every `remote` here is a value the operator did NOT
# configure.
SUBSTRING_CASES = [
    ('10.0.0.5', '0.0.0.5'),
    ('10.0.0.5', '0.0.5'),
    ('10.0.0.5', '.0.0.5'),
    ('192.168.1.10', '92.168.1.1'),
    ('192.168.1.10', '2.168.1.1'),
    ('192.168.1.10', '68.1.10'),
    ('10.0.0.5,10.0.0.6', '5,10.0.0.6'),
    ('203.0.113.42', '3.0.113.4'),
]


@pytest.fixture
def untrusted(app, monkeypatch):
    """`debug` off, so `is_trusted_request` reaches the line under test.

    Returns a caller that sets the config through `monkeypatch.setitem` rather
    than writing it: the `app` fixture is SESSION scoped (tests/conftest.py:219),
    so a plain assignment would leave `SKIP_RATE_LIMIT_IPS` set for every later
    test on the same worker -- and the value these rows set is a deliberately
    wrong one.
    """
    monkeypatch.setattr(app, 'debug', False)

    def asked(configured, remote):
        return _asked(app, monkeypatch, configured, remote)

    return asked


def _asked(app, monkeypatch, configured, remote):
    from app.api.alpha.routes import is_trusted_request
    monkeypatch.setitem(app.config, 'SKIP_RATE_LIMIT_IPS', configured)
    with app.test_request_context('/', environ_base={'REMOTE_ADDR': remote}):
        return is_trusted_request()


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------


class TestTheParser:
    def test_a_comma_separated_string_becomes_a_list(self):
        assert ip_list('10.0.0.5,10.0.0.6') == ['10.0.0.5', '10.0.0.6']

    def test_whitespace_around_an_entry_is_dropped(self):
        """An operator writes `a, b`, not `a,b`."""
        assert ip_list(' 10.0.0.5 , 10.0.0.6 ') == ['10.0.0.5', '10.0.0.6']

    def test_an_empty_entry_is_dropped(self):
        """A trailing comma is not an entry, and an entry of `''` would be `in`
        every string -- the substring failure in its purest form."""
        assert ip_list('10.0.0.5,,') == ['10.0.0.5']
        assert '' not in ip_list('10.0.0.5,,')

    def test_a_list_is_returned_as_a_list(self):
        assert ip_list(['127.0.0.1', '10.0.0.5']) == ['127.0.0.1', '10.0.0.5']

    def test_a_single_address_is_a_list_of_one(self):
        assert ip_list('10.0.0.5') == ['10.0.0.5']

    @pytest.mark.parametrize('value', [None, '', [], '   ', ','])
    def test_nothing_configured_and_no_default_is_an_empty_list(self, value):
        """Empty, never `['']`: an empty list trusts nobody, and a list holding
        the empty string trusts everybody."""
        assert ip_list(value) == []

    @pytest.mark.parametrize('value', [None, ''])
    def test_nothing_configured_falls_back_to_the_default(self, value):
        assert ip_list(value, '127.0.0.1') == ['127.0.0.1']

    def test_a_configured_value_beats_the_default(self):
        assert ip_list('10.0.0.5', '127.0.0.1') == ['10.0.0.5']


def test_the_shipped_configuration_is_a_list_of_one_loopback_address():
    """What an instance that has not set the variable runs with. A string here
    would be the defect back at its source.

    Not sufficient on its own: with the variable unset, `ip_list(...)` and the
    `os.environ.get(...) or ['127.0.0.1']` it replaces produce the SAME list, so
    this row passes against the defect. `test_setting_the_variable_still_yields
    _a_list` below is the one that discriminates.
    """
    assert Config.SKIP_RATE_LIMIT_IPS == ['127.0.0.1']
    assert isinstance(Config.SKIP_RATE_LIMIT_IPS, list)


@pytest.mark.parametrize('value,expected', [
    ('192.168.1.10', ['192.168.1.10']),
    ('10.0.0.5,10.0.0.6', ['10.0.0.5', '10.0.0.6']),
    (' 10.0.0.5 , 10.0.0.6 ', ['10.0.0.5', '10.0.0.6']),
    ('', ['127.0.0.1']),
])
def test_setting_the_variable_still_yields_a_list(monkeypatch, tmp_path, value,
                                                 expected):
    """The row the defect fails. `config.py` reads the environment once, at
    import, so no amount of `monkeypatch.setenv` reaches the `Config` this process
    already holds -- which is exactly why the defect survived: every test saw the
    unset case, the only one that was ever right.

    So config.py is executed again, under a different module name and with the
    variable set, and the value its assignment produces is asserted. Loaded by
    path rather than through `importlib.reload`, which would rebind
    `sys.modules['config']` under the running app.
    """
    import importlib.util
    import sys

    monkeypatch.setenv('SKIP_RATE_LIMIT_IPS', value)
    spec = importlib.util.spec_from_file_location(
        'config_under_test', Path(__file__).resolve().parent.parent / 'config.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop('config_under_test', None)
    spec.loader.exec_module(module)

    assert module.Config.SKIP_RATE_LIMIT_IPS == expected
    assert isinstance(module.Config.SKIP_RATE_LIMIT_IPS, list)


# --------------------------------------------------------------------------
# The caller
# --------------------------------------------------------------------------


class TestWhoIsTrusted:
    @pytest.mark.parametrize('configured,remote', SUBSTRING_CASES)
    def test_an_address_merely_inside_the_configured_string_is_not_trusted(
            self, untrusted, configured, remote):
        """The defect. Each `remote` is a substring of `configured` and is not an
        address the operator listed."""
        assert untrusted(configured, remote) is False

    @pytest.mark.parametrize('configured', ['10.0.0.5', ['10.0.0.5'],
                                            '10.0.0.4,10.0.0.5',
                                            ['10.0.0.4', '10.0.0.5'],
                                            ' 10.0.0.5 '])
    def test_a_configured_address_is_trusted_however_it_was_written(
            self, untrusted, configured):
        """The control, and the point of normalising at the caller too: the
        exemption has to keep working whether the value arrived as the
        environment's string or as a Python list."""
        assert untrusted(configured, '10.0.0.5') is True

    def test_the_loopback_default_trusts_the_loopback(self, untrusted):
        assert untrusted(['127.0.0.1'], '127.0.0.1') is True

    @pytest.mark.parametrize('remote', ['9.9.9.9', '127.0.0.2', '1270.0.1',
                                        '', None])
    def test_an_unrelated_caller_is_not_trusted(self, untrusted, remote):
        assert untrusted(['127.0.0.1'], remote) is False

    @pytest.mark.parametrize('configured', ['', None, [], ','])
    def test_nothing_configured_trusts_nobody(self, untrusted, configured):
        """`ip_list` is called without a default at the caller, so an empty
        configuration exempts no one rather than falling back to a value the
        operator cleared on purpose."""
        assert untrusted(configured, '127.0.0.1') is False

    def test_debug_trusts_everybody(self, app, monkeypatch):
        """Recorded, not repaired: a development instance wants this, and it is
        why the two limits never fire in the suite."""
        monkeypatch.setattr(app, 'debug', True)
        assert _asked(app, monkeypatch, ['127.0.0.1'], '9.9.9.9') is True


# --------------------------------------------------------------------------
# What the exemption reaches
# --------------------------------------------------------------------------


def test_the_two_limits_it_exempts_are_the_credential_ones():
    """Asserted on the source, because it is what makes the substring test worth
    a defect number rather than a tidy-up. Both `exempt_when=is_trusted_request`
    sites guard a password check, so a caller who is wrongly trusted gets
    unmetered guesses.

    A third site added without a limit, or one of these two losing its limit,
    changes what the function above is responsible for -- so the count and the
    limits are both pinned.
    """
    import ast
    import inspect

    import app.api.alpha.routes as routes

    tree = ast.parse(inspect.getsource(routes))
    guarded = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not any(isinstance(kw.value, ast.Name)
                   and kw.value.id == 'is_trusted_request'
                   and kw.arg == 'exempt_when' for kw in node.keywords):
            continue
        guarded.append(node.args[0].value)

    assert sorted(guarded) == ['20/hour', '6/hour']

    source = inspect.getsource(routes)
    for path in ('/user/login', '/user/verify_credentials'):
        assert path in source
