"""The onion media cache caps at 5 GiB and copes with its directory absent (onion unlinkability spec, goal 6).

On prod the directory sits on a LUKS volume, which is absent while the volume is locked. The fill must never create it
or a parent (that would put the cache on the unencrypted root disk): it is refused, with one fixed WARNING that names
no URL, path or host. A read is a cache miss and eviction frees nothing.
"""
import logging
from unittest.mock import patch

import pytest
from redis.exceptions import RedisError

import app as app_pkg
import app.onion_media_fill as onion_media_fill
from app import cli
from app.onion_media import DEFAULT_CACHE_BYTES, evict, proxy_url
from app.onion_media_fill import BG_SLOT_KEY, MAX_REQUEUES, fill, fill_task
from app.utils import get_setting
from tests.test_admin_routes_entry import _seed, _settings_admin, login
from tests.test_onion_guard_limits import onion, onion_get  # noqa: F401
from tests.test_onion_media_fill import free_disk, image, remote  # noqa: F401

IMAGE = 'https://media.example/pictrs/image/a.png'
REFUSED = 'onion media cache directory unavailable, fill refused'

@pytest.fixture(autouse=True)
def warned_afresh(monkeypatch):
    monkeypatch.setattr(onion_media_fill, '_absent_logged', False)

@pytest.fixture
def absent(app, tmp_path, monkeypatch):
    """A cache directory under a parent that does not exist, as on a locked volume."""
    directory = tmp_path / 'locked' / 'media_proxy'
    monkeypatch.setitem(app.config, 'MEDIA_PROXY_DIR', str(directory))
    return directory

def refusals(caplog):
    return [record for record in caplog.records if record.name == 'app' and record.levelno == logging.WARNING]

def test_default_cache_cap_is_5_gib(app, db_session):
    assert DEFAULT_CACHE_BYTES == 5368709120
    assert get_setting('onion_media_cache_bytes', DEFAULT_CACHE_BYTES) == 5 * 1024 ** 3
    cli.register(app)   # pyfedi.py registers the commands; the test app has none
    with patch('app.cli.evict') as evicting:
        assert app.test_cli_runner().invoke(args=['evict_onion_media']).exit_code == 0
    evicting.assert_called_once_with(5368709120)

def test_default_cache_cap_is_5_gib_in_the_admin_form(app, db_session):
    client = app.test_client()
    login(client, _settings_admin(_seed()[0]))
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/misc')
    assert render.call_args.kwargs['form'].onion_media_cache_mb.data == 5120

def test_onion_page_renders_when_the_cache_dir_is_absent(app, onion, absent, caplog):    # noqa: F811
    response = onion_get(app.test_client(), proxy_url(IMAGE))
    assert response.status_code == 404          # a refused fill, not a 500
    assert not absent.parent.exists()
    assert [record.getMessage() for record in refusals(caplog)] == [REFUSED]

def test_fill_refused_without_error_when_the_dir_is_absent(remote, absent, caplog):     # noqa: F811
    remote.routes[IMAGE] = image()
    assert fill(IMAGE, max_seconds=5) is None
    assert remote.seen == []                    # nothing was fetched
    assert not absent.parent.exists()           # nor was the directory or a parent created
    assert [record.getMessage() for record in refusals(caplog)] == [REFUSED]
    assert IMAGE not in caplog.text and str(absent) not in caplog.text and 'example' not in caplog.text

def test_the_absent_dir_warning_is_logged_once_per_process(remote, absent, caplog):     # noqa: F811
    for _ in range(3):
        assert fill(IMAGE, max_seconds=5) is None
    assert [record.getMessage() for record in refusals(caplog)] == [REFUSED]

def test_fill_refused_when_the_dir_is_not_writable(remote, tmp_path, monkeypatch, caplog):      # noqa: F811
    remote.routes[IMAGE] = image()
    monkeypatch.setattr('app.onion_media_fill.os.access', lambda path, mode: False)
    assert fill(IMAGE, max_seconds=5) is None
    assert remote.seen == [] and list(tmp_path.iterdir()) == []
    assert [record.getMessage() for record in refusals(caplog)] == [REFUSED]

def test_evict_with_absent_dir_frees_nothing_and_does_not_raise(app, absent):
    assert evict(1) == 0

def test_evict_with_a_file_where_the_dir_should_be_frees_nothing(app, tmp_path, monkeypatch):
    stand_in = tmp_path / 'media_proxy'
    stand_in.write_bytes(b'x')
    monkeypatch.setitem(app.config, 'MEDIA_PROXY_DIR', str(stand_in))
    assert evict(1) == 0


def _echoing(*args, **kwargs):
    raise RedisError(f'down: {IMAGE}')


def _not_cached(remote):                # noqa: F811
    assert fill(IMAGE, max_seconds=5) is None               # a 404 from the remote: refused, remembered dead


def _could_not_store(remote, monkeypatch):      # noqa: F811
    remote.routes[IMAGE] = image()
    free_disk(monkeypatch, 0)                               # under the disk reserve: NoRoom, an OSError
    assert fill(IMAGE, max_seconds=5) is None


def _could_not_count(remote, monkeypatch):      # noqa: F811
    remote.routes[IMAGE] = image()
    monkeypatch.setattr('app.onion_redis_client.incrby', _echoing)
    assert fill(IMAGE, max_seconds=5) is not None


def _could_not_remember(remote, monkeypatch):   # noqa: F811
    monkeypatch.setattr('app.onion_redis_client.set', _echoing)
    assert fill(IMAGE, max_seconds=5) is None


def _slot_stayed_busy(remote, monkeypatch):     # noqa: F811
    app_pkg.onion_redis_client.set(BG_SLOT_KEY, 'another task', ex=630)
    fill_task(IMAGE, MAX_REQUEUES)


@pytest.mark.parametrize('branch', [_could_not_store, _could_not_count, _could_not_remember, _slot_stayed_busy,
                                    _not_cached])
def test_no_fill_log_line_names_the_media_url(remote, monkeypatch, caplog, branch):     # noqa: F811
    """A Celery task is not an onion request, so the log scrub never sees these lines: they must not name the URL."""
    caplog.set_level(logging.INFO, logger='app')
    if branch is _not_cached:
        branch(remote)
    else:
        branch(remote, monkeypatch)
    lines = [record.getMessage() for record in caplog.records if record.name == 'app']
    assert lines                                            # the branch did log
    assert not [line for line in lines if 'media.example' in line or 'a.png' in line or IMAGE in line]
