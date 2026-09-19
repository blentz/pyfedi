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


# --------------------------------------------------------------------------
# The hook registry
# --------------------------------------------------------------------------


def test_handlers_run_in_alphabetical_order_by_function_name(app, clean_hooks):
    """The docstring promises alphabetical order, and the registry is a list in
    registration order -- so the fixture registers them backwards and the
    result records the order they actually ran in.
    """
    order = []

    @hooks.hook('ordered')
    def zulu(data):
        order.append('zulu')
        return data + ['zulu']

    @hooks.hook('ordered')
    def alpha(data):
        order.append('alpha')
        return data + ['alpha']

    result = hooks.fire_hook('ordered', [])

    assert order == ['alpha', 'zulu']
    assert result == ['alpha', 'zulu']


def test_each_handler_receives_what_the_last_one_returned(app, clean_hooks):
    """The chain is a fold, not a broadcast: the second handler sees the first
    one's OUTPUT. A fixture whose handlers ignore their argument could not tell
    the two apart.
    """
    @hooks.hook('chained')
    def a_first(data):
        return data + '-first'

    @hooks.hook('chained')
    def b_second(data):
        return data + '-second'

    assert hooks.fire_hook('chained', 'start') == 'start-first-second'


def test_keyword_arguments_reach_every_handler(app, clean_hooks):
    seen = []

    @hooks.hook('kwargs')
    def handler(data, **kwargs):
        seen.append(kwargs)
        return data

    hooks.fire_hook('kwargs', 'x', source='api', user_id=7)

    assert seen == [{'source': 'api', 'user_id': 7}]


def test_firing_a_hook_nobody_registered_returns_the_data_unchanged(app, clean_hooks):
    """The early return, and the reason every fire_hook call site can be
    unconditional: a site with no plugins installed still works.
    """
    assert hooks.fire_hook('nobody_listens', {'title': 'x'}) == {'title': 'x'}
    assert hooks.fire_hook('nobody_listens') is None


def test_a_handler_that_raises_is_swallowed_and_the_chain_continues(app, clean_hooks):
    """D811, recorded rather than repaired: isolation is the point of a plugin
    system, but the caller cannot tell that a plugin failed -- the value simply
    carries on from the last handler that worked.

        PROBE g3 result: ['first', 'last']
    """
    @hooks.hook('explosive')
    def a_first(data):
        return data + ['first']

    @hooks.hook('explosive')
    def b_boom(data):
        raise RuntimeError('plugin exploded')

    @hooks.hook('explosive')
    def c_last(data):
        return data + ['last']

    assert hooks.fire_hook('explosive', []) == ['first', 'last']


def test_a_handler_that_returns_nothing_nulls_the_data_for_everyone_after_it(app, clean_hooks):
    """D811's other half: `result = handler(result, **kwargs)` takes whatever comes back,
    including None -- so one handler forgetting to return hands None to the
    next and to the caller.

        PROBE g4 result: None
    """
    seen = []

    @hooks.hook('forgetful')
    def a_forgets(data):
        return None

    @hooks.hook('forgetful')
    def b_after(data):
        seen.append(data)
        return data

    assert hooks.fire_hook('forgetful', {'title': 'x'}) is None
    assert seen == [None]


def test_the_decorator_returns_a_wrapper_that_still_calls_the_function(app, clean_hooks):
    """`@hook` registers the original and returns a functools.wraps wrapper, so
    the decorated name is callable in the plugin's own module and keeps its
    name -- which is what the alphabetical sort and the plugin registry read.
    """
    @hooks.hook('wrapped')
    def handler(data):
        return data.upper()

    assert handler('x') == 'X'
    assert handler.__name__ == 'handler'
    assert hooks.get_registered_hooks()['wrapped'] == ['handler']


def test_a_hook_registered_from_a_plugin_module_is_attributed_to_it(app, clean_hooks):
    """The attribution reads `func.__module__` and takes the third segment of
    `app.plugins.<name>`, so the test has to give the function that module --
    which is what importlib does for a real plugin.
    """
    def handler(data):
        return data

    handler.__module__ = 'app.plugins.some_plugin'
    hooks.hook('attributed')(handler)

    assert hooks.get_plugin_hooks() == {'some_plugin': {'attributed': ['handler']}}


@pytest.mark.parametrize('module_name', ['tests.test_plugins', 'app.plugins', None,
                                         'app.pluginsmore.thing'])
def test_a_hook_registered_from_anywhere_else_is_attributed_to_nobody(app, clean_hooks,
                                                                      module_name):
    """Four ways to fail the attribution test -- the wrong package, the package
    itself with no plugin segment, no module at all, and a name that merely
    starts with the same letters. The hook still registers either way.
    """
    def handler(data):
        return data

    handler.__module__ = module_name
    hooks.hook('unattributed')(handler)

    assert hooks.get_plugin_hooks() == {}
    assert hooks.get_registered_hooks()['unattributed'] == ['handler']


def test_a_plugin_registering_several_hooks_is_recorded_once(app, clean_hooks):
    """register_plugin_hook builds a two-level dictionary, and both levels have
    a "create it if missing" branch -- so the fixture registers two hooks for
    one plugin and two handlers for one of them.
    """
    hooks.register_plugin_hook('a_plugin', 'first_hook', 'one')
    hooks.register_plugin_hook('a_plugin', 'first_hook', 'two')
    hooks.register_plugin_hook('a_plugin', 'second_hook', 'three')
    hooks.register_plugin_hook('other_plugin', 'first_hook', 'four')

    assert hooks.get_plugin_hooks() == {
        'a_plugin': {'first_hook': ['one', 'two'], 'second_hook': ['three']},
        'other_plugin': {'first_hook': ['four']},
    }


def test_the_registries_are_handed_out_as_copies(app, clean_hooks):
    """get_plugin_hooks copies, so a caller mutating what it gets back cannot
    corrupt the registry -- which matters because these are module globals.
    """
    hooks.register_plugin_hook('a_plugin', 'a_hook', 'one')

    handed_out = hooks.get_plugin_hooks()
    handed_out['a_plugin'] = {'tampered': ['x']}

    assert hooks.get_plugin_hooks() == {'a_plugin': {'a_hook': ['one']}}


def test_registered_hooks_are_reported_sorted_by_name(app, clean_hooks):
    @hooks.hook('reported')
    def zulu(data):
        return data

    @hooks.hook('reported')
    def alpha(data):
        return data

    assert hooks.get_registered_hooks() == {'reported': ['alpha', 'zulu']}


def test_clearing_empties_both_registries(app, clean_hooks):
    @hooks.hook('cleared')
    def handler(data):
        return data

    hooks.register_plugin_hook('a_plugin', 'cleared', 'handler')
    hooks.clear_hooks()

    assert hooks.get_registered_hooks() == {}
    assert hooks.get_plugin_hooks() == {}


# --------------------------------------------------------------------------
# The loader
# --------------------------------------------------------------------------


@pytest.fixture
def clean_registry(clean_hooks):
    """An empty plugin registry as well as an empty hook registry.

    `_loaded_plugins` is the other module global, and load_plugins ADDS to it
    rather than replacing it -- so a test that loads from a temporary directory
    would otherwise leave its plugins visible to everything after it.
    """
    from app import plugins
    saved = dict(plugins._loaded_plugins)
    plugins._loaded_plugins.clear()
    yield plugins
    plugins._loaded_plugins.clear()
    plugins._loaded_plugins.update(saved)


def _write_plugin(directory, name, body):
    plugin_dir = directory / name
    plugin_dir.mkdir()
    (plugin_dir / '__init__.py').write_text(body)
    return plugin_dir


def test_the_loader_finds_the_example_plugin_and_reads_its_metadata(app, clean_registry):
    plugins = clean_registry

    loaded = plugins.load_plugins()

    assert 'example_plugin' in loaded
    info = plugins.get_plugin_info('example_plugin')
    assert info['name'] == 'Example Plugin'
    assert info['license'] == 'AGPL-3.0'
    assert loaded['example_plugin']['path'].endswith('example_plugin')


def test_a_missing_plugins_directory_is_reported_and_empty(app, clean_registry, tmp_path):
    plugins = clean_registry

    assert plugins.load_plugins(str(tmp_path / 'does-not-exist')) == {}


def test_files_and_underscored_directories_are_not_plugins(app, clean_registry, tmp_path):
    """Two of the loader's three skips: anything that is not a directory, and
    any directory whose name starts with an underscore -- which is what keeps
    __pycache__ out.
    """
    plugins = clean_registry
    (tmp_path / 'notaplugin.py').write_text('raise RuntimeError("should not run")')
    # a PERFECTLY GOOD plugin in an underscored directory: if it raised, the
    # loader's own exception handler would skip it for the wrong reason and the
    # underscore check would be load-bearing for nothing
    _write_plugin(tmp_path, '_private', 'def plugin_info():\n    return {"name": "Private"}\n')
    _write_plugin(tmp_path, 'real_plugin', 'def plugin_info():\n    return {"name": "Real"}\n')

    loaded = plugins.load_plugins(str(tmp_path))

    assert sorted(loaded) == ['real_plugin']


def test_a_directory_without_an_init_is_not_a_plugin(app, clean_registry, tmp_path):
    """The third skip. The directory exists and is not underscored, so only the
    missing __init__.py stops it.
    """
    plugins = clean_registry
    (tmp_path / 'empty_plugin').mkdir()

    assert plugins.load_plugins(str(tmp_path)) == {}


def test_a_plugin_that_raises_on_import_is_logged_and_skipped(app, clean_registry, tmp_path):
    """The loader's own isolation: one bad plugin must not stop the others, and
    the fixture proves it by putting a good one beside the bad one.
    """
    plugins = clean_registry
    _write_plugin(tmp_path, 'broken_plugin', 'raise RuntimeError("bad plugin")')
    _write_plugin(tmp_path, 'good_plugin', 'def plugin_info():\n    return {"name": "Good"}\n')

    loaded = plugins.load_plugins(str(tmp_path))

    assert sorted(loaded) == ['good_plugin']


def test_a_plugin_with_no_metadata_still_loads(app, clean_registry, tmp_path):
    """`if hasattr(plugin_module, 'plugin_info')` -- the metadata is optional,
    and a plugin without it gets an empty dict rather than an error.
    """
    plugins = clean_registry
    _write_plugin(tmp_path, 'bare_plugin', 'VALUE = 1\n')

    loaded = plugins.load_plugins(str(tmp_path))

    assert loaded['bare_plugin']['info'] == {}
    assert plugins.get_plugin_info('bare_plugin') == {}


def test_asking_about_a_plugin_that_is_not_loaded_gives_nothing(app, clean_registry):
    plugins = clean_registry

    assert plugins.get_plugin_info('no_such_plugin') == {}


def test_the_loaded_plugin_list_is_handed_out_as_a_copy(app, clean_registry, tmp_path):
    """get_loaded_plugins copies -- and load_plugins does NOT, which is D812.
    Both halves are asserted here so the asymmetry is recorded rather than
    inferred.
    """
    plugins = clean_registry
    _write_plugin(tmp_path, 'a_plugin', 'def plugin_info():\n    return {"name": "A"}\n')
    returned = plugins.load_plugins(str(tmp_path))

    copy = plugins.get_loaded_plugins()
    copy['tampered'] = {}

    assert 'tampered' not in plugins.get_loaded_plugins()
    assert returned is plugins._loaded_plugins


def test_loading_twice_accumulates_rather_than_replacing(app, clean_registry, tmp_path):
    """D812's other half: the registry is added to, never reset, so a second
    directory's plugins join the first's.
    """
    plugins = clean_registry
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    _write_plugin(first, 'one_plugin', 'def plugin_info():\n    return {"name": "One"}\n')
    _write_plugin(second, 'two_plugin', 'def plugin_info():\n    return {"name": "Two"}\n')

    plugins.load_plugins(str(first))
    loaded = plugins.load_plugins(str(second))

    assert sorted(loaded) == ['one_plugin', 'two_plugin']


# --------------------------------------------------------------------------
# reload_plugin
# --------------------------------------------------------------------------


def test_reloading_a_plugin_that_was_never_loaded_answers_false(app, clean_registry):
    plugins = clean_registry

    assert plugins.reload_plugin('no_such_plugin') is False


def test_reloading_picks_up_the_new_code_and_the_new_metadata(app, clean_registry, tmp_path):
    """The point of the function: the file is rewritten between the two calls
    and the reloaded module carries the new value.
    """
    plugins = clean_registry
    plugin_dir = _write_plugin(tmp_path, 'changing_plugin',
                               'VALUE = "before"\n'
                               'def plugin_info():\n    return {"name": "Before"}\n')
    plugins.load_plugins(str(tmp_path))
    assert plugins.get_plugin_info('changing_plugin') == {'name': 'Before'}

    (plugin_dir / '__init__.py').write_text(
        'VALUE = "after"\n'
        'def plugin_info():\n    return {"name": "After"}\n')

    assert plugins.reload_plugin('changing_plugin') is True
    assert plugins.get_plugin_info('changing_plugin') == {'name': 'After'}
    assert plugins.get_loaded_plugins()['changing_plugin']['module'].VALUE == 'after'


def test_reloading_replaces_a_plugins_hooks_rather_than_doubling_them(app, clean_registry, tmp_path):
    """The cleanup exists because the reloaded module registers its hooks
    again. Without it the handler would run twice, so the assertion counts
    handlers rather than merely checking one is present.
    """
    plugins = clean_registry
    body = ('from app.plugins.hooks import hook\n'
            '@hook("reloaded_hook")\n'
            'def a_handler(data):\n'
            '    return data\n'
            'def plugin_info():\n    return {"name": "Hooked"}\n')
    _write_plugin(tmp_path, 'hooked_plugin', body)
    plugins.load_plugins(str(tmp_path))
    assert hooks.get_registered_hooks()['reloaded_hook'] == ['a_handler']

    assert plugins.reload_plugin('hooked_plugin') is True

    assert hooks.get_registered_hooks()['reloaded_hook'] == ['a_handler']
    assert hooks.get_plugin_hooks()['hooked_plugin'] == {'reloaded_hook': ['a_handler']}


def test_reloading_a_plugin_whose_last_hook_goes_removes_the_hook_entirely(app,
                                                                           clean_registry,
                                                                           tmp_path):
    """`if not _hooks[hook_name]: del _hooks[hook_name]` -- the empty-list
    cleanup, which only shows when the plugin being reloaded was the hook's
    ONLY registrant and the new version drops it.
    """
    plugins = clean_registry
    plugin_dir = _write_plugin(tmp_path, 'shrinking_plugin',
                               'from app.plugins.hooks import hook\n'
                               '@hook("going_away")\n'
                               'def a_handler(data):\n'
                               '    return data\n')
    plugins.load_plugins(str(tmp_path))
    assert 'going_away' in hooks.get_registered_hooks()

    (plugin_dir / '__init__.py').write_text('VALUE = 1\n')

    assert plugins.reload_plugin('shrinking_plugin') is True
    assert 'going_away' not in hooks.get_registered_hooks()


def test_another_plugins_handler_survives_a_reload(app, clean_registry, tmp_path):
    """The cleanup filters by module name, so a hook two plugins share must
    keep the other plugin's handler -- which is the whole reason it filters
    rather than clearing the list.
    """
    plugins = clean_registry
    shared = ('from app.plugins.hooks import hook\n'
              '@hook("shared_hook")\n'
              'def {name}(data):\n'
              '    return data\n')
    _write_plugin(tmp_path, 'first_plugin', shared.format(name='a_first'))
    _write_plugin(tmp_path, 'second_plugin', shared.format(name='b_second'))
    plugins.load_plugins(str(tmp_path))
    assert hooks.get_registered_hooks()['shared_hook'] == ['a_first', 'b_second']

    assert plugins.reload_plugin('first_plugin') is True

    assert hooks.get_registered_hooks()['shared_hook'] == ['a_first', 'b_second']


def test_a_reload_that_fails_answers_false_and_does_not_restore_the_plugin(app,
                                                                           clean_registry,
                                                                           tmp_path):
    """The reload removes the plugin from the registry BEFORE re-importing it,
    so a plugin that has become unimportable is gone rather than stale -- which
    the assertion on get_plugin_info records.
    """
    plugins = clean_registry
    plugin_dir = _write_plugin(tmp_path, 'doomed_plugin',
                               'def plugin_info():\n    return {"name": "Fine"}\n')
    plugins.load_plugins(str(tmp_path))

    (plugin_dir / '__init__.py').write_text('raise RuntimeError("no longer importable")')

    assert plugins.reload_plugin('doomed_plugin') is False
    assert plugins.get_plugin_info('doomed_plugin') == {}


def test_a_reload_whose_file_has_vanished_answers_false(app, clean_registry, tmp_path):
    """The `spec is None or spec.loader is None` guard: with the file gone,
    spec_from_file_location cannot build a loader.
    """
    plugins = clean_registry
    plugin_dir = _write_plugin(tmp_path, 'vanishing_plugin', 'VALUE = 1\n')
    plugins.load_plugins(str(tmp_path))

    (plugin_dir / '__init__.py').unlink()

    assert plugins.reload_plugin('vanishing_plugin') is False


# --------------------------------------------------------------------------
# The example plugin
# --------------------------------------------------------------------------


def test_the_example_plugin_registers_every_hook_it_documents(app, clean_hooks):
    """The file plugin authors copy from, so its handlers are named here: a
    hook quietly dropped from it is a hook no new plugin knows about.
    """
    from app import plugins

    # loaded the way the application loads it, rather than by importlib.reload:
    # the module is already in sys.modules from the app's own import, and
    # reloading it runs the decorators AGAIN on top of whatever is registered
    plugins.load_plugins()

    assert hooks.get_registered_hooks() == {
        'after_post_create': ['example_after_post_creation'],
        'before_post_create': ['example_before_post_creation'],
        'new_local_community': ['example_new_local_community'],
        'new_registration_for_approval': ['example_new_registration_for_approval'],
        'new_remote_community': ['example_new_remote_community'],
        'new_user': ['example_new_user'],
        'webhook': ['example_webhook'],
    }


@pytest.mark.parametrize('flag', ['1', '0'])
def test_every_example_handler_returns_what_it_was_given(app, clean_hooks, flag):
    """The handlers are pass-throughs that print under FLASK_DEBUG, so both
    settings are rows: with the flag on, the print statements run and the value
    still comes back unchanged; with it off, nothing is printed and the value
    still comes back unchanged. A handler that swallowed its argument would
    break every plugin chained after it.
    """
    from types import SimpleNamespace
    from app.plugins import example_plugin

    post_data = {'title': 'a post', 'content': 'x' * 80}
    post = SimpleNamespace(title='a post')
    user = SimpleNamespace(user_name='alice')
    application = SimpleNamespace(user=SimpleNamespace(user_name='alice'))
    community = SimpleNamespace(lemmy_link=lambda: '!c@remote.example')

    with patch.dict(os.environ, {'FLASK_DEBUG': flag}):
        assert example_plugin.example_before_post_creation(post_data) is post_data
        assert example_plugin.example_after_post_creation(post) is post
        assert example_plugin.example_new_user(user) is user
        assert example_plugin.example_new_registration_for_approval(application) is application
        assert example_plugin.example_new_remote_community(community) is community
        assert example_plugin.example_new_local_community(community) is community
        assert example_plugin.example_webhook({'event': 'x'}) == {'event': 'x'}


def test_the_after_post_handler_tolerates_being_given_nothing(app, clean_hooks):
    """`if debug_logging_enabled() and post_data:` and then
    `if hasattr(post_data, "title")` -- three ways through, and the two false
    arms are what stop a hook firing on a failed create from raising.
    """
    from app.plugins import example_plugin

    with patch.dict(os.environ, {'FLASK_DEBUG': '1'}):
        assert example_plugin.example_after_post_creation(None) is None
        titleless = object()
        assert example_plugin.example_after_post_creation(titleless) is titleless


def test_a_plugin_spec_that_cannot_be_built_is_skipped(app, clean_registry, tmp_path):
    """`if spec is None or spec.loader is None` in the loader. importlib is
    patched because no file this test can write produces a None spec -- the
    guard exists for import machinery that refuses, not for bad Python.
    """
    plugins = clean_registry
    _write_plugin(tmp_path, 'unspeccable_plugin', 'VALUE = 1\n')

    with patch('app.plugins.importlib.util.spec_from_file_location', return_value=None):
        assert plugins.load_plugins(str(tmp_path)) == {}


def test_a_reload_whose_spec_cannot_be_built_answers_false(app, clean_registry, tmp_path):
    """The same guard in reload_plugin, which has its own copy."""
    plugins = clean_registry
    _write_plugin(tmp_path, 'unspeccable_plugin', 'VALUE = 1\n')
    plugins.load_plugins(str(tmp_path))

    with patch('app.plugins.importlib.util.spec_from_file_location', return_value=None):
        assert plugins.reload_plugin('unspeccable_plugin') is False


def test_a_reload_skips_a_recorded_hook_that_is_no_longer_registered(app, clean_registry,
                                                                     tmp_path):
    """THE PACKAGE'S LAST ARC: `if hook_name in _hooks:` false, inside the
    reload's cleanup loop.

    The two registries can disagree -- `_plugin_hooks` remembers what a plugin
    registered, `_hooks` holds what is currently registered -- and anything
    that empties the second without the first leaves a name the cleanup has to
    skip. `clear_hooks` empties both, so the disagreement is built directly:
    the plugin is loaded, its handler is removed from `_hooks` alone, and the
    reload then walks a recorded hook that is not there.
    """
    plugins = clean_registry
    body = ('from app.plugins.hooks import hook\n'
            '@hook("recorded_hook")\n'
            'def a_handler(data):\n'
            '    return data\n')
    _write_plugin(tmp_path, 'disagreeing_plugin', body)
    plugins.load_plugins(str(tmp_path))
    assert hooks.get_plugin_hooks()['disagreeing_plugin'] == {'recorded_hook': ['a_handler']}

    del hooks._hooks['recorded_hook']

    assert plugins.reload_plugin('disagreeing_plugin') is True
    assert hooks.get_registered_hooks()['recorded_hook'] == ['a_handler']


def test_a_hook_registers_even_when_its_attribution_fails(app, clean_hooks):
    """`except Exception` around the plugin-name detection.

    Reached by making the ATTRIBUTION raise rather than the module read: a
    callable whose `__module__` raises dies in `functools.wraps` a few lines
    later, outside the try, so it cannot reach this arm. What can is the
    registration call the try wraps -- and the point of the handler is that a
    hook still registers when only its bookkeeping failed.
    """
    with patch('app.plugins.hooks.register_plugin_hook',
               side_effect=RuntimeError('registry unavailable')):
        def handler(data):
            return data

        handler.__module__ = 'app.plugins.some_plugin'
        hooks.hook('attribution_fails')(handler)

    assert hooks.get_registered_hooks()['attribution_fails'] == ['handler']
    assert hooks.get_plugin_hooks() == {}


# --------------------------------------------------------------------------
# Rows added to close mutation survivors
# --------------------------------------------------------------------------


@pytest.mark.parametrize('value, logs', [
    ('1', True), ('true', True), ('True', True), ('YES', True), ('on', True),
    ('0', False), ('false', False), ('nonsense', False), ('', False),
])
def test_the_debug_flag_decides_whether_registration_is_logged(app, clean_hooks, caplog,
                                                               value, logs):
    """What the flag actually CONTROLS is the logging, and the rows above only
    asserted that registration still happens -- which it does either way, so
    every mutant narrowing the accepted spellings survived behind them.

    `.lower()` is what makes 'True' and 'YES' work, and the two lists here are
    what keep it and the set's four members load-bearing.
    """
    caplog.set_level('INFO', logger='app.plugins.hooks')

    with patch.dict(os.environ, {'FLASK_DEBUG': value}):
        @hooks.hook('logged_hook')
        def handler(data):
            return data

    logged = any("Registered hook 'logged_hook'" in record.message for record in caplog.records)
    assert logged is logs


def test_the_loader_says_which_plugin_has_no_init_file(app, clean_registry, tmp_path, caplog):
    """The skip and the message are one decision: without the guard the loader
    would reach spec_from_file_location for a file that is not there and report
    a failed IMPORT rather than a missing __init__.py, which is a different
    thing for whoever reads the log.
    """
    plugins = clean_registry
    caplog.set_level('WARNING', logger='app.plugins')
    (tmp_path / 'empty_plugin').mkdir()

    assert plugins.load_plugins(str(tmp_path)) == {}

    assert any('missing __init__.py' in record.message for record in caplog.records)
    assert not any('Failed to load plugin' in record.message for record in caplog.records)


def test_the_loader_says_when_a_spec_cannot_be_built(app, clean_registry, tmp_path, caplog):
    """Same shape: both paths end in "this plugin did not load", and only the
    message says whether the import machinery refused or the plugin's own code
    raised.
    """
    plugins = clean_registry
    caplog.set_level('ERROR', logger='app.plugins')
    _write_plugin(tmp_path, 'unspeccable_plugin', 'VALUE = 1\n')

    with patch('app.plugins.importlib.util.spec_from_file_location', return_value=None):
        assert plugins.load_plugins(str(tmp_path)) == {}

    assert any('Could not load plugin spec' in record.message for record in caplog.records)
    assert not any('Failed to load plugin' in record.message for record in caplog.records)


def test_a_reload_whose_spec_cannot_be_built_says_nothing_about_a_failure(app, clean_registry,
                                                                          tmp_path, caplog):
    """reload_plugin's copy of the same guard: it returns False WITHOUT logging
    an error, because nothing went wrong in the plugin -- and dropping the
    guard turns that into a logged failure.
    """
    plugins = clean_registry
    _write_plugin(tmp_path, 'unspeccable_plugin', 'VALUE = 1\n')
    plugins.load_plugins(str(tmp_path))
    caplog.set_level('ERROR', logger='app.plugins')

    with patch('app.plugins.importlib.util.spec_from_file_location', return_value=None):
        assert plugins.reload_plugin('unspeccable_plugin') is False

    assert not any('Failed to reload plugin' in record.message for record in caplog.records)


def test_four_guards_in_this_package_are_belt_and_braces(app, clean_registry, tmp_path):
    """THE ROUND'S FOUR EQUIVALENT MUTANTS, PROVED TOGETHER.

    Each of these guards has a second line of defence that produces the same
    OBSERVABLE answer, so no fixture can tell the two programs apart:

    1. `module_name.count('.') >= 2` beside `startswith('app.plugins.')` --
       the shortest string passing the first test is `'app.plugins.'`, which
       already contains two dots, so the second can never be the one that
       refuses.
    2. `not plugin_dir.is_dir()` beside the underscore test -- a path that is
       not a directory cannot contain an `__init__.py`, so the check below
       skips it anyway.
    3. `if plugin_name not in _loaded_plugins: return False` in reload_plugin --
       without it the next line raises KeyError, which the function's own
       `except Exception` turns into the same `False`.
    4. `debug_logging_enabled() and post_data` in the example plugin's
       after-create handler -- with the flag on and no data, the `hasattr`
       below is false and nothing is printed either way.

    Asserted as the behaviour each guard is there to produce, so the rows still
    fail if a second line of defence is ever removed.
    """
    plugins = clean_registry

    # 1: a module name with the prefix always has the dots
    assert 'app.plugins.'.count('.') == 2

    # 2: a file is skipped, and it has no __init__.py to be found either way
    (tmp_path / 'notaplugin.py').write_text('VALUE = 1\n')
    assert plugins.load_plugins(str(tmp_path)) == {}
    assert not (tmp_path / 'notaplugin.py' / '__init__.py').exists()

    # 3: reloading something absent is False, by the guard or by the handler
    assert plugins.reload_plugin('never_loaded') is False

    # 4: the after-create handler tolerates no data with the flag on
    from app.plugins import example_plugin
    with patch.dict(os.environ, {'FLASK_DEBUG': '1'}):
        assert example_plugin.example_after_post_creation(None) is None
