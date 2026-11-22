# A user's own sidebar order

A signed-in user chooses which of the sidebars' boxes they see and in what order, for the home page sidebar and the
community pages' sidebar, each separately. It builds on the admins' order (spec 2026-11-20-multitenant-domain-config
MT4, `app/sidebar.py`): the domain decides which boxes there are, and the user arranges those.

## Stored

`user.home_sidebar` and `user.community_sidebar` (text, NULL; migration `5b7e0c3d9a41`), in the format the site's and
each domain's columns use: box names comma-separated in order, a hidden one after a `-`. NULL means the user has no
order of their own and sees the domain's.

## What a page shows

`shown_boxes(area)`:

1. The boxes the request's domain shows, in its order (`domain_offered`). A box the domain hides is never shown and
   not offered to users, except an optional box (spec 2026-11-22-sidebar-live-feed), which is always offered.
2. For a signed-in user with an order of their own, those boxes in the user's order, without the ones they hid
   (`personal_layout`). A box their value does not name (one the domain started showing after they saved) comes
   after the boxes they arranged, shown. Names their value has that the domain does not show are ignored.
3. Anyone else: the domain's order.

The same order applies on every domain the user visits, within the boxes each shows.

## The settings page

`/user/settings/sidebar` ("Sidebar" in the settings nav), signed in. It lists each sidebar's offered boxes in the
user's order with up, down and hide/show buttons, and "Use the site's order" when they have their own. Each button is
one POST (`<sidebar>:<up|down|toggle>:<box>` or `reset:<sidebar>`), CSRF-checked, then a redirect back. Nothing needs
JavaScript. A user's first change starts from the domain's order. The value stored names the offered boxes only, so
moving a box passes the one next to it on the page. A button naming a box the page does not offer is a 400.

## Not changed

- The admins' page and the domain's order are unchanged; the box list and `changed` moved into `app/sidebar.py` to be
  shared.
- The settings export and the APIs do not carry the order.
