"""The boxes the home page and community page sidebars show, in the order admins set, each domain its own.

A sidebar's stored value (`Site.home_sidebar`, `Site.community_sidebar` and the same columns of `DomainBranding`) is
its box names, comma-separated, in the order they show, a hidden box's name after a '-'. A domain whose value is blank
shows the site's (app/branding.py). A blank value, a name no box has, or a box the value leaves out are read so that
every box is there once: unknown names and repeats are dropped, and a missing box is shown after the others, in its
default place among them. A shown box with nothing in it (no new communities, say) still shows nothing.

Templates render each box into `boxes` by name and hand them to `sidebar_boxes`, which writes those shown in order.
"""
from __future__ import annotations

from flask_babel import lazy_gettext as _l
from markupsafe import Markup

from app.branding import current_branding

HOME = 'home'
COMMUNITY = 'community'
HIDDEN = '-'

# Each sidebar's boxes in their default order, with the names admins see
BOXES = {
    HOME: {
        'new_communities': _l('New communities'),
        'active_communities': _l('Active communities'),
        'upcoming_events': _l('Upcoming events'),
        'recent_tags': _l('Recent tags'),
        'about': _l('About this site'),
        'new_instances': _l('New instances'),
    },
    COMMUNITY: {
        'actions': _l('Actions: create post, join, settings, search'),
        'upcoming_events': _l('Upcoming events'),
        'microblog_servers': _l('Servers'),
        'about_community': _l('About the community'),
        'related_communities': _l('Related communities'),
        'community_feeds': _l('Feeds'),
    },
}
# The Site and DomainBranding column (and Branding field) each sidebar is stored in
FIELDS = {HOME: 'home_sidebar', COMMUNITY: 'community_sidebar'}

Layout = list[tuple[str, bool]]   # every box of a sidebar in order, and whether it is shown


def layout(stored: str | None, area: str) -> Layout:
    """Every box of `area`'s sidebar once, in the order `stored` gives, and whether each is shown."""
    known = BOXES[area]
    boxes = {}
    for item in (stored or '').split(','):
        item = item.strip()
        name = item.removeprefix(HIDDEN)
        if name in known and name not in boxes:
            boxes[name] = not item.startswith(HIDDEN)
    for name in known:
        boxes.setdefault(name, True)
    return list(boxes.items())


def stored(boxes: Layout) -> str:
    """What `layout` reads back as `boxes`."""
    return ','.join(name if shown else HIDDEN + name for name, shown in boxes)


def moved(boxes: Layout, name: str, step: int) -> Layout:
    """`boxes` with `name` moved `step` places later (earlier, if negative), stopping at either end."""
    names = [box for box, _shown in boxes]
    here = names.index(name)
    there = min(max(here + step, 0), len(boxes) - 1)
    result = list(boxes)
    result.insert(there, result.pop(here))
    return result


def toggled(boxes: Layout, name: str) -> Layout:
    """`boxes` with `name` hidden if it was shown, shown if it was hidden."""
    return [(box, not shown if box == name else shown) for box, shown in boxes]


def shown_boxes(area: str) -> list[str]:
    """The names of the boxes the request's domain shows in `area`'s sidebar, in order."""
    return [name for name, shown in layout(getattr(current_branding(), FIELDS[area]), area) if shown]


def sidebar_boxes(area: str, boxes: dict) -> Markup:
    """The rendered `boxes` (box name to its markup) this domain shows in `area`'s sidebar, in its order. A box that
    rendered to nothing but white space is left out."""
    return Markup('\n').join(Markup(boxes[name]) for name in shown_boxes(area)
                             if name in boxes and str(boxes[name]).strip())
