# Plain community names

Date: 2026-11-19. Status: implemented (`app/plain_names.py`, `tests/test_plain_names.py`).

## Goal

Make the site usable by people who don't know what the fediverse is. The web view stops showing which server a
community lives on and refers to a community by name alone. People who want the protocol details can turn them back
on. Only the HTML served to browsers changes: the API, ActivityPub, stored data and federation stay the same.

## The setting

- `User.show_instance_details` (boolean, default false), labelled **Enable FediVerse instance details** in
  Settings → User interface.
- On: the web view is unchanged from before this feature.
- Off (the default, and always for logged-out visitors): the rest of this document applies.
- `instance_details_shown()` reads it. Outside a request context (Celery tasks, the CLI) it is always true, so
  nothing stored or sent depends on a reader's preference.

## Resolving a name

`resolve_community_name(name)` returns the community a bare name means to the current reader, memoized per request:

1. the local community of that name on this storefront (`local_clause`), unless hidden here;
2. else a remote community of that name the reader is a member of (including as moderator), most subscribed first;
3. else any non-private remote community of that name, most subscribed first.

It skips banned communities and communities hidden here (`hidden_here`), as well as instances the reader banned
or blocked. "Most subscribed" means the community's subscribers **on this instance** (`subscriptions_count`, which
we count ourselves); ties go to the oldest (lowest id). A remote server's reported total
(`total_subscriptions_count`) is never used: a squatter could inflate it to take over a popular bare name. `_rank`
repeats this order in memory for lists that are already loaded, so a menu and the page its link opens pick the same
community. Its "local" is `here()`: local to this request's storefront, as `local_clause` means it. On one of
several domains an install serves, a community local to a sister domain ranks as any other, never as this domain's
own.

## What changes when the setting is off

| Surface | Behaviour |
|---|---|
| Community names | `Community.display_name()` returns the title alone only while a web template renders (render depth tracked by Flask's template signals; `email/` templates excluded). Python code outside rendering still gets `title@domain`, so notifications, flash messages that get stored, and the API are unaffected. `render_communityname` and the hand-built `title@ap_domain` breadcrumbs (`community_label()`) drop the domain. Post teasers drop the `@domain` span. |
| Community links | Navigational links (`href="/c/{{ community_href(c) }}"`, breadcrumbs, `post_community_link`) to a remote community use the bare name. A local community keeps `link()`: it is already bare on its own domain, and on another of our storefront domains the bare name means that domain's own community. A server view keeps its `microblogs@<host>` link. **Action URLs (join, leave, submit, moderate, flair, RSS) keep `community.link()`**, so they always act on the exact community. |
| `/c/<name>` | For a browser request with no `@`, if no local community matches, the route shows the resolved community instead of redirecting to "add local community". ActivityPub requests are handled as before (404 for a name that isn't local). |
| Posting | `possible_communities()` (the share and cross-post destination choices) and `post_destination_sections()` (the post chooser) list each name once, grouped as in the menu and labelled by title. In `possible_communities()`, the reader's own sections are grouped only among themselves. The "Others" section (every other community, whose names and titles anyone can choose) is grouped only within itself, and an entry that reads as one of the reader's own names or titles is dropped, so a stranger's community can never replace, hide or merge the reader's own. `main.add_post` checks a pick against `possible_communities(plain=False)`, the full list, because the chooser groups only the reader's own communities and can keep one that loses its name among all communities. A community's own "create post" button still posts to that community. |
| Mentions in posts and comments | The `community_links` filter (applied when rendering) shows a handle (`!name@domain`, as text or as a peer's link) as `!name`, but **the link still goes to the community the handle names**: `/c/<handle>` when we know it, otherwise the lookup page (for text) or the peer's own href (for a link). A handle never resolves to another community of the same name. A bare `!name` is linked to `/c/name` only when it resolves. Lookups are memoized per request, and new ones are capped at `MENTION_LOOKUPS` (100) per request; past the cap a handle links to the lookup page and a bare name stays text, without a query. Code blocks are left alone. Stored `body_html` is never changed. |
| Community theme | `base.html` and the side pane take the theme (and the per-user "allow theme" toggle) from `theme_community(c)`, the community that `c.name` resolves to. |
| Navbar communities menu and topics menu | Each name is listed once across Favorites, Moderating and Joined, in the first section that holds it. Two communities count as one name when their titles read the same (what the menu shows) or their URL names do (where `/c/<name>` leads). Both comparisons ignore case and treat `_`, `-` and runs of spaces as one space (`name_key`), and the grouping is transitive (`_duplicate_groups`). The community `_resolve`'s order ranks first stands in for the whole group. The `@domain` duplicate marker is gone. |
| All communities list (`/communities`, and the communities search, which redirects there) | Each title and each name is listed once among the communities the filters and search leave. `plain_list_clause()` keeps a community only when `_rank`'s order (local here, then joined, then most subscribed here) puts it first among those with its title and among those with its name, both read as `name_key` does. The database does it (window functions), so the count and the pages follow the shorter list and a request costs no more however many communities there are. Unlike the menu's transitive grouping, a chain (A shares a title with B, B a name with C) can keep two of the chain; no two kept rows ever share a title or a name. Server view rows are not listed, as every one reads "microblogs"; the microblogs community stands for them. |
| Server views (`microblogs@<host>`) | Their headings and post links show plain "microblogs". |

## Not changed

- ActivityPub documents, inbox processing, outgoing activities and webfinger.
- `/api/*` responses.
- Anything stored: `body_html`, notification titles, report text.
- Emails, which go to a recipient, not to the person whose request sent them.
- Admin pages that show `ap_domain` explicitly (e.g. Admin → Communities).
- User names (`alice@host`). This feature only changes communities.

## Trade-offs

- When two communities share a name, the one that loses resolution can't be reached by a bare link. A reader can
  still find it through search or the All communities list, or by turning the setting on.
- On a community page opened through a bare name, links built from `community.link()` (pagination, sort) show the
  full handle in the address bar.

## Housekeeping

The community name `microblogs` is now one constant, `MICROBLOGS_COMMUNITY_NAME` in `app/constants.py`. It replaces
the string literals in Python and templates, and `community.live.LIVE_COMMUNITY` is set from it. The two
`is_local_microblogs` functions (`app/utils.py`, `app/community/live.py`) stay separate on purpose: one asks whether
a community is local by `is_local()` and the other by `ap_id is None`.
