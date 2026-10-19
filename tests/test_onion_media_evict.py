"""Onion media cache eviction and its admin settings (onion media proxy spec 2026-10-19, section 3.5).

The cache directory is a tmp_path put in `MEDIA_PROXY_DIR`. File ages are set with `os.utime`, so nothing sleeps.
"""
import os
import time
from datetime import timedelta
from unittest.mock import patch

import pytest

from redis.exceptions import ConnectionError as RedisConnectionError

from app import cli
from app.onion_media import BYTES_KEY, evict
from app.utils import get_setting, set_setting
from tests.test_admin_routes_entry import _language, _seed, _settings_admin, _valid_misc_payload, csrf, login

MIB = 1048576


@pytest.fixture
def cache(app, tmp_path, monkeypatch, redis_double):
    monkeypatch.setitem(app.config, 'MEDIA_PROXY_DIR', str(tmp_path))
    return tmp_path


def _entry(cache, number, size, age):
    path = cache / (f'{number:064x}' + '.jpg')
    path.write_bytes(b'x' * size)
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def test_evicts_least_recently_used_to_90_percent(cache):
    files = [_entry(cache, n, 100, age=1000 - n * 100) for n in range(1, 11)]   # n=1 is the oldest
    freed = evict(900)                      # 1000 bytes held, over the cap: down to 810 means 2 files go
    assert freed == 200
    assert [p.exists() for p in files] == [False, False] + [True] * 8


def test_under_cap_evicts_nothing(cache):
    files = [_entry(cache, n, 100, age=n) for n in range(1, 4)]
    assert evict(300) == 0                  # exactly at the cap is not over it
    assert all(p.exists() for p in files)


def test_stale_tmp_files_removed_fresh_ones_kept(cache):
    stale, fresh = cache / ('.tmp-' + 'a' * 32), cache / ('.tmp-' + 'b' * 32)
    stale.write_bytes(b'x' * 7)
    fresh.write_bytes(b'x' * 5)
    old = time.time() - 3601
    os.utime(stale, (old, old))
    kept = _entry(cache, 1, 10, age=5)
    assert evict(10 ** 9) == 7
    assert not stale.exists() and fresh.exists() and kept.exists()


def test_evict_touches_only_cache_and_tmp_names(cache):
    stray = cache / 'notes.txt'
    stray.write_bytes(b'x' * 500)
    subdirectory = cache / ('f' * 64 + '.jpg.d')
    subdirectory.mkdir()
    (subdirectory / ('0' * 64 + '.jpg')).write_bytes(b'x' * 500)
    outside = cache.parent / 'outside.bin'
    outside.write_bytes(b'x' * 500)
    link = cache / ('1' * 64 + '.jpg')
    link.symlink_to(outside)
    directory_named_like_a_cache_file = cache / ('2' * 64 + '.png')
    directory_named_like_a_cache_file.mkdir()
    old = time.time() - 7200
    os.utime(link, (old, old), follow_symlinks=False)
    evict(1)
    assert stray.exists() and (subdirectory / ('0' * 64 + '.jpg')).exists()
    assert outside.exists() and link.is_symlink()
    assert directory_named_like_a_cache_file.is_dir()


def test_evict_with_no_cache_directory_frees_nothing(app, tmp_path, monkeypatch):
    monkeypatch.setitem(app.config, 'MEDIA_PROXY_DIR', str(tmp_path / 'absent'))
    assert evict(1) == 0


def test_a_cap_of_zero_evicts_every_file(cache):
    for n in range(1, 4):
        _entry(cache, n, 100, age=1000 - n)
    assert evict(0) == 300
    assert list(cache.iterdir()) == []


def test_a_file_that_vanishes_before_its_stat_is_skipped(cache):
    kept = _entry(cache, 1, 100, age=10)
    real_scandir = os.scandir

    class Gone:
        name = f'{2:064x}.jpg'
        path = str(cache / name)

        def is_file(self, follow_symlinks=True):
            return True

        def stat(self, follow_symlinks=True):
            raise FileNotFoundError

    with patch('app.onion_media.os.scandir', lambda path: [*real_scandir(path), Gone()]):
        assert evict(10 * MIB) == 0
    assert kept.exists()


def test_a_file_that_vanishes_before_its_eviction_still_counts_as_freed(cache):
    _entry(cache, 1, 100, age=100)
    _entry(cache, 2, 100, age=10)
    with patch('app.onion_media.os.unlink', side_effect=FileNotFoundError):
        assert evict(100) == 200   # 200 bytes down to the 90-byte target takes both files; another worker removed them first


def test_evict_sets_the_byte_counter_to_the_real_total(cache, redis_double):
    redis_double.set(BYTES_KEY, 10 ** 9)                # counted fills since the last run, files deleted by hand
    for n in range(1, 11):
        _entry(cache, n, 100, age=1000 - n)
    (cache / ('.tmp-' + 'a' * 32)).write_bytes(b'x' * 7)   # a fill still running is not in the total
    evict(900)
    assert int(redis_double.get(BYTES_KEY)) == 800
    evict(10 ** 9)                                      # under the cap: still the real total
    assert int(redis_double.get(BYTES_KEY)) == 800


def test_evict_without_redis_still_evicts(cache, monkeypatch):
    class Down:
        def set(self, *args, **kwargs):
            raise RedisConnectionError('redis down')

    monkeypatch.setattr('app.redis_client', Down())
    for n in range(1, 4):
        _entry(cache, n, 100, age=1000 - n)
    assert evict(0) == 300


def test_cli_command_uses_the_setting(app, db_session, cache):
    set_setting('onion_media_cache_bytes', 250)
    for n in range(1, 6):
        _entry(cache, n, 100, age=1000 - n)
    cli.register(app)   # pyfedi.py registers the commands; the test app has none
    result = app.test_cli_runner().invoke(args=['evict_onion_media'])
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in cache.iterdir()) == [f'{n:064x}.jpg' for n in (4, 5)]   # 225 bytes is the target


def test_cli_command_default_cap_leaves_a_small_cache(app, db_session, cache):
    kept = _entry(cache, 1, 100, age=1)
    cli.register(app)
    assert app.test_cli_runner().invoke(args=['evict_onion_media']).exit_code == 0
    assert kept.exists()


def test_cli_command_logs_the_run_like_the_other_cron_jobs(app, db_session, cache):
    from app.models import CronJobLog
    cli.register(app)
    assert app.test_cli_runner().invoke(args=['evict_onion_media']).exit_code == 0
    row = CronJobLog.query.filter_by(name='evict_onion_media').one()
    assert row.get_frequency() == timedelta(hours=1)


def _post(app, db_session, **fields):
    instance, _ordinary = _seed()
    admin = _settings_admin(instance)
    client = app.test_client()
    login(client, admin)
    data = _valid_misc_payload(_language())
    data.update({'submit': 'Save', 'csrf_token': csrf(app, client)})
    data.update(fields)
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.post('/admin/misc', data=data)
    return set(render.call_args.kwargs['form'].errors), client


def test_admin_misc_saves_and_shows_the_three_settings(app, db_session):
    errors, client = _post(app, db_session, onion_media_cache_mb='2048', onion_media_image_max_mb='30',
                           onion_media_av_max_mb='700')
    assert errors == set()
    assert get_setting('onion_media_cache_bytes', None) == 2048 * MIB
    assert get_setting('onion_media_image_max_bytes', None) == 30 * MIB
    assert get_setting('onion_media_av_max_bytes', None) == 700 * MIB
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/misc')
    form = render.call_args.kwargs['form']
    assert (form.onion_media_cache_mb.data, form.onion_media_image_max_mb.data, form.onion_media_av_max_mb.data) == \
           (2048, 30, 700)


def test_admin_misc_shows_the_defaults(app, db_session):
    client = app.test_client()
    login(client, _settings_admin(_seed()[0]))
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/misc')
    form = render.call_args.kwargs['form']
    assert (form.onion_media_cache_mb.data, form.onion_media_image_max_mb.data, form.onion_media_av_max_mb.data) == \
           (10240, 20, 500)


@pytest.mark.parametrize('field, value', [('onion_media_cache_mb', '1023'), ('onion_media_cache_mb', '1048577'),
                                          ('onion_media_image_max_mb', '0'), ('onion_media_image_max_mb', '101'),
                                          ('onion_media_av_max_mb', '0'), ('onion_media_av_max_mb', '2049')])
def test_admin_misc_refuses_a_value_out_of_range(app, db_session, field, value):
    assert _post(app, db_session, **{field: value})[0] == {field}
    assert get_setting('onion_media_cache_bytes', None) is None


def test_admin_misc_keeps_the_settings_when_left_out(app, db_session):
    set_setting('onion_media_cache_bytes', 5 * 1024 * MIB)
    assert _post(app, db_session, onion_media_cache_mb='', onion_media_image_max_mb='',
                 onion_media_av_max_mb='')[0] == set()
    assert get_setting('onion_media_cache_bytes', None) == 5 * 1024 * MIB
    assert get_setting('onion_media_image_max_bytes', None) is None
