"""The admin Themes page: list, copy, rename, retire, delete (custom themes plan, Task 5; spec §3.2, §3.5, §3.6).

Theme holders of a domain are `edit domain branding` holders on it, or for the first domain `change instance
settings` holders (CT4). Every route checks that, and a theme is reached only through the domain that owns it."""
import hashlib
import os

import pytest
from flask import g, session as flask_session
from flask_wtf.csrf import generate_csrf
from markupsafe import escape

from app import db
from app.custom_themes import (THEMES_DIR, copy_theme, custom_identifier, rewrite_relative_urls, shipped_css,
                               shipped_name, shipped_themes, storefront, theme_version)
from app.models import CustomTheme, DomainBranding, ModLog, Role, RolePermission, Site, User
from app.storefront import clear_domain_cache
from tests.factories import grant_permission, grant_role, limit_role
from tests.conftest import forget_login
from tests.test_branding_pages import PROXY, page, visit
from tests.two_domains import FIRST

pytestmark = pytest.mark.usefixtures('site')
BASE = f'https://{FIRST}'


def client_for(app, user):
    client = app.test_client()
    with client.session_transaction(base_url=BASE) as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


def csrf(app, client):
    """A CSRF token for `client`'s session. The test app context outlives each request, so `g` still holds the token
    made for the last caller: it is dropped first."""
    g.pop('csrf_token', None)
    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    g.pop('csrf_token', None)
    with client.session_transaction(base_url=BASE) as session:
        session['csrf_token'] = raw
    return token


class Actor:
    """A logged-in client that posts with a CSRF token. Each request forgets the last caller's login, which
    Flask-Login keeps in the shared `g`."""
    def __init__(self, app, user):
        self.app, self.user, self.client = app, user, client_for(app, user)

    def get(self, path):
        forget_login()
        return self.client.get(path, base_url=BASE)

    def post(self, path, **data):
        token = csrf(self.app, self.client)
        forget_login()
        return self.client.post(path, data={'csrf_token': token, **data}, base_url=BASE)


def themes_url(domain, rest=''):
    return f'/admin/domains/{domain.id}/themes{rest}'


# --- Who may reach what (Review Focus 1) ---

def test_a_holder_sees_the_themes_link_only_for_their_domain(custom_themes_admin_world):
    page = custom_themes_admin_world['holder'].get('/admin/domains').get_data(as_text=True)
    assert themes_url(custom_themes_admin_world['onion']) in page
    assert themes_url(custom_themes_admin_world['default']) not in page


def test_the_site_admin_sees_every_domains_themes_link(custom_themes_admin_world):
    page = custom_themes_admin_world['admin'].get('/admin/domains').get_data(as_text=True)
    assert themes_url(custom_themes_admin_world['onion']) in page and themes_url(custom_themes_admin_world['default']) in page


def test_the_list_shows_shipped_themes_and_the_domains_own(custom_themes_admin_world):
    response = custom_themes_admin_world['holder'].get(themes_url(custom_themes_admin_world['onion']))
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'Night' in page and 'Daylight' not in page
    assert 'Forest' in page and 'PieFed' in page


def test_a_holder_cannot_reach_another_domains_themes(custom_themes_admin_world):
    holder, default, day = custom_themes_admin_world['holder'], custom_themes_admin_world['default'], custom_themes_admin_world['day']
    before = (day.name, day.retired, day.css)
    assert holder.get(themes_url(default)).status_code == 403
    assert holder.get(themes_url(default, '/shipped/forest')).status_code == 403
    assert holder.post(themes_url(default, '/copy'), source='forest').status_code == 403
    for action in ('rename', 'retire', 'unretire', 'delete'):
        assert holder.post(themes_url(default, f'/{day.id}/{action}'), name='Taken').status_code == 403
        # through the domain they do hold: the theme is not that domain's, so it is not found
        assert holder.post(themes_url(custom_themes_admin_world['onion'], f'/{day.id}/{action}'), name='Taken').status_code == 404
    assert holder.post(themes_url(custom_themes_admin_world['onion'], '/copy'), source=custom_identifier(day.id)).status_code == 400
    db.session.expire_all()
    still = db.session.get(CustomTheme, day.id)
    assert (still.name, still.retired, still.css) == before
    assert db.session.query(CustomTheme).count() == 2


def test_the_first_domains_themes_need_change_instance_settings(app, custom_themes_admin_world):
    branding_only = custom_themes_admin_world['two_domains'].local_user('brandingonly', custom_themes_admin_world['default'])
    grant_permission(branding_only, 'edit domain branding')
    actor = Actor(app, branding_only)
    assert actor.get(themes_url(custom_themes_admin_world['default'])).status_code == 403
    assert actor.get(themes_url(custom_themes_admin_world['onion'])).status_code == 200
    assert custom_themes_admin_world['admin'].get(themes_url(custom_themes_admin_world['default'])).status_code == 200


def test_an_unknown_domain_or_theme_is_not_found(custom_themes_admin_world):
    admin = custom_themes_admin_world['admin']
    assert admin.get('/admin/domains/999999/themes').status_code == 404
    assert admin.post(themes_url(custom_themes_admin_world['onion'], '/999999/retire')).status_code == 404
    assert admin.get(themes_url(custom_themes_admin_world['onion'], '/shipped/nope')).status_code == 404
    assert admin.get(themes_url(custom_themes_admin_world['onion'], '/shipped/hacker_bbs_shared')).status_code == 404


def test_a_post_without_a_csrf_token_is_refused(custom_themes_admin_world):
    forget_login()
    response = custom_themes_admin_world['holder'].client.post(themes_url(custom_themes_admin_world['onion'], '/copy'), data={'source': 'forest'},
                                           base_url=BASE)
    assert response.status_code == 400


# --- Viewing and copying ---

def test_viewing_a_shipped_themes_css(custom_themes_admin_world):
    page = custom_themes_admin_world['holder'].get(themes_url(custom_themes_admin_world['onion'], '/shipped/forest')).get_data(as_text=True)
    assert 'Forest' in page
    assert str(escape(shipped_css('forest')))[:300] in page
    page = custom_themes_admin_world['holder'].get(themes_url(custom_themes_admin_world['onion'], '/shipped/piefed')).get_data(as_text=True)
    assert 'standard look' in page


def test_copying_a_shipped_theme(custom_themes_admin_world):
    """Review Focus 5: the copy's relative @import resolves where the shipped theme's did."""
    response = custom_themes_admin_world['holder'].post(themes_url(custom_themes_admin_world['onion'], '/copy'), source='hacker_bbs_amber')
    assert response.status_code == 302
    copy = db.session.query(CustomTheme).filter_by(site_domain_id=custom_themes_admin_world['onion'].id,
                                                   base_theme='hacker_bbs_amber').one()
    assert copy.name == f'Copy of {shipped_name("hacker_bbs_amber")}'
    assert '@import url("/static/themes/hacker_bbs_shared/base.css?v=4")' in copy.css
    assert '../' not in copy.css
    assert copy.retired is False and copy.updated_by == custom_themes_admin_world['holder'].user.id
    log = db.session.query(ModLog).filter_by(action='create_theme').one()
    assert log.site_domain_id == custom_themes_admin_world['onion'].id and log.link_text == copy.name


def test_copying_piefed_gives_an_empty_stylesheet(custom_themes_admin_world):
    custom_themes_admin_world['holder'].post(themes_url(custom_themes_admin_world['onion'], '/copy'), source='piefed')
    copy = db.session.query(CustomTheme).filter_by(base_theme='piefed').one()
    assert copy.css == '' and copy.name == 'Copy of PieFed'


def test_copying_an_own_custom_theme_and_names_stay_unique(custom_themes_admin_world):
    night = custom_themes_admin_world['night']
    for _ in range(2):
        custom_themes_admin_world['holder'].post(themes_url(custom_themes_admin_world['onion'], '/copy'), source=custom_identifier(night.id))
    names = sorted(t.name for t in db.session.query(CustomTheme).filter_by(site_domain_id=custom_themes_admin_world['onion'].id))
    assert names == ['Copy of Night', 'Copy of Night (2)', 'Night']
    copy = db.session.query(CustomTheme).filter_by(name='Copy of Night').one()
    assert copy.css == '.night{}' and copy.base_theme == 'forest'


@pytest.mark.parametrize('source', ['', 'nope', 'custom:999999', '../forest', 'hacker_bbs_shared'])
def test_copying_nothing_copyable_is_a_400(custom_themes_admin_world, source):
    assert custom_themes_admin_world['holder'].post(themes_url(custom_themes_admin_world['onion'], '/copy'), source=source).status_code == 400


@pytest.mark.parametrize('name', shipped_themes())
def test_every_shipped_copy_has_no_relative_reference_left(app, name):
    css = rewrite_relative_urls(shipped_css(name), name)
    text, spans = storefront._css_url_spans(css)
    for start, _end, url in spans:
        quoted = start > 0 and text[start - 1] in '"\''
        before = text[:start - 1].rstrip().lower()
        if quoted and not (before.endswith('url(') or before.endswith('@import')):
            continue
        assert url.startswith(('/', 'data:', '#', 'http:', 'https:')), (name, url)


@pytest.mark.parametrize('css, expected', [
    ('a{background:url(img/x.png)}', 'a{background:url(/static/themes/base/img/x.png)}'),
    ('a{background:url("./x.png")}', 'a{background:url("/static/themes/base/x.png")}'),
    ("@import '../shared/a.css?v=2';", "@import '/static/themes/shared/a.css?v=2';"),
    ('@import url(../shared/a.css#top);', '@import url(/static/themes/shared/a.css#top);'),
    ('a{font-family:"My Font"; content:"x.png"}', 'a{font-family:"My Font"; content:"x.png"}'),
    ('a{background:url(/static/images/x.gif)}', 'a{background:url(/static/images/x.gif)}'),
    ('a{background:url(data:image/png;base64,AAAA)}', 'a{background:url(data:image/png;base64,AAAA)}'),
    ('a{background:url(https://example.org/x.png)}', 'a{background:url(https://example.org/x.png)}'),
    ('a{mask:url(#m)}', 'a{mask:url(#m)}'),
    ('a{background:url("my image.png")}', 'a{background:url("/static/themes/base/my%20image.png")}'),
    ('/* url(x.png) */ a{}', '/* url(x.png) */ a{}'),
    ('a{background:url()}', 'a{background:url()}'),
])
def test_rewrite_relative_urls(css, expected):
    assert rewrite_relative_urls(css, 'base') == expected


def test_copy_theme_refuses_another_domains_theme(custom_themes_admin_world):
    with pytest.raises(ValueError):
        copy_theme(custom_identifier(custom_themes_admin_world['day'].id), custom_themes_admin_world['onion'].id, None)


# --- Rename, retire ---

def test_renaming(custom_themes_admin_world):
    night = custom_themes_admin_world['night']
    custom_themes_admin_world['holder'].post(themes_url(custom_themes_admin_world['onion'], f'/{night.id}/rename'), name='  Midnight  ')
    db.session.expire_all()
    assert db.session.get(CustomTheme, night.id).name == 'Midnight'
    log = db.session.query(ModLog).filter_by(action='rename_theme').one()
    assert log.reason == 'Night' and log.link_text == 'Midnight'


@pytest.mark.parametrize('name', ['', '   ', 'x' * 101, 'Forest', 'forest', 'PieFed', 'NIGHT2'])
def test_a_name_that_cannot_be_used_is_refused(custom_themes_admin_world, name):
    onion = custom_themes_admin_world['onion']
    db.session.add(CustomTheme(site_domain_id=onion.id, name='Night2', base_theme='forest'))
    db.session.commit()
    night = custom_themes_admin_world['night']
    response = custom_themes_admin_world['holder'].post(themes_url(onion, f'/{night.id}/rename'), name=name)
    assert response.status_code == 302
    db.session.expire_all()
    assert db.session.get(CustomTheme, night.id).name == 'Night'


def test_renaming_to_its_own_name_in_another_case_is_allowed(custom_themes_admin_world):
    night = custom_themes_admin_world['night']
    custom_themes_admin_world['holder'].post(themes_url(custom_themes_admin_world['onion'], f'/{night.id}/rename'), name='NIGHT')
    db.session.expire_all()
    assert db.session.get(CustomTheme, night.id).name == 'NIGHT'


def test_retiring_and_offering_again(custom_themes_admin_world):
    night, onion = custom_themes_admin_world['night'], custom_themes_admin_world['onion']
    custom_themes_admin_world['holder'].post(themes_url(onion, f'/{night.id}/retire'))
    db.session.expire_all()
    assert db.session.get(CustomTheme, night.id).retired is True
    assert 'retired' in custom_themes_admin_world['holder'].get(themes_url(onion)).get_data(as_text=True)
    custom_themes_admin_world['holder'].post(themes_url(onion, f'/{night.id}/unretire'))
    db.session.expire_all()
    assert db.session.get(CustomTheme, night.id).retired is False
    assert [log.action for log in db.session.query(ModLog).order_by(ModLog.id)] == ['retire_theme', 'unretire_theme']


def test_theme_actions_are_admin_only_in_the_modlog(custom_themes_admin_world):
    custom_themes_admin_world['holder'].post(themes_url(custom_themes_admin_world['onion'], f'/{custom_themes_admin_world["night"].id}/retire'))
    assert db.session.query(ModLog).filter_by(action='retire_theme').one().public is False


# --- Delete (CT7, Review Focus 6) ---

def use_as_domain_default(world, theme):
    db.session.add(DomainBranding(site_domain_id=world['onion'].id, default_theme=custom_identifier(theme.id)))


def use_by_a_user(world, theme):
    world['two_domains'].local_user('picker', world['onion']).theme = custom_identifier(theme.id)


def use_by_a_community(world, theme):
    world['two_domains'].local_community('themed', world['onion']).theme = custom_identifier(theme.id)


@pytest.mark.parametrize('use, said', [(use_as_domain_default, "domain's default theme"),
                                       (use_by_a_user, "1 person's own theme"),
                                       (use_by_a_community, "1 community's theme")])
def test_a_theme_in_use_is_not_deleted(app, custom_themes_admin_world, use, said):
    night = custom_themes_admin_world['night']
    use(custom_themes_admin_world, night)
    db.session.commit()
    clear_domain_cache()
    response = custom_themes_admin_world['holder'].post(themes_url(custom_themes_admin_world['onion'], f'/{night.id}/delete'))
    assert response.status_code == 302
    assert db.session.get(CustomTheme, night.id) is not None
    page = custom_themes_admin_world['holder'].get(themes_url(custom_themes_admin_world['onion'])).get_data(as_text=True)
    assert said in page.replace('&#39;', "'")


def test_the_site_default_counts_as_a_use_of_a_first_domain_theme(custom_themes_admin_world):
    day = custom_themes_admin_world['day']
    db.session.get(Site, 1).default_theme = custom_identifier(day.id)
    db.session.commit()
    custom_themes_admin_world['admin'].post(themes_url(custom_themes_admin_world['default'], f'/{day.id}/delete'))
    assert db.session.get(CustomTheme, day.id) is not None


def test_an_unused_theme_is_deleted(custom_themes_admin_world):
    night = custom_themes_admin_world['night']
    custom_themes_admin_world['holder'].post(themes_url(custom_themes_admin_world['onion'], f'/{night.id}/delete'))
    db.session.expire_all()
    assert db.session.get(CustomTheme, night.id) is None
    assert db.session.query(ModLog).filter_by(action='delete_theme').one().link_text == 'Night'


def test_a_retired_theme_still_held_by_a_user_is_not_deleted(custom_themes_admin_world):
    night = custom_themes_admin_world['night']
    use_by_a_user(custom_themes_admin_world, night)
    night.retired = True
    db.session.commit()
    custom_themes_admin_world['holder'].post(themes_url(custom_themes_admin_world['onion'], f'/{night.id}/delete'))
    assert db.session.get(CustomTheme, night.id) is not None
    assert db.session.query(User).filter_by(theme=custom_identifier(night.id)).count() == 1


# --- Shipped themes never change (CT1) ---

def fingerprint():
    found = {}
    for root, _dirs, files in os.walk(THEMES_DIR):
        for name in files:
            path = os.path.join(root, name)
            with open(path, 'rb') as file:
                found[path] = (hashlib.sha256(file.read()).hexdigest(), os.path.getmtime(path))
    return found


def test_no_action_writes_under_the_shipped_themes(custom_themes_admin_world):
    before = fingerprint()
    holder, onion = custom_themes_admin_world['holder'], custom_themes_admin_world['onion']
    for name in shipped_themes():
        holder.post(themes_url(onion, '/copy'), source=name)
    for theme in db.session.query(CustomTheme).filter_by(site_domain_id=onion.id).all():
        holder.post(themes_url(onion, f'/{theme.id}/rename'), name=f'Renamed {theme.id}')
        holder.post(themes_url(onion, f'/{theme.id}/retire'))
        holder.post(themes_url(onion, f'/{theme.id}/delete'))
    assert fingerprint() == before


# --- Editing the CSS (Task 6, spec §3.8) ---

def edit_url(domain, theme):
    return themes_url(domain, f'/{theme.id}/edit')


def test_the_edit_page_shows_the_css(custom_themes_admin_world):
    page = custom_themes_admin_world['holder'].get(edit_url(custom_themes_admin_world['onion'], custom_themes_admin_world['night'])).get_data(as_text=True)
    assert '.night{}' in page and 'onion' in page


def test_saving_changes_the_served_stylesheet_and_its_version(app, custom_themes_admin_world):
    night, onion = custom_themes_admin_world['night'], custom_themes_admin_world['onion']
    old_version = theme_version(night)
    response = custom_themes_admin_world['holder'].post(edit_url(onion, night), css='.night { color: black; }\r\n')
    assert response.status_code == 302
    db.session.expire_all()
    saved = db.session.get(CustomTheme, night.id)
    assert saved.css == '.night { color: black; }\n'
    assert theme_version(saved) != old_version and saved.updated_by == custom_themes_admin_world['holder'].user.id
    assert db.session.query(ModLog).filter_by(action='edit_theme').one().site_domain_id == onion.id
    served = visit(app, f'/theme/{night.id}/styles.css?v={theme_version(saved)}', onion=True)
    assert served.get_data(as_text=True) == '.night { color: black; }\n'


def test_css_over_the_cap_is_refused(app, custom_themes_admin_world, monkeypatch):
    monkeypatch.setitem(app.config, 'CUSTOM_THEME_MAX_CSS_BYTES', 10)
    night = custom_themes_admin_world['night']
    response = custom_themes_admin_world['holder'].post(edit_url(custom_themes_admin_world['onion'], night), css='.x{color:red}')
    assert response.status_code == 200
    assert 'over 10' in response.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(CustomTheme, night.id).css == '.night{}'


def test_the_onion_refuses_an_off_site_reference(custom_themes_admin_world):
    night = custom_themes_admin_world['night']
    response = custom_themes_admin_world['holder'].post(edit_url(custom_themes_admin_world['onion'], night),
                                    css='a{background:url(https://elsewhere.example/x.png)}')
    assert response.status_code == 200
    assert 'Off the onion: https://elsewhere.example/x.png' in response.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(CustomTheme, night.id).css == '.night{}'


def test_elsewhere_an_off_site_reference_is_saved_with_a_warning(custom_themes_admin_world):
    day, default, admin = custom_themes_admin_world['day'], custom_themes_admin_world['default'], custom_themes_admin_world['admin']
    admin.post(edit_url(default, day), css='a{background:url(https://elsewhere.example/x.png)}')
    db.session.expire_all()
    assert 'elsewhere.example' in db.session.get(CustomTheme, day.id).css
    with admin.client.session_transaction(base_url=BASE) as session:
        flashes = [message for _category, message in session.get('_flashes', [])]
    assert any('https://elsewhere.example/x.png' in message for message in flashes)


def test_markup_in_css_is_served_as_css_and_never_put_in_a_page(app, custom_themes_admin_world):
    night, onion = custom_themes_admin_world['night'], custom_themes_admin_world['onion']
    custom_themes_admin_world['holder'].post(edit_url(onion, night), css='</style><script>alert(1)</script>')
    db.session.expire_all()
    saved = db.session.get(CustomTheme, night.id)
    served = visit(app, f'/theme/{night.id}/styles.css?v={theme_version(saved)}', onion=True)
    assert served.mimetype == 'text/css'
    db.session.add(DomainBranding(site_domain_id=onion.id, default_theme=custom_identifier(night.id)))
    db.session.commit()
    clear_domain_cache()
    assert '<script>alert(1)</script>' not in page(app, '/', onion=True)


def test_editing_another_domains_theme_is_refused(custom_themes_admin_world):
    day = custom_themes_admin_world['day']
    assert custom_themes_admin_world['holder'].get(edit_url(custom_themes_admin_world['default'], day)).status_code == 403
    assert custom_themes_admin_world['holder'].post(edit_url(custom_themes_admin_world['onion'], day), css='x').status_code == 404
    db.session.expire_all()
    assert db.session.get(CustomTheme, day.id).css == '.day{}'


def test_css_the_form_refuses_is_not_saved(custom_themes_admin_world):
    night = custom_themes_admin_world['night']
    response = custom_themes_admin_world['holder'].post(edit_url(custom_themes_admin_world['onion'], night), css='a{}\x00')
    assert response.status_code == 200
    db.session.expire_all()
    assert db.session.get(CustomTheme, night.id).css == '.night{}'
