"""Property checks for the sidebars' box order (app/sidebar.py) and the admin buttons that change it
(app/admin/sidebar_routes.py).

A stored order is text an admin's buttons wrote, but it is read on every page, so whatever is in the column must read
as a usable order; and the button's value is posted form text.

* sidebar_layout: random text as a stored order of each sidebar. No exception; every box of that sidebar exactly once,
  in an order that `stored` writes and `layout` reads back unchanged.
* sidebar_action: random text as a posted button over a random stored order. No exception; None (refused), or the
  stored values of both sidebars after it, each a stored order of the first property's kind, or None (the inherited
  order) for the sidebar a reset names. Only the sidebar the button names changes.

No database and no network.
"""

from app.admin.sidebar_routes import apply_action
from app.sidebar import BOXES, layout, stored
from tests.fuzz.targets._target import Target
from tests.fuzz.targets.signatures import _fields, _text

MAX_INPUT = 4096


def _usable(value: str, area: str) -> None:
    boxes = layout(value, area)
    assert sorted(name for name, _shown in boxes) == sorted(BOXES[area]), f'{value!r} read as {boxes!r}'
    assert all(isinstance(shown, bool) for _name, shown in boxes)
    assert layout(stored(boxes), area) == boxes, f'{boxes!r} does not read back as itself'


def check_sidebar_layout(data: bytes) -> None:
    value = _text(data[:MAX_INPUT])
    for area in BOXES:
        _usable(value, area)


def check_sidebar_action(data: bytes) -> None:
    home, community, action = (_text(field) for field in _fields(data[:MAX_INPUT], 3))
    values = {'home': home, 'community': community}
    result = apply_action(values, values, action)
    if result is None:
        return
    assert set(result) == set(BOXES)
    changed = [area for area in BOXES if result[area] != values[area]]
    assert len(changed) <= 1, f'{action!r} changed {changed!r}'
    for area in BOXES:
        if result[area] is None:
            assert action == f'reset:{area}', f'{action!r} cleared {area}'
        elif area in changed:
            _usable(result[area], area)


TARGETS = [
    Target(name='sidebar_layout', check=check_sidebar_layout, surface='sidebar', budget_ms=None, needs_app=False),
    Target(name='sidebar_action', check=check_sidebar_action, surface='sidebar', budget_ms=None, needs_app=False),
]
