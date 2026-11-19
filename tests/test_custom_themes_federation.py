"""A community's theme federates only as a shipped theme's name (custom themes plan, Task 4; spec §3.9).

Out: a local community whose theme is custom publishes its base theme, never `custom:<id>`. In: a remote Group's
`theme` is stored only when it is a shipped theme, `piefed` or `disabled`; anything else leaves the stored value as it
was. Without the inbound check a peer could point its community at one of this server's custom themes."""
import pytest

from app import db
from app.activitypub.util import actor_json_to_model, community_actor_data, refresh_community_profile_task
from app.custom_themes import custom_identifier, federated_theme, received_theme
from app.models import CustomTheme
from tests.test_ap_actor_json_group import PEER, _group
from tests.test_ap_refresh_profiles import _group_document, _remote_community
from tests.factories import peer_instance


def make_theme(domain_id, base_theme):
    theme = CustomTheme(site_domain_id=domain_id, name=f'On {base_theme}', base_theme=base_theme)
    db.session.add(theme)
    db.session.commit()
    return theme


@pytest.mark.parametrize('value, expected', [
    ('forest', 'forest'), ('hacker_bbs_amber', 'hacker_bbs_amber'), ('piefed', 'piefed'), ('disabled', 'disabled'),
    ('custom:1', None), ('nope', None), ('../forest', None), ('hacker_bbs_shared', None), ('', None), (None, None),
    (False, None), (0, None), ([], None), (['forest'], None), ({'a': 1}, None),
])
def test_received_theme(app, value, expected):
    assert received_theme(value) == expected


def test_federated_theme(app, two_domains):
    default = two_domains.domains[0]
    on_forest, on_piefed, on_gone = (make_theme(default.id, base) for base in ('forest', 'piefed', 'gone-theme'))
    assert federated_theme(custom_identifier(on_forest.id)) == 'forest'
    assert federated_theme(custom_identifier(on_piefed.id)) is None
    assert federated_theme(custom_identifier(on_gone.id)) is None
    assert federated_theme('custom:999999') is None
    assert federated_theme('forest') == 'forest'
    assert federated_theme('disabled') == 'disabled'
    assert federated_theme('') is None
    assert federated_theme(None) is None


def test_a_local_community_federates_its_custom_themes_base(app, two_domains):
    default = two_domains.domains[0]
    community = two_domains.local_community('dusk', default)
    community.theme = custom_identifier(make_theme(default.id, 'forest').id)
    db.session.commit()
    with app.test_request_context():
        document = community_actor_data(community)
    assert document['theme'] == 'forest'
    assert 'custom:' not in str(document)
    community.theme = custom_identifier(make_theme(default.id, 'piefed').id)
    db.session.commit()
    with app.test_request_context():
        assert 'theme' not in community_actor_data(community)


class TestInbound:
    def test_a_custom_identifier_is_not_stored(self, app, db_session):
        peer_instance(PEER)
        community = actor_json_to_model(_group('memes', fields={'theme': 'custom:1'}), '!memes', PEER)
        assert community.theme == ''

    def test_an_unknown_name_is_not_stored(self, app, db_session):
        peer_instance(PEER)
        community = actor_json_to_model(_group('memes', fields={'theme': '../../etc'}), '!memes', PEER)
        assert community.theme == ''

    def test_a_shipped_name_is_stored(self, app, db_session):
        peer_instance(PEER)
        community = actor_json_to_model(_group('memes', fields={'theme': 'forest'}), '!memes', PEER)
        assert community.theme == 'forest'

    @pytest.mark.parametrize('sent', ['custom:1', 'nope', ['forest'], 7])
    def test_an_update_keeps_the_stored_theme_over_one_not_to_store(self, app, db_session, http_mock, sent):
        community = _remote_community()
        community.theme = 'forest'
        db.session.commit()
        refresh_community_profile_task(community.id, _group_document(fields={'theme': sent}))
        db.session.refresh(community)
        assert community.theme == 'forest'

    def test_an_update_stores_a_shipped_name(self, app, db_session, http_mock):
        community = _remote_community()
        refresh_community_profile_task(community.id, _group_document(fields={'theme': 'amethyst'}))
        db.session.refresh(community)
        assert community.theme == 'amethyst'
