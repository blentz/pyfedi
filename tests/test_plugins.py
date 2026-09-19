"""app/plugins -- the loader, the hook registry, and the example plugin.

MEASUREMENT BASIS. On the full-suite --cov=app run at f3b915ebf the package
carried 71 missing statements and 39 missing arcs: `__init__.py` at 43.59,
`example_plugin/__init__.py` at 47.368, `hooks.py` at 80.282.

One defect is pinned here:

  P1  `int(os.environ.get('FLASK_DEBUG', '0'))`, in nine places, raises for
      FLASK_DEBUG=true -- and because hook registration runs while a plugin is
      being IMPORTED, load_plugins' `except Exception` swallows it and every
      plugin silently fails to load.

THE HOOK REGISTRY IS GLOBAL MUTABLE STATE. `_hooks` and `_plugin_hooks` live in
app/plugins/hooks.py and survive between tests, so every test here runs inside
the `clean_hooks` fixture rather than calling clear_hooks() by hand -- a
forgotten call leaks handlers into whatever runs next, including the real
example plugin's, which the app registers at import.
"""
import os

import pytest
from unittest.mock import patch

from app.plugins import hooks


@pytest.fixture
def clean_hooks():
    """An empty registry for the test, and the app's own hooks restored after.

    The example plugin registers seven handlers when the application imports
    it, and other tests in this suite fire hooks -- so the fixture saves what
    was there, clears, and puts it back.
    """
    saved_hooks = {name: list(handlers) for name, handlers in hooks._hooks.items()}
    saved_plugin_hooks = {plugin: {hook: list(names) for hook, names in registered.items()}
                          for plugin, registered in hooks._plugin_hooks.items()}
    hooks.clear_hooks()
    yield
    hooks.clear_hooks()
    hooks._hooks.update(saved_hooks)
    hooks._plugin_hooks.update(saved_plugin_hooks)


# --------------------------------------------------------------------------
# P1: FLASK_DEBUG
# --------------------------------------------------------------------------


@pytest.mark.parametrize('value', ['1', 'true', 'True', 'yes', 'TRUE'])
def test_a_debug_flag_in_any_common_spelling_registers_a_hook(app, clean_hooks, value):
    """Before the repair, anything but a number raised:

        PROBE g1 registration exception: ValueError invalid literal for int()
        with base 10: 'true'

    -- and registration happens while a plugin is being imported, so
    load_plugins caught it, logged `Failed to load plugin`, and moved on. The
    system did not crash; every plugin simply was not there.
    """
    with patch.dict(os.environ, {'FLASK_DEBUG': value}):
        @hooks.hook('pinned_hook')
        def handler(data):
            return data

    assert hooks.get_registered_hooks()['pinned_hook'] == ['handler']


@pytest.mark.parametrize('value', ['0', '', 'false', 'no', 'nonsense'])
def test_a_flag_that_is_not_set_leaves_the_logging_off(app, clean_hooks, value):
    """The other half: everything else reads as off, including a value nobody
    defined -- which is what the old `int()` would have raised on.
    """
    from app.plugins.hooks import debug_logging_enabled

    with patch.dict(os.environ, {'FLASK_DEBUG': value}):
        assert debug_logging_enabled() is False

        @hooks.hook('quiet_hook')
        def handler(data):
            return data

    assert hooks.get_registered_hooks()['quiet_hook'] == ['handler']


def test_the_flag_is_off_when_it_is_not_set_at_all(app, clean_hooks):
    from app.plugins.hooks import debug_logging_enabled
    environment = {key: value for key, value in os.environ.items() if key != 'FLASK_DEBUG'}

    with patch.dict(os.environ, environment, clear=True):
        assert debug_logging_enabled() is False


def test_a_plugin_loads_with_the_flag_in_a_word(app, clean_hooks):
    """P1's real consequence, end to end: the example plugin is imported by
    load_plugins, and before the repair that import raised inside the loader's
    own exception handler.
    """
    from app import plugins

    with patch.dict(os.environ, {'FLASK_DEBUG': 'true'}):
        loaded = plugins.load_plugins()

    assert 'example_plugin' in loaded
    assert loaded['example_plugin']['info']['name'] == 'Example Plugin'
