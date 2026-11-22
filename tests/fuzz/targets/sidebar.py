"""Property checks for the sidebars' box order (app/sidebar.py) and the admin buttons that change it
(app/admin/sidebar_routes.py), and a user's own order and the buttons that change it (app/user/sidebar_routes.py).

A stored order is text an admin's buttons wrote, but it is read on every page, so whatever is in the column must read
as a usable order; and the button's value is posted form text.

* sidebar_layout: random text as a stored order of each sidebar. No exception; every box of that sidebar exactly once,
  in an order that `stored` writes and `layout` reads back unchanged.
* sidebar_action: random text as a posted button over a random stored order. No exception; None (refused), or the
  stored values of both sidebars after it, each a stored order of the first property's kind, or None (the inherited
  order) for the sidebar a reset names. Only the sidebar the button names changes.
* sidebar_user_action: the same over a user's order, with a random part of each sidebar's boxes offered. A result's
  changed order reads, through `personal_layout`, as each offered box exactly once and nothing else.

No database and no network.
"""

from app.admin.sidebar_routes import apply_action
from app.sidebar import BOXES, layout, personal_layout, stored
from app.user.sidebar_routes import apply_action as apply_user_action
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


def check_sidebar_user_action(data: bytes) -> None:
    home, community, action, picks = (_text(field) for field in _fields(data[:MAX_INPUT], 4))
    values = {'home': home or None, 'community': community or None}
    offered = {area: [(name, ord(picks[index]) % 4 != 1) for index, name in enumerate(BOXES[area])
                      if index < len(picks) and ord(picks[index]) % 2] for area in BOXES}
    result = apply_user_action(values, offered, action)
    if result is None:
        return
    changed = [area for area in BOXES if result[area] != values[area]]
    assert len(changed) <= 1, f'{action!r} changed {changed!r}'
    for area in BOXES:
        if result[area] is None:
            assert values[area] is None or action == f'reset:{area}', f'{action!r} cleared {area}'
        elif area in changed:
            boxes = personal_layout(result[area], area, offered[area])
            assert sorted(name for name, _shown in boxes) == sorted(dict(offered[area])), \
                f'{result[area]!r} read as {boxes!r}'


TARGETS = [
    Target(name='sidebar_layout', check=check_sidebar_layout, surface='sidebar', budget_ms=None, needs_app=False),
    Target(name='sidebar_action', check=check_sidebar_action, surface='sidebar', budget_ms=None, needs_app=False),
    Target(name='sidebar_user_action', check=check_sidebar_user_action, surface='sidebar', budget_ms=None,
           needs_app=False),
]
