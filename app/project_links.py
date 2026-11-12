"""Where this server's source code, bug tracker and get-involved page are: the footer's links, nodeinfo's repository
and the Mastodon API's source_url. Under the AGPL (§13) users of a modified server must be offered its source, so an
admin running one sets the source URL to where that source is. Each is set on the site profile; a blank one is
upstream PieFed's.
"""
from __future__ import annotations

from typing import NamedTuple
from urllib.parse import urlsplit

from flask import g

UPSTREAM_SOURCE = 'https://codeberg.org/rimu/pyfedi'
UPSTREAM_BUG_REPORT = 'https://codeberg.org/rimu/pyfedi/issues'
UPSTREAM_GET_INVOLVED = 'https://join.piefed.social/get-involved/'
# The Site column each is stored in, and its default
FIELDS = {'source_url': UPSTREAM_SOURCE, 'bug_report_url': UPSTREAM_BUG_REPORT,
          'get_involved_url': UPSTREAM_GET_INVOLVED}
MAX_LENGTH = 256


class ProjectLinks(NamedTuple):
    source_url: str
    bug_report_url: str
    get_involved_url: str


def project_links(site=None) -> ProjectLinks:
    """The three links of `site` (the request's g.site by default), each its own where set, else upstream's."""
    site = g.site if site is None else site
    return ProjectLinks(*((getattr(site, field, None) or default) for field, default in FIELDS.items()))


def is_web_url(value: str) -> bool:
    """Whether `value` is an absolute http or https URL with a host: what a link may be set to."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in ('http', 'https') and bool(parts.hostname) and not any(c.isspace() for c in value)


def footer_invitation(translated, links: ProjectLinks):
    """The footer's translated "Please report bugs or get involved" with its two links pointed at `links`. The
    message keeps upstream's URLs, which every translation copies, so no translation is lost; `translated` is Markup,
    whose replace escapes the URLs put in."""
    return translated.replace(UPSTREAM_BUG_REPORT, links.bug_report_url) \
        .replace(UPSTREAM_GET_INVOLVED, links.get_involved_url)
