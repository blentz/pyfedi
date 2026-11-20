"""Custom themes appear in their domain's theme pickers (custom themes plan, Task 3; spec §3.4, CT3, CT9).

Every picker offers the shipped themes, then the custom themes of the domain the choice is for, retired ones left
out unless one is the field's saved value. A choice outside that list is refused, as any other unoffered choice is."""
from unittest.mock import patch

import pytest

from app import db
from app.community.util import community_theme_list
from app.custom_themes import custom_identifier
from app.models import CommunityMember, CustomTheme, DomainBranding, User
from app.shared.community import _refuse_community_settings_the_form_would_not_offer
from app.storefront import clear_domain_cache
from app.utils import theme_choices, theme_list
from tests.factories import grant_permission
from tests.test_community_lifecycle import able_to_create
from tests.test_user_settings import csrf, settings_payload
from tests.two_domains import FIRST

pytestmark = pytest.mark.usefixtures('site')


def make_theme(domain, name, retired=False):
    theme = CustomTheme(site_domain_id=domain.id, name=name, base_theme='forest', retired=retired)
    db.session.add(theme)
    db.session.commit()
    return theme


@pytest.fixture
def themes(two_domains):
    default, onion = two_domains.domains
    return dict(own=make_theme(default, 'Daylight'), old=make_theme(default, 'Old', retired=True),
                onion=make_theme(onion, 'Night'))


def labelled(choices):
    """(value, label) of each choice: a plain pair, or a form's SelectChoice."""
    return [(choice.value, choice.label) if hasattr(choice, 'value') else tuple(choice) for choice in choices]


def values(choices):
    return [value for value, _label in labelled(choices)]


def as_user(app, user, host=FIRST):
    client = app.test_client()
    with client.session_transaction(base_url=f'https://{host}') as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


def test_theme_choices(app, two_domains, themes):
    default, onion = two_domains.domains
    own, old, night = (custom_identifier(themes[key].id) for key in ('own', 'old', 'onion'))
    with app.test_request_context():
        choices = theme_choices(default.id)
        assert choices[:len(theme_list())] == theme_list()
        assert choices[len(theme_list()):] == [(own, 'Daylight (custom)')]
        assert values(theme_choices(onion.id)) == values(theme_list()) + [night]
        assert theme_choices(default.id, old)[-1] == (old, 'Old (retired)')
        assert values(theme_choices(default.id, night)) == values(theme_list()) + [own]   # another domain's: no
        assert theme_choices(None) == theme_list()


def test_community_theme_list(app, two_domains, themes):
    default, _onion = two_domains.domains
    with app.test_request_context():
        choices = community_theme_list(default.id)
        assert choices[0] == ('disabled', 'Disabled')
        assert values(choices)[1:] == values(theme_choices(default.id))
        assert community_theme_list()[1:] == theme_list()


@pytest.fixture
def settings_user(app, two_domains):
    return two_domains.local_user('picker', two_domains.domains[0])


def save_theme(app, client, theme):
    token = csrf(app, client)
    with patch('app.user.routes.render_template', return_value='rendered') as render:
        client.post('/user/settings', data=settings_payload(token, theme=theme), base_url=f'https://{FIRST}')
    return render


def test_the_user_settings_picker_offers_this_domains_themes(app, two_domains, themes, settings_user):
    client = as_user(app, settings_user)
    with patch('app.user.routes.render_template', return_value='rendered') as render:
        assert client.get('/user/settings', base_url=f'https://{FIRST}').status_code == 200
    offered = values(render.call_args.kwargs['form'].theme.choices)
    assert custom_identifier(themes['own'].id) in offered
    assert custom_identifier(themes['onion'].id) not in offered
    assert custom_identifier(themes['old'].id) not in offered


@pytest.mark.parametrize('key, saved', [('own', True), ('onion', False), ('old', False)])
def test_the_user_settings_form_saves_only_an_offered_theme(app, two_domains, themes, settings_user, key, saved):
    client = as_user(app, settings_user)
    identifier = custom_identifier(themes[key].id)
    save_theme(app, client, identifier)
    stored = db.session.get(User, settings_user.id).theme
    assert (stored == identifier) is saved


def test_a_retired_saved_theme_survives_saving_the_form_unchanged(app, two_domains, themes, settings_user):
    retired = custom_identifier(themes['old'].id)
    settings_user.theme = retired
    db.session.commit()
    client = as_user(app, settings_user)
    with patch('app.user.routes.render_template', return_value='rendered') as render:
        client.get('/user/settings', base_url=f'https://{FIRST}')
    assert (retired, 'Old (retired)') in labelled(render.call_args.kwargs['form'].theme.choices)
    save_theme(app, client, retired)
    assert db.session.get(User, settings_user.id).theme == retired


def test_the_community_api_refuses_another_domains_theme(app, two_domains, themes):
    community = two_domains.local_community('pickers', two_domains.domains[0])
    night = two_domains.local_community('nightpickers', two_domains.domains[1])
    with app.test_request_context():   # the community's home domain decides, whatever host the request is on
        _refuse_community_settings_the_form_would_not_offer({'theme': custom_identifier(themes['onion'].id)}, night)
        with pytest.raises(Exception, match='theme is not one of the allowed values'):
            _refuse_community_settings_the_form_would_not_offer({'theme': custom_identifier(themes['own'].id)}, night)
        _refuse_community_settings_the_form_would_not_offer({'theme': custom_identifier(themes['own'].id)}, community)
        for refused in (custom_identifier(themes['onion'].id), custom_identifier(themes['old'].id), 'custom:999999',
                        'nope'):
            with pytest.raises(Exception, match='theme is not one of the allowed values'):
                _refuse_community_settings_the_form_would_not_offer({'theme': refused}, community)
        community.theme = custom_identifier(themes['old'].id)
        _refuse_community_settings_the_form_would_not_offer({'theme': community.theme}, community)


def test_the_community_edit_form_offers_its_home_domains_themes(app, two_domains, themes):
    default, _onion = two_domains.domains
    community = two_domains.local_community('dayart', default)
    owner = two_domains.local_user('dayowner', default)
    community.user_id = owner.id
    db.session.commit()
    db.session.add(CommunityMember(user_id=owner.id, community_id=community.id, is_owner=True, is_moderator=True))
    db.session.commit()
    client = as_user(app, owner)
    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get(f'/community/{community.id}/edit', base_url=f'https://{FIRST}')
    offered = values(render.call_args.kwargs['form'].theme.choices)
    assert custom_identifier(themes['own'].id) in offered
    assert custom_identifier(themes['onion'].id) not in offered


def test_the_admin_pickers_offer_their_domains_themes(app, two_domains, themes):
    default, onion = two_domains.domains
    admin = two_domains.local_user('themeadmin', default)
    grant_permission(admin, 'edit domain branding')
    grant_permission(admin, 'change instance settings')
    db.session.add(DomainBranding(site_domain_id=onion.id))
    db.session.commit()
    clear_domain_cache()
    client = as_user(app, admin)
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get(f'/admin/domains/{onion.id}/branding', base_url=f'https://{FIRST}')
    offered = values(render.call_args.kwargs['form'].default_theme.choices)
    assert offered[0] == ''
    assert custom_identifier(themes['onion'].id) in offered and custom_identifier(themes['own'].id) not in offered
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/misc', base_url=f'https://{FIRST}')
    offered = values(render.call_args.kwargs['form'].default_theme.choices)
    assert custom_identifier(themes['own'].id) in offered and custom_identifier(themes['onion'].id) not in offered


def test_the_new_community_form_offers_the_themes_of_the_domain_it_is_on(app, two_domains, themes):
    """The community is made where the form is (owner 2026-11-19), so it offers that domain's themes: an onion account
    on the first domain sees the first domain's, not its own home's."""
    creator = two_domains.local_user('creator', two_domains.domains[1])
    able_to_create(creator)
    db.session.commit()
    client = as_user(app, creator)
    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get('/community/add_local', base_url=f'https://{FIRST}')
    offered = values(render.call_args.kwargs['form'].theme.choices)
    assert custom_identifier(themes['own'].id) in offered
    assert custom_identifier(themes['onion'].id) not in offered
