"""Property checks for the source, bug report and get-involved links an admin sets (app/project_links.py).

* project_links_url: random text through is_web_url. No exception; a True answer is an http or https URL with a host
  and no white space, as urlsplit reads it.
* project_links_footer: random text as the two footer links, through footer_invitation over the upstream message. No
  exception; the result is Markup, and its only elements are the message's own two links: no text of a link becomes
  markup.

No database and no network.
"""
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from markupsafe import Markup

from app.project_links import UPSTREAM_SOURCE, ProjectLinks, footer_invitation, is_web_url
from tests.fuzz.targets._target import Target
from tests.fuzz.targets.signatures import _fields, _text

MAX_INPUT = 4096
INVITATION = Markup('Please <a href="https://codeberg.org/rimu/pyfedi/issues">report bugs</a> or '
                    '<a href="https://join.piefed.social/get-involved/">get involved</a>.')


def check_project_links_url(data: bytes) -> None:
    value = _text(data[:MAX_INPUT])
    if is_web_url(value):
        parts = urlsplit(value)
        assert parts.scheme in ('http', 'https') and parts.hostname, f'{value!r} accepted'
        assert not any(character.isspace() for character in value), f'{value!r} accepted with white space'


def check_project_links_footer(data: bytes) -> None:
    bugs, involved = (_text(field) for field in _fields(data[:MAX_INPUT], 2))
    written = footer_invitation(INVITATION, ProjectLinks(UPSTREAM_SOURCE, bugs, involved))
    assert isinstance(written, Markup)
    tags = BeautifulSoup(str(written), 'html.parser').find_all(True)
    assert [tag.name for tag in tags] == ['a', 'a'], f'{written!r}'
    assert all(list(tag.attrs) == ['href'] for tag in tags), f'{written!r}'


TARGETS = [
    Target(name='project_links_url', check=check_project_links_url, surface='project_links', budget_ms=None,
           needs_app=False),
    Target(name='project_links_footer', check=check_project_links_footer, surface='project_links', budget_ms=None,
           needs_app=False),
]
