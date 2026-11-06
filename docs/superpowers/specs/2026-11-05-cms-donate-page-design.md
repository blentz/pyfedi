# The Donate page as a CMS page, and header and footer links for CMS pages

Date: 2026-11-05. Status: built (tests: tests/test_cms_donate_page.py).

## Summary

`/donate` is now a CMS page like any other: seeded, edited at `/admin/pages`, and served by the same catch-all as every
other page. Any CMS page can be linked from the header and the footer. What a donor needs in order to pay or to stop
paying moved to `/donations`, in code.

This is the first step of a wider monetization track, which gets its own spec. That track covers infrastructure only:
admin-configured links and integrations for donations, subscriptions and microtransactions. What payment buys is
deliberately not decided yet.

## Rulings

| # | Ruling |
|---|---|
| D1 | `/donate` behaves like any other CMS page. Its links follow its title and URL, and deleting it removes it entirely. |
| D2 | CMS pages opt in to header and footer links separately. A new page has neither. |
| D3 | The Stripe plan cards and the cancel and update-card buttons stay in code, at `/donations`, so deleting the CMS page cannot stop a donor from cancelling. |
| D4 | `/donations` needs a login. Without Stripe or on the onion it answers 404 with a page saying plans are not available, without asking anybody to log in. It is reached from the seeded page and the account menu: "Donation plans", or "Manage donations" for a donor. |
| D5 | The migration seeds `/donate` linked from both places, with the sidebar on. A site with its own `/donate` page keeps its text and gets both links and the sidebar, as it had until now. |
| D6 | `flask init-db` seeds the same page, because it drops every table. |
| D7 | The downgrade deletes the page only if its body is still word for word the seed. |
| D8 | A page URL must be a path on this site: `//host` and any backslash are refused, because the URL is now a link on every page. |
| D9 | A CMS page opts in to the site sidebar with `show_sidebar`. A new page has none. `/about` ignores it, having its own layout. |
| D10 | The footer's fixed `/about` and `/privacy` links win over a page at those URLs opted in to the footer: one link, with the fixed label. The header may still link to them. |
| D11 | The seeded page links to `/donations` on every site, which is why D4's 404 explains itself. |

## Consequences

- The seeded text is English for every viewer. A CMS page has one language, so the translated strings for this page are
  no longer used. The link label is the page title, so it is not translated either.
- The sidebar's RSS link is gone. `/donate` never passed it a feed, so its `href` was empty.
- An anonymous visitor no longer gets the "Log in to donate" flash, and a member on a Stripe site sees the CMS page at
  `/donate` like everybody else.
- Page links are cached for an hour and cleared on add, edit and delete. A change made directly in the database can take
  up to that hour to show.
