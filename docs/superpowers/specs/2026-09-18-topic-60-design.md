# Sub-project 60: `app/topic/routes.py` — closing the package

**Status:** design approved, ready for an implementation plan
**Branch:** `blentz`, at `c60264813`
**Predecessor:** sub-project 59, which floored `app/activitypub/actor.py` at 98
— 36 floors.

## Goal

Cover the topic package's only uncovered file — the topic page, its RSS feed,
the submit form, the notification toggle and the suggestion form — repair the
two defects probed while scoping it, and **take a floor, which closes
`app/topic`** (`__init__.py` and `forms.py` are already at 100).

## Targets

From the full-suite JSON at the delivered tree (`app/topic/routes.py` 48.197%,
223 statements):

| function | where | missing statements | missing arcs | total |
|---|---|---|---|---|
| `show_topic` | `:32-211` | 54 | 44 | 98 |
| `suggest_topics` | `:311-325` | 12 | 4 | 16 |
| `topic_create_post` | `:272-286` | 10 | 4 | 14 |
| `show_topic_rss` | `:216-265` | 6 | 6 | 12 |
| `topic_notification` | `:291-306` | 9 | 2 | 11 |
| `get_all_child_topic_ids` | `:334-339` | 4 | 2 | 6 |
| `suggestion_denied` | `:330-331` | 1 | 0 | 1 |
| **total** | | **96** | **62** | **158** |

Above the 110-130 band by 28, taken at that size because it is what the package
has left and this round closes it — the same reason sub-project 55 gave.

## THE TWO PRODUCTION CHANGES

### P1 — the "next page" link points into an empty page

```python
has_next_page = len(post_ids) > page + 1 * page_length      # app/topic/routes.py:103
```

`*` binds tighter than `+`, so this asks whether there are more than
`page + page_length` posts. The intent — and what `paginate_post_ids` slices
by — is `(page + 1) * page_length`.

**Probe:**

```
PROBE t1 page=0 total=30 actual_next=True  intended_next=True
PROBE t1 page=1 total=30 actual_next=True  intended_next=False
PROBE t1 page=2 total=30 actual_next=True  intended_next=False
PROBE t1 page=1 total=21 actual_next=False intended_next=False
```

Page 0 agrees by arithmetic accident, which is why the bug survived: the first
page is the one anyone looks at. From page 1 on, the reader is offered a next
page that renders nothing.

**The same expression appears in four modules** —
`app/topic/routes.py:103`, `app/feed/routes.py:522`,
`app/main/routes.py:144` and `app/api/alpha/utils/post.py:537` (the last with
`limit` in place of `page_length`).

**This round repairs the two that are covered**: `app/topic/routes.py` and
`app/feed/routes.py`, which is floored at 99 and has tests that will exercise
the change. The other two are at 22.8% and 14.9%; a change there would be
unverified, so they are registered as R1 with this round named as the
precedent.

### P2 — a crafted `community_id` is a 500

```python
if request.form.get('community_id', '') != '':
    community = Community.query.get_or_404(int(request.form.get('community_id')))
```

**Probe:** `PROBE t3 exception: ValueError invalid literal for int() with base 10: 'not-a-number'`.

The form posts a numeric id, so this is reachable only by a crafted request —
but the answer to a crafted request is a 400 or a 404, not a traceback.

**Fix:** `request.form.get('community_id', type=int)`, the idiom already used
at `app/feed/routes.py:459` and adopted by D726. A value that is not an integer
then reads as absent, which lands on the render the route already has for "no
community chosen".

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `app/main/routes.py:144`; `app/api/alpha/utils/post.py:537` | **The other two copies of P1's precedence bug.** Both modules are effectively uncovered (22.8% and 14.9%), so a change there cannot be verified by this round's tests. | Registered with P1 named as the precedent, so whichever round covers those modules repairs them with evidence rather than by copying. |
| R2 | `app/topic/routes.py:161`, `:164-168` | **The comments tab hands a 0-based page to a 1-based paginator.** `PROBE t4 page=0 -> page attr 1` and `page=1 -> page attr 1`: the first two pages render identically, and `prev_url` is separately suppressed for `page != 1`. | Making it coherent means choosing whether the comments tab is 0-based like the posts tab or 1-based like the paginator, and rewriting both urls. A behaviour decision, not a crash. |
| R3 | `app/topic/routes.py:289-291`; `app/templates/topic/_notification_toggle.html` | **The notification toggle mutates state on GET.** `PROBE t2 GET status: 200 subscriptions now: 1` — the route accepts `['GET', 'POST']` and subscribes on either, so any `<img src="/topic/3/notification">` toggles a logged-in reader's subscription. The template's `href` is the no-JS fallback; `hx-post` is the path a browser with JS takes. | Restricting to POST removes the toggle for no-JS readers and replacing it with a confirm form is a feature. Covered as behaviour, registered with the probe. |
| R4 | `app/topic/routes.py:56-63` | **The breadcrumb trail builds a namedtuple CLASS per entry and assigns attributes to it** rather than instantiating — **D731's shape, in a second module**. It works only because each iteration makes a fresh class. | Registered so it is not mistaken for a bug, and so that whoever fixes D731 finds this copy. |
| R5 | `app/topic/routes.py:180`, `:190` | **`user_filters_posts(current_user.id)` is called twice per request** — once into `content_filters` and again inside the render call, for the same value. | A duplicated query, not a defect. Registered with both lines quoted. |

## Success criteria

- Every function at `[]`/`[]` on the **full-suite** run, except arcs declared
  unreachable with a named cause and a proof.
- **`app/topic/routes.py` takes a floor** at the measured figure rounded down,
  which closes `app/topic` — 37 floors.
- Both repairs land with pins inverted; `git diff --numstat` names exactly
  `app/topic/routes.py` and `app/feed/routes.py`, the second for P1's one line.
- Suite green; floors checked with `&&`; a mutation pass over the module, per
  D602.
- Findings registered from **D776**; `tests/README.md` facts from **314**.
- `tests/test_zz_topic_probe.py` deleted before delivery.
