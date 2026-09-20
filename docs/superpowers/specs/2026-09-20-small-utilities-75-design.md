# Sub-project 75: three small utilities — the translation client, the sidebar helpers, and a markdown residual

**Status:** design approved, ready to implement
**Branch:** `blentz`, at `ae2dfca0e`
**Predecessor:** sub-project 74, which closed the app factory — 53 floors.

## Goal

Close three modules that are each too small to be a round of their own, and take
three floors — 56.

| module | before | gaps | what it is |
|---|---|---|---|
| `app/translation.py` | 20.930% | 34 | a vendored LibreTranslate HTTP client |
| `app/main/util.py` | 71.429% | 22 | the sidebar's community lists |
| `app/markdown_extras.py` | 96.396% | 4 | one residual in the image-attribute parser |

Bundled for the reason sub-project 69 bundled its three: the alternative is
three rounds of ceremony around a handful of assertions each.

## `app/translation.py` — reachable entirely through `http_mock`

35 statements, all of them HTTP. Every method posts or gets, calls
`raise_for_status()`, and reads one key out of the JSON. `httpx_client` is the
module-level client the campaign already mocks with `respx`, so nothing here
needs the network.

The uncovered surface is the whole file: three methods, the API-key branch on
each, and the constructor's trailing-slash handling.

**Note the constructor's `assert`.** `assert len(self.url) > 0` is a production
assertion, which `python -O` removes — registered as R1 rather than repaired,
because turning it into a raise changes what an operator's misconfiguration
does and that is not a coverage round's call.

## `app/main/util.py` — the anonymous and the blocked

Two near-identical functions, `sidebar_active_communities` and
`sidebar_new_communities`, each with:

- an anonymous fast path (`user_id == 0`) that skips every per-user filter,
- three `if <ids>:` guards for communities the reader is banned from, has
  blocked, or whose instance they have blocked.

Neither function's guards have ever been exercised: the missing arcs are
`[18,19]`, `[22,23]`, `[25,26]`, `[28,29]` and the same four again at `[36,37]`
through `[46,47]`.

`_base_list_communities_context` (`:71-76`) is uncovered outright, and
`reload_url` is missing the arc `[92,95]` — the authenticated reader whose sort
is neither `new` nor a `top*`.

**Both sidebar functions are `@cache.memoize`**, which is a no-op under the
suite's `NullCache` (fact from sub-project 70's neighbourhood), so the rows do
not have to fight a cache.

## `app/markdown_extras.py` — one residual

`:159` and the arcs `[71,84]`, `[79,84]`, `[158,159]`: the `add_attrs` early
return when a marker carries no `data-enhanced-img` id, and the two ways the
comma-splitting branch can decline to produce a second URL.

## No production change expected

None of the three shows a defect on reading. If the rows turn one up it is
repaired with them, as always — but this round is scoped as coverage, and
sub-project 74's result is the precedent for saying so up front.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `app/translation.py:38` | **`assert len(self.url) > 0` in production code.** `python -O` strips it, and the failure then moves to a confusing place — `self.url[-1]` on an empty string raises `IndexError` instead. | Turning it into a raise changes what a misconfigured `LIBRETRANSLATE_URL` does at startup. An operator-facing decision. |
| R2 | `app/translation.py:1` | **The file is a vendored copy** of LibreTranslate's own client, kept as a copy rather than a dependency. | Named so the next reader knows edits here do not flow upstream, and that the upstream has since changed. |
| R3 | `app/main/util.py:17-48` | **`sidebar_active_communities` and `sidebar_new_communities` are the same function twice**, differing only in a date cutoff and the final `order_by`. Eight of this round's rows exist twice for that reason. | A refactor across a file the sidebar renders on every page; both copies are covered here, so the duplication is pinned. |

## Success criteria

- All three modules at `[]`/`[]` on the **full-suite** run — which is the number
  that counts, per fact 339.
- Three floors of 100 — **56 floors**.
- Suite green; floors checked with `&&`; a mutation pass over the translation
  client and the sidebar guards, anchors checked first — D833.
- Findings registered from **D874**; `tests/README.md` facts from **340**.
