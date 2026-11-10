"""A person's settings and a link's metadata in Lemmy's API for Jerboa (plan W3; spec 2026-11-10-lemmy-v3-design §2):
Lemmy's settings are kept under PieFed's names where PieFed has them, and a link's metadata answers Lemmy's model."""
import pytest

from app import cache, db
from app.lemmy import settings as lemmy_settings
from app.models import User
from tests import lemmy_app_contract as contract
from tests.factories import make_user


@pytest.fixture
def world(app, api_baseline, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    cache.clear()
    person = make_user(None, 'lemmysettings', local=True)
    person.about = 'Old bio'
    db.session.commit()
    return {'person': person, 'client': app.test_client(),
            'headers': {'Authorization': f'Bearer {person.encode_jwt_token()}'}}


def _save(world, **fields):
    response = world['client'].put('/api/v3/user/save_user_settings', json=fields, headers=world['headers'])
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json() == {'success': True}
    return db.session.get(User, world['person'].id)


def test_lemmys_settings_are_kept_under_piefeds_names(world):
    person = _save(world, display_name='Settled', default_sort_type='TopWeek', default_listing_type='Local',
                   show_bot_accounts=False, show_read_posts=False, send_notifications_to_email=False, theme='dark',
                   bio=None, matrix_user_id=None)
    assert (person.title, person.default_sort, person.default_filter) == ('Settled', 'top', 'local')
    assert (person.ignore_bots, person.hide_read_posts, person.email_unread) == (1, True, False)
    assert person.about == 'Old bio'   # a null is a setting not given, not one cleared


def test_nsfw_is_shown_blurred_or_hidden(world):
    assert _save(world, show_nsfw=True, blur_nsfw=True).hide_nsfw == 2
    assert _save(world, show_nsfw=False).hide_nsfw == 1


@pytest.mark.parametrize('lemmy,kept', [('Hot', 'hot'), ('TopAll', 'top'), ('MostComments', 'active'),
                                        ('Controversial', 'hot'), ('Scaled', 'scaled')])
def test_each_default_sort_is_kept_as_its_nearest(world, lemmy, kept):
    assert _save(world, default_sort_type=lemmy).default_sort == kept


def test_settings_need_a_login(world):
    response = world['client'].put('/api/v3/user/save_user_settings', json={'show_nsfw': True})
    assert response.status_code == 400 and response.get_json() == {'error': 'incorrect_login'}


def test_a_links_metadata(world, monkeypatch):
    monkeypatch.setattr('app.api.alpha.utils.site.opengraph_parse',
                        lambda url: {'og:title': 'A page', 'description': 'About it', 'og:image': 'https://x/i.png'})
    response = world['client'].get('/api/v3/post/site_metadata', query_string={'url': 'https://example.com/page'},
                                   headers=world['headers'])
    body = response.get_json()
    assert response.status_code == 200 and contract.problems(body, 'GetSiteMetadataResponse') == []
    assert body['metadata'] == {'title': 'A page', 'description': 'About it', 'image': 'https://x/i.png',
                                'embed_video_url': None}


def test_a_link_with_no_metadata_says_so(world, monkeypatch):
    monkeypatch.setattr('app.api.alpha.utils.site.opengraph_parse', lambda url: None)
    response = world['client'].get('/api/v3/post/site_metadata', query_string={'url': 'https://example.com/none'})
    assert response.status_code == 400 and response.get_json() == {'error': 'fetch_failed'}


def test_the_sort_kept_is_alphas_name_for_it():
    assert lemmy_settings._sort('TopSixHour') == 'Top' and lemmy_settings._sort('New') == 'New'
