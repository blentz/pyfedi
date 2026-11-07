"""The media cap: local copies of a remote post's images go once the post is older than
`Site.remote_image_cache_days`, and pages then load the image from `source_url` (owner rulings 2026-11-07)."""
import os
import time
from datetime import timedelta
from unittest.mock import patch

import pytest
from flask import g

from app import cli, db, media_cap
from app.models import File, Site, jpeg_rendition_path, post_file, utcnow
from tests.factories import make_community, make_instance, make_post, make_user
from tests.test_admin_routes_entry import _language, _seed, _settings_admin, _valid_misc_payload, csrf, login

POSTS = 'app/static/media/posts/mediacap'


@pytest.fixture
def world(app, api_baseline):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.remote_image_cache_days = 35
    db.session.commit()
    g.site = site
    made = []

    def on_disk(name, directory=POSTS):
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, 'wb') as handle:
            handle.write(b'not really an image')
        made.append(path)
        return path

    instance = make_instance('remote.example')
    yield {'on_disk': on_disk, 'community': make_community('capped'), 'author': make_user(instance, 'capauthor'),
           'local': make_user(None, 'caplocal', local=True), 'serial': iter(range(1, 1000))}
    for path in made:
        if os.path.exists(path):
            os.unlink(path)


def a_copied_file(world, stem, **columns):
    """A File our server made from a peer's image: an unmarked row unless the caller marks it."""
    fields = dict(file_path=world['on_disk'](f'{stem}.webp'), thumbnail_path=world['on_disk'](f'{stem}_thumbnail.webp'),
                  source_url=f'https://remote.example/{stem}.jpg', thumbnail_width=170, thumbnail_height=100)
    fields.update(columns)
    file = File(**fields)
    db.session.add(file)
    db.session.flush()
    return file


def a_post(world, file=None, days=40, author=None):
    post = make_post(world['community'], author or world['author'],
                     f"https://remote.example/p/{next(world['serial'])}")
    post.created_at = utcnow() - timedelta(days=days)
    if file is not None:
        post.image_id = file.id
    db.session.commit()
    return post


def test_an_old_remote_posts_copies_go_and_pages_fall_back_to_the_source(world):
    file = a_copied_file(world, 'capoldremotefile')
    full, thumbnail = file.file_path, file.thumbnail_path
    a_post(world, file)

    assert media_cap.evict_old_post_media()['evicted'] == 1

    db.session.expire_all()
    file = db.session.get(File, file.id)
    assert not os.path.exists(full) and not os.path.exists(thumbnail)
    assert file.file_path is None and file.thumbnail_path is None
    assert file.thumbnail_width is None and file.thumbnail_height is None
    source = 'https://remote.example/capoldremotefile.jpg'
    assert (file.view_url(), file.medium_url(), file.thumbnail_url()) == (source, source, source)


def test_a_marked_file_goes_the_same_way(world):
    file = a_copied_file(world, 'capmarkedfile000', file_written_by_us=True, thumbnail_written_by_us=True)
    full = file.file_path
    a_post(world, file)
    assert media_cap.evict_old_post_media()['evicted'] == 1
    assert not os.path.exists(full)


def test_a_gallery_picture_of_an_old_post_goes(world):
    file = a_copied_file(world, 'capgalleryfile00')
    full = file.file_path
    post = a_post(world)
    db.session.execute(post_file.insert().values(post_id=post.id, file_id=file.id))
    db.session.commit()
    assert media_cap.evict_old_post_media()['evicted'] == 1
    assert not os.path.exists(full)


def test_the_age_is_the_posts_arrival_not_its_date(world):
    file = a_copied_file(world, 'capnewarrival000')
    post = a_post(world, file, days=1)
    post.posted_at = utcnow() - timedelta(days=4000)   # an old note that only just reached us
    db.session.commit()
    assert media_cap.evict_old_post_media()['evicted'] == 0
    assert os.path.exists(file.file_path)


def test_a_post_exactly_inside_the_cap_keeps_its_copies(world):
    file = a_copied_file(world, 'capinsidecap0000')
    a_post(world, file, days=34)
    assert media_cap.evict_old_post_media()['evicted'] == 0


def test_a_file_a_recent_post_also_uses_is_kept(world):
    file = a_copied_file(world, 'capsharedrecent0')
    a_post(world, file)
    a_post(world, file, days=1)
    assert media_cap.evict_old_post_media()['evicted'] == 0
    assert os.path.exists(file.file_path)


def test_a_local_authors_post_keeps_its_image(world):
    file = a_copied_file(world, 'caplocalauthor00')
    a_post(world, file, author=world['local'])
    assert media_cap.evict_old_post_media()['evicted'] == 0
    assert os.path.exists(file.file_path)


def test_a_file_with_no_outside_source_is_kept(world):
    for stem, source in (('capnosource00000', None), ('capemptysource00', ''),
                         ('capunsafesource0', 'javascript:alert(1)')):
        a_post(world, a_copied_file(world, stem, source_url=source))
    assert media_cap.evict_old_post_media()['evicted'] == 0


def test_a_file_whose_source_is_on_this_server_is_kept(world, app):
    file = a_copied_file(world, 'capownhostsource',
                         source_url=f"{app.config['SERVER_URL']}/static/media/posts/mediacap/capownhostsource.webp")
    a_post(world, file)
    assert media_cap.evict_old_post_media()['evicted'] == 0
    assert os.path.exists(file.file_path)


def test_a_file_a_local_account_owns_is_kept(world):
    file = a_copied_file(world, 'caplocalowner000')
    a_post(world, file)
    world['local'].avatar_id = file.id
    db.session.commit()
    assert media_cap.evict_old_post_media()['evicted'] == 0
    assert os.path.exists(file.file_path)


def test_a_path_outside_media_posts_is_never_unlinked(world):
    """A peer could set file_path until 397a91156: the row names someone's avatar, and the cap must leave it."""
    avatar = world['on_disk']('capvictimavatar0.webp', 'app/static/media/users/mediacap')
    file = a_copied_file(world, 'capoutsideposts0', file_path=avatar)
    a_post(world, file)
    media_cap.evict_old_post_media()
    assert os.path.exists(avatar)
    assert db.session.get(File, file.id).file_path == avatar


def test_a_path_climbing_out_of_media_posts_is_never_unlinked(world):
    avatar = world['on_disk']('capclimbingout00.webp', 'app/static/media/users/mediacap')
    climbing = os.path.join(POSTS, '..', 'users', 'mediacap', 'capclimbingout00.webp')
    os.makedirs(POSTS, exist_ok=True)
    a_post(world, a_copied_file(world, 'capclimbrow00000', file_path=climbing))
    media_cap.evict_old_post_media()
    assert os.path.exists(avatar)


def test_a_url_in_file_path_is_left_alone(world):
    file = a_copied_file(world, 'caphttpfilepath0', file_path='https://cdn.example/x.webp')
    a_post(world, file)
    media_cap.evict_old_post_media()
    assert db.session.get(File, file.id).file_path == 'https://cdn.example/x.webp'


def test_a_file_another_record_names_stays_until_the_last_record_goes(world):
    first = a_copied_file(world, 'capshareddisk000')
    full = first.file_path
    second = File(file_path=full, source_url='https://remote.example/other.jpg')
    db.session.add(second)
    db.session.flush()
    keeper = File(file_path=full, source_url='https://remote.example/kept.jpg')
    db.session.add(keeper)
    db.session.flush()
    a_post(world, first)
    a_post(world, second)

    media_cap.evict_old_post_media()
    assert os.path.exists(full)                                  # the keeper's post is not old
    assert db.session.get(File, first.id).file_path is None      # both evicted rows now load from their source


def test_rows_sharing_a_path_with_no_other_keeper_remove_it(world):
    first = a_copied_file(world, 'capsharedboth000')
    full = first.file_path
    second = File(file_path=full, source_url='https://remote.example/other2.jpg')
    db.session.add(second)
    db.session.flush()
    a_post(world, first)
    a_post(world, second)
    assert media_cap.evict_old_post_media()['evicted'] == 2
    assert not os.path.exists(full)


def test_a_file_already_gone_from_disk_is_still_cleared(world):
    file = a_copied_file(world, 'capalreadygone00')
    os.unlink(file.file_path)
    a_post(world, file)
    assert media_cap.evict_old_post_media()['evicted'] == 1
    assert db.session.get(File, file.id).file_path is None


@pytest.mark.parametrize('days', [0, None])
def test_no_cap_set_evicts_nothing(world, days):
    db.session.get(Site, 1).remote_image_cache_days = days
    db.session.commit()
    file = a_copied_file(world, 'capnocapset00000')
    a_post(world, file)
    assert media_cap.evict_old_post_media() == {'evicted': 0, 'failed': 0}
    assert os.path.exists(file.file_path)


def test_one_file_failing_does_not_stop_the_rest(world):
    bad = a_copied_file(world, 'capfailingfile00')
    good = a_copied_file(world, 'capworkingfile00')
    good_path = good.file_path
    a_post(world, bad)
    a_post(world, good)
    real = File.evict_local_copies

    def fails_once(self, cache_urls):
        if self.id == bad.id:
            raise OSError('disk says no')
        return real(self, cache_urls)

    with patch.object(File, 'evict_local_copies', fails_once):
        assert media_cap.evict_old_post_media() == {'evicted': 1, 'failed': 1}
    assert not os.path.exists(good_path)


def test_a_run_stops_at_its_time_budget(world, monkeypatch):
    file = a_copied_file(world, 'capoutofbudget00')
    a_post(world, file)
    monkeypatch.setattr(media_cap, 'MEDIA_CAP_SECONDS', 0)
    assert media_cap.evict_old_post_media()['evicted'] == 0
    assert os.path.exists(file.file_path)


def test_evicted_urls_are_flushed_from_the_cdn(world):
    a_post(world, a_copied_file(world, 'capcdnflush00000'))
    with patch('app.media_cap.flush_cdn_cache') as flush:
        media_cap.evict_old_post_media()
    assert len(flush.call_args.args[0]) == 2


def test_the_jpeg_rendition_goes_too(world):
    file = a_copied_file(world, 'capjpegrendition')
    a_post(world, file)
    jpeg = world['on_disk'](os.path.basename(jpeg_rendition_path(file.id)), os.path.dirname(jpeg_rendition_path(file.id)))
    media_cap.evict_old_post_media()
    assert not os.path.exists(jpeg)


def _aged(path, days=2):
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))
    return path


def test_the_sweep_removes_a_file_no_record_names(world):
    orphan = _aged(world['on_disk']('caporphanfile000.webp'))
    assert media_cap.remove_post_orphans() >= 1
    assert not os.path.exists(orphan)


def test_the_sweep_keeps_a_file_a_record_names(world):
    file = a_copied_file(world, 'capnamedbyrow000')
    db.session.commit()
    _aged(file.file_path)
    _aged(file.thumbnail_path)
    media_cap.remove_post_orphans()
    assert os.path.exists(file.file_path) and os.path.exists(file.thumbnail_path)


def test_the_sweep_keeps_a_file_a_record_names_by_url(world, app):
    kept = _aged(world['on_disk']('capnamedbyurl000.webp'))
    db.session.add(File(source_url=f"{app.config['SERVER_URL']}/static/media/posts/mediacap/capnamedbyurl000.webp"))
    db.session.commit()
    media_cap.remove_post_orphans()
    assert os.path.exists(kept)


def test_the_sweep_keeps_a_file_too_new_to_judge(world):
    """Written, its record not committed yet."""
    fresh = world['on_disk']('capfreshorphan00.webp')
    media_cap.remove_post_orphans()
    assert os.path.exists(fresh)


def test_the_sweep_keeps_a_file_with_too_short_a_name(world):
    short = _aged(world['on_disk']('short.webp'))
    media_cap.remove_post_orphans()
    assert os.path.exists(short)


def test_the_sweep_leaves_a_local_posts_video_alone(world):
    video = _aged(world['on_disk']('capuploadedvideo.mp4'))
    media_cap.remove_post_orphans()
    assert os.path.exists(video)


def test_the_sweep_stops_at_its_time_budget(world, monkeypatch):
    orphan = _aged(world['on_disk']('capsweepbudget00.webp'))
    monkeypatch.setattr(media_cap, 'ORPHAN_SWEEP_SECONDS', 0)
    assert media_cap.remove_post_orphans() == 0
    assert os.path.exists(orphan)


def test_the_sweep_survives_a_file_vanishing_under_it(world):
    _aged(world['on_disk']('capvanishing0000.webp'))
    with patch('app.media_cap.os.unlink', side_effect=FileNotFoundError):
        assert media_cap.remove_post_orphans() == 0


def test_the_sweep_survives_a_file_vanishing_before_its_age_is_read(world):
    _aged(world['on_disk']('capvanishstat000.webp'))
    with patch('app.media_cap.os.path.getmtime', side_effect=FileNotFoundError):
        assert media_cap.remove_post_orphans() == 0


def test_the_job_runs_expiry_then_the_cap_and_is_logged(app, api_baseline):
    g.site = db.session.get(Site, 1)
    cli.register(app)
    with patch.object(cli, 'expire_relayed_posts') as expire, patch.object(cli, 'evict_old_post_media') as evict, \
            patch.object(cli, 'log_cron_task_to_db') as log:
        result = app.test_cli_runner().invoke(args=['expire_old_media'])
    assert result.exit_code == 0, result.output
    expire.assert_called_once_with()
    evict.assert_called_once_with()
    log.assert_called_once_with('expire_old_media')


def test_remove_orphan_files_sweeps_the_post_media(app, api_baseline):
    g.site = db.session.get(Site, 1)
    cli.register(app)
    with patch.object(cli, 'remove_post_orphans') as sweep, patch('app.cli.os.walk', return_value=[]):
        result = app.test_cli_runner().invoke(args=['remove_orphan_files'])
    assert result.exit_code == 0, result.output
    sweep.assert_called_once_with()


def _misc_client(app):
    instance, _ordinary = _seed()
    admin = _settings_admin(instance)
    client = app.test_client()
    login(client, admin)
    client.misc_payload = _valid_misc_payload(_language())
    client.misc_payload.update({'submit': 'Save', 'csrf_token': csrf(app, client)})
    return client


def _post_misc(app, client, value):
    data = dict(client.misc_payload, remote_image_cache_days=value)
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.post('/admin/misc', data=data)
    return render


def test_admin_misc_shows_and_saves_the_cap(app, db_session):
    client = _misc_client(app)
    _post_misc(app, client, '60')
    db.session.expire_all()
    assert db.session.get(Site, 1).remote_image_cache_days == 60
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/misc')
    assert render.call_args.kwargs['form'].remote_image_cache_days.data == 60


def test_admin_misc_left_blank_keeps_the_cap(app, db_session):
    client = _misc_client(app)
    _post_misc(app, client, '60')
    _post_misc(app, client, '')
    db.session.expire_all()
    assert db.session.get(Site, 1).remote_image_cache_days == 60


@pytest.mark.parametrize('value', ['-1', '36501'])
def test_admin_misc_refuses_a_cap_out_of_range(app, db_session, value):
    render = _post_misc(app, _misc_client(app), value)
    assert 'remote_image_cache_days' in render.call_args.kwargs['form'].errors
