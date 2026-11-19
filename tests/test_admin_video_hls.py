"""/admin/video_hls (owner rulings H4, HL1-HL3, 2026-11-12): the HLS toggle, off by default, with its storage hint;
a backfill button, refused while HLS is off; a delete button; and what HLS takes now."""
import pytest

from app import db
from app.models import HLS_SETTING
from app.utils import get_setting, set_setting
from tests.conftest import forget_login
from tests.factories import make_community, make_post, make_user
from tests.test_admin_domain_branding import REPO
from tests.test_admin_site_profile import csrf, login

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def queued(monkeypatch):
    found = []
    monkeypatch.setattr('app.admin.hls_routes.task_selector', lambda key, **kw: found.append(key))
    return found


def press(admin, action, **form):
    client, token = admin
    forget_login()
    return client.post('/admin/video_hls', data={'csrf_token': token, 'action': action, **form})


def test_hls_is_off_and_the_page_says_what_it_costs(admin_domain_branding_admin, admin_domain_branding_domains):
    assert not get_setting(HLS_SETTING, False)
    body = admin_domain_branding_admin[0].get('/admin/video_hls').get_data(as_text=True)
    assert 'id="hls_enabled"' in body and 'checked' not in body.split('id="hls_enabled"')[1].split('>')[0]
    assert 'roughly doubles' in body
    assert '0 videos have HLS copies' in body


def test_saving_turns_it_on_and_off(admin_domain_branding_admin, admin_domain_branding_domains):
    response = press(admin_domain_branding_admin, 'save', enabled='on')
    assert response.status_code == 302 and response.headers['Location'].endswith('/admin/video_hls')
    assert get_setting(HLS_SETTING, False) is True
    press(admin_domain_branding_admin, 'save')
    assert get_setting(HLS_SETTING, False) is False


def test_the_backfill_is_refused_while_hls_is_off(admin_domain_branding_admin, admin_domain_branding_domains, queued):
    press(admin_domain_branding_admin, 'backfill')
    assert queued == []
    set_setting(HLS_SETTING, True)
    press(admin_domain_branding_admin, 'backfill')
    assert queued == ['backfill_hls']


def test_the_delete_button_queues_the_delete(admin_domain_branding_admin, admin_domain_branding_domains, queued):
    press(admin_domain_branding_admin, 'delete')
    assert queued == ['delete_all_hls']


def test_an_unknown_action_is_refused(admin_domain_branding_admin, admin_domain_branding_domains, queued):
    assert press(admin_domain_branding_admin, 'explode').status_code == 400 and queued == []


def test_the_page_counts_the_copies_and_their_size(admin_domain_branding_admin, admin_domain_branding_domains):
    community = make_community('hlscount')
    author = make_user(admin_domain_branding_domains['instance'], 'hlsauthor', local=True)
    for index, variants in enumerate(([{'bytes': 1000}, {'bytes': 2000}], [{'bytes': 500}], 'not a list')):
        post = make_post(community, author, ap_id=f'https://x.example/post/hls{index}', title=f'v{index}')
        post.extensions = {'media': {'hls': variants}}
    db.session.commit()
    body = admin_domain_branding_admin[0].get('/admin/video_hls').get_data(as_text=True)
    assert '2 videos have HLS copies, taking 3.5 kB.' in body


def test_only_instance_settings_holders_see_it(app, admin_domain_branding_domains):
    ordinary = make_user(admin_domain_branding_domains['instance'], 'ordinary', local=True)
    ordinary.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, ordinary)
    token = csrf(app, client)
    assert client.get('/admin/video_hls').status_code == 302
    forget_login()
    client.post('/admin/video_hls', data={'csrf_token': token, 'action': 'save', 'enabled': 'on'})
    assert not get_setting(HLS_SETTING, False)


def test_the_misc_page_links_to_it():
    assert "url_for('admin.admin_video_hls')" in (REPO / 'app' / 'templates' / 'admin' / 'misc.html').read_text()
