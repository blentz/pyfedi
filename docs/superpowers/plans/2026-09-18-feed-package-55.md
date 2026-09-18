# Sub-project 55 implementation plan: `app/feed/util.py` and `app/feed/forms.py`

**Design:** `docs/superpowers/specs/2026-09-18-feed-package-55-design.md`
**Base:** `blentz` at `93f719f67`
**Targets:** 64 missing statements, 45 missing arcs across the two modules.

## Global Constraints

Unchanged. **Two floors this round**, each the measured blended figure rounded
down, with the checker re-run after they land — 31 floors, and `app/feed` closed.

## Harness facts for these modules

- **`search_for_feed` sleeps `randint(3, 10)` seconds on a webfinger HTTPError
  and retries.** Patch `app.feed.util.sleep` in every test that reaches it, or
  the suite pays ten seconds a row.
- `get_request` is imported into `app.feed.util`; patch it there. `respx` is
  active in this suite, so an unpatched call fails loudly rather than reaching
  the network (`AllMockedAssertionError`).
- `actor_json_to_model` and `retrieve_mods_and_backfill` are imported into
  `app.feed.util` at module level — patch them there too.
- The forms are exercised through a request context with formdata, not through
  a route: `AddCopyFeedForm(formdata=MultiDict({...}))` inside
  `app.test_request_context()`. `apply_feed_url_rules` reads `current_user`, so
  the context needs a logged-in user for the private-url arm.

## File Structure

| file | responsibility |
|---|---|
| `tests/test_feed_util.py` | **Create.** `app/feed/util.py`'s tests. |
| `tests/test_feed_forms.py` | **Create.** `app/feed/forms.py`'s tests. |
| `app/feed/forms.py` | **Modify** once: P1's guard. |
| `app/feed/util.py` | **Modify** once: P2's rejection. |
| `coverage_floors.ini` | **Modify.** Two new lines. |
| `tests/README.md`, the register | Facts from **302**, findings from **D733**. |

---

## Task 1: P1 and P2

**Pins**: a POST to `/feed/new` with no url field raises `AttributeError`
(asserted as the exception, since the client re-raises); `search_for_feed(
'~name@host@extra')` raises `ValueError`.

**Fixes**: `EditFeedForm`'s `is not None` guard in `AddCopyFeedForm`; and a
rejection in `search_for_feed` for an address that does not split into exactly
two parts.

**Inversions**: the POST comes back as a form error rather than a crash — assert
the `url` field's error, not merely a 200; and `search_for_feed` returns None
for the two-`@` address, with a control that a well-formed one still resolves.

## Task 2: `app/feed/util.py`

- `feeds_for_form` and `feeds_for_form_children`: a three-level tree, asserting
  the `--` depth prefixes and that `current_feed` is skipped at every level —
  the recursion is the whole function.
- `search_for_feed`: the banned-instance raise with and without a reason; the
  local-server shortcut; an already-known remote feed; `allow_fetch=False`; the
  webfinger path end to end with `get_request` patched, including the retry
  after one `httpx.HTTPError` and the give-up after two; a 404 from webfinger; a
  `links` entry without `rel`; a non-`Feed` actor type; and `actor_json_to_model`
  returning None.
- `actor_to_feed`: the `@` and bare-name arms, the bare arm with **mixed case**
  (it compares `func.lower` on both sides) and with `ap_id=None` load-bearing —
  a remote feed with the same name must not match.
- `feed_communities_for_edit`: a local community (gets `@SERVER_NAME` appended),
  a remote one (already has a host), a banned one (excluded), and the sort
  order — the function returns them sorted, and a two-entry fixture in the wrong
  order is what proves it.
- `initialise_new_communities`: the `num_communities == 0` early return; a
  community with posts (skipped); one without, in both debug states — the debug
  arm `break`s after one, the other dispatches for all, and two candidate
  communities are what make that visible.

## Task 3: `app/feed/forms.py`

- `AddCopyFeedForm.validate`: the empty url; the url that fails
  `apply_feed_url_rules`; a url colliding with a **local community**; one
  colliding with a **user**, both live and deleted (two different messages); and
  the communities field's format check, including the blank-line `continue`.
- `EditFeedForm.validate`: the same shapes where they differ, and the
  `url.data is not None` guard that P1 copies.
- `SearchRemoteFeed`: whatever validation it carries.

## Task 4: floors, suite, mutation, register

The `&&` chain; both floors added and the checker re-run; a mutation pass over
both modules; entries from **D733**, facts from **302**.

## Self-review

1. `git diff --numstat <base> HEAD -- app/` names exactly the two modules.
2. Pins inverted, original claims struck, P1's naming the twin it copies.
3. Citations checked at the commit that writes them.
4. Both floors are measured figures rounded down, and the checker passes.
5. No probe file, no `# MUT`, `git status` clean but for the intended files.
