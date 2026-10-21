"""The community page offers Settings to everyone the settings page admits: any moderator, not only the owner."""
import pytest

from tests.factories import make_community_member
from tests.test_per_domain_html_routes import signed_in, world  # noqa: F401
from tests.two_domains import HELL, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

# the Options menu of the page header (every theme with it), and dillo's own settings card
SETTINGS_LINKS = {
    'piefed': 'class="dropdown-item" href="/community/{id}/edit"',
    'dillo': '<p><a href="/community/{id}/edit" class="btn btn-primary">',
}


def settings_link(app, art, name, two_domains, moderator, theme):
    """Whether the page of `art`, seen by a new member (a moderator, or not), offers the link in `theme`."""
    person = two_domains.local_user(name, two_domains.domains[0])
    person.private_key = 'a key'
    person.theme = theme
    make_community_member(person, art, is_moderator=moderator)
    page = signed_in(app, person, HELL).get('/c/art').get_data(as_text=True)
    return SETTINGS_LINKS[theme].format(id=art.id) in page


@pytest.mark.parametrize('theme', sorted(SETTINGS_LINKS))
def test_a_moderator_who_is_not_the_owner_sees_the_settings_link(app, world, two_domains, theme):
    assert settings_link(app, world.art[HELL], 'themod', two_domains, True, theme) is True


@pytest.mark.parametrize('theme', sorted(SETTINGS_LINKS))
def test_a_plain_member_does_not_see_the_settings_link(app, world, two_domains, theme):
    assert settings_link(app, world.art[HELL], 'justmember', two_domains, False, theme) is False
