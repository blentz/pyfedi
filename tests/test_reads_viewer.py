"""app/reads/viewer.py: a viewer's restrictions, loaded per list on first use."""
import pytest

from app.reads import viewer
from app.reads.viewer import Restrictions

LISTS = {
    'blocked_person_ids': 'blocked_users',
    'blocked_community_ids': 'blocked_communities',
    'blocked_instance_ids': 'blocked_or_banned_instances',
    'blocked_domain_ids': 'blocked_domains',
    'private_community_ids': 'community_membership_private',
    'read_language_ids': 'user_filters_languages',
}


@pytest.fixture
def loaders(monkeypatch):
    """Each loader answers with its own name and the user id, and counts its calls."""
    calls = []
    for loader in LISTS.values():
        monkeypatch.setattr(viewer, loader, lambda user_id, name=loader: calls.append(name) or [name, user_id])
    return calls


@pytest.mark.parametrize('attribute, loader', sorted(LISTS.items()), ids=sorted(LISTS))
def test_each_list_comes_from_its_loader(loaders, attribute, loader):
    assert getattr(Restrictions(7), attribute) == [loader, 7]


def test_a_list_loads_once_and_only_when_read(loaders):
    restrictions = Restrictions(7)
    assert loaders == []
    restrictions.blocked_person_ids
    restrictions.blocked_person_ids
    assert loaders == ['blocked_users']


@pytest.mark.parametrize('restrictions', [Restrictions(None), Restrictions.none()], ids=['anonymous', 'exempt'])
def test_no_viewer_has_no_restrictions_and_asks_nothing(loaders, restrictions):
    assert [getattr(restrictions, attribute) for attribute in LISTS] == [[]] * len(LISTS)
    assert loaders == []


def test_the_real_loaders_answer_for_a_user(app, api_baseline):
    """Wired to app.utils: a fresh user has blocked nothing and reads every language."""
    restrictions = Restrictions(api_baseline.user3.id)
    assert restrictions.blocked_person_ids == []
    assert restrictions.blocked_community_ids == []
    assert restrictions.blocked_domain_ids == []
    assert not restrictions.read_language_ids   # None: every language
