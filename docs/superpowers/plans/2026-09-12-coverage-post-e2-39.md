# Coverage sub-project 39 Implementation Plan — closing `app/shared/post.py`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take `edit_post`'s last 41 statements and 35 branch arcs to zero, closing `app/shared/post.py` — the module's only remaining gap.

**Architecture:** A new test file, `tests/test_shared_post_url.py`, drives `edit_post` through its two URL-type dispatches, its old-file teardown, and the poll/event arms of its tail. It reuses `_seed`, `_api_input` and `_web_form` from `tests/test_shared_post_edit.py`, `chdir_upload` and `make_upload` from `tests/test_shared_post_upload.py`, and the opengraph page-serving pattern already worked out for the federated twin in `tests/test_ap_update_post_tails.py`. A statement-scoped mutation pass verifies the result, then the floor rises and the register closes the module.

**Tech Stack:** pytest, respx (`http_mock`), SQLAlchemy 2.0.52, Pillow 12.3.0, Flask, Celery (eager), coverage.py with `--cov-branch`.

**Spec:** `docs/superpowers/specs/2026-09-12-coverage-post-e2-39-design.md`

---

## Global Constraints

These bind every task. They are the campaign's standing rules, and several were learned the expensive way.

**Running tests**

- **There is NO host Python with flask or pytest.** Everything runs through `./run_tests.sh`, which is `podman compose exec -T test-runner pytest "$@"` (`run_tests.sh:85`).
- **To run python inside the container:** `podman-compose -f compose.test.yaml exec -T test-runner python ...` (`tests/README.md:403-405`). **`run_tests.sh` has NO `--exec` flag** — passing one is rejected by pytest with exit 4.
- **Only the controller runs the full suite**, one pytest session at a time, in the foreground. An implementer runs its own file and the other post test files, never the whole suite.
- **`pytest` exits 1 on a session timeout and `run_tests.sh` propagates it.** A shell **pipeline** eats the status. Read `${PIPESTATUS[0]}`, or do not pipe.
- Before believing any failure, run `./run_tests.sh --down` and retry once. A timeout in this suite has been memory pressure, not the tests.

**Coverage**

- **Coverage takes the dotted module form:** `--cov=app.shared.post`. A path form (`--cov=app/shared/post.py`) collects nothing, writes no JSON, and **exits 0** — a silent green failure.
- **Write coverage JSON outside the repository.** `/app` is bind-mounted. Use `--cov-report=json:/tmp/<name>.json`.
- Read the module percentage from `summary.percent_covered`, never `percent_statements_covered`. `tests/check_coverage_floors.py:75` reads `entry['summary']['percent_covered']`.
- **Test counts come from pytest's own collection output**, never from a number written in this plan.

**Editing and citation**

- **Delete nothing the task did not create.** `git checkout -- app/` is permitted ONLY as a mutation-restore step.
- Every line number must be re-derived with numbered output before it is cited: `awk 'NR>=X && NR<=Y {printf "%d\t%s\n",NR,$0}' FILE`.
- **VERIFY EVERY CITATION MECHANICALLY AND PASTE THE PROOF** into the report beside the claim.
- **No duplicate test names.** Check with `grep -oE "^def (test_[a-z0-9_]+)" FILE | sort | uniq -d` — **the `0-9` matters**; the older `[a-z_]` form truncates at the first digit and silently reports nothing.
- **No ordered assertions over rows a query planner returned.** Compare sets.
- `app/shared/post.py` is **1193 lines** at the start of this round and no production change is planned. `wc -l app/shared/post.py` must still report 1193 at the end.

**Mutations**

- One at a time, **line-scoped `sed`**. Dry-run and read the line first, apply, run, restore, then assert an empty `git diff -- app/` and `wc -l app/shared/post.py` = 1193. **Restore before any point where you might stop and report.**
- **Scope the pass by the STATEMENT list, not the arc table.** Sub-project 38's statement-scoped pass found 10 holes an arc-scoped pass would have missed — every one of them a branchless statement (D470).
- **Compounds get one mutation per operand, and the compound list is derived mechanically** via `ast.walk` for `BoolOp` nodes, not by reading. Sub-project 38's controller hand-listed five compounds; the AST reported seven, and the hand list both included a non-compound and omitted two real ones.
- A crash kill is not a kill unless a viable non-crashing variant of the same fault also dies. An operator can be structurally void. **An arc being equivalent does not make every mutation of its line equivalent.** Non-failures are evidence.

**Commits**

- `git commit -F <file>`, never `-m`. Lowercase `type:` subject prefix, normal English prose in the body.
- Trailers, last two lines, in this order:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn
  ```

**Subagent contract**

- An implementer NEVER dispatches subagents — not helpers, and never a reviewer.
- Reviewer dispatches carry an explicit no-filesystem-modification line, scoped to the repository, explicitly permitting the review file.
- **During a mutation window, HEAD is the artifact, not the working tree.** A background scanner sampling the tree mid-mutation will file false positives; verify against `git show HEAD:` before believing one.

**The five false-witness mechanisms** (D451's four plus D469's fifth). Every test's docstring must name which branch it witnesses and why the assertion can only be produced by that branch.

1. Asserting on state something else sets unconditionally.
2. A fixture coincidence making two arms produce the same value.
3. Asserting emptiness with no same-mechanism positive control.
4. An input that takes the same path under both arms.
5. Two independent conditions exercised only in lockstep cannot detect a swap between them, however precise the assertions.

---

## The target

Measured at `544a7eb1`: module 788 statements / 41 missing, 438 branches / 35 missing, `percent_covered` 93.80097879282219. Floor is 93. `edit_post` is the module's only function with any gap.

**The 41 missing statements:**

```
405 407 409 438 439 440 441 447 448 449 451
613 614 620 621 622 623 624 625 626 627 628 629
631 632 633 634 635 636 637 638 639 640
644 645 646 647 648 649 650 661
```

**The 35 missing arcs:**

```
387->389  404->405  406->407  408->409  410->413  415->418
437->438  439->440  439->441  444->446  446->447  448->449  448->451
612->613  619->620  622->623  622->628  624->625  624->628
630->631  633->634  633->640  635->636  635->640
643->644  645->646  645->652  647->648  647->652
660->661
665->668  673->682  675->674  683->686  699->703
```

**Ownership, one row per task. Every arc and statement appears exactly once.**

| Task | Region | Statements | Arcs |
|---|---|---|---|
| 1 | `:403-411` dispatch on `post.url` | 405, 407, 409 | `404->405`, `406->407`, `408->409`, `410->413` |
| 2 | `:387` permission, `:415` scheduled | — | `387->389`, `415->418` |
| 3 | `:436-451` old-file teardown | 438-441, 447-449, 451 | `437->438`, `439->440`, `439->441`, `444->446`, `446->447`, `448->449`, `448->451` |
| 4 | `:612` event banner, `:660` video host | 613, 614, 661 | `612->613`, `660->661` |
| 5 | `:619-629` pixelfed arm | 620-629 | `619->620`, `622->623`, `622->628`, `624->625`, `624->628` |
| 6 | `:630-640` loops.video arm | 631-640 | `630->631`, `633->634`, `633->640`, `635->636`, `635->640` |
| 7 | `:641-650` generic opengraph arm | 644-650 | `643->644`, `645->646`, `645->652`, `647->648`, `647->652` |
| 8 | `:662-703` poll and event tail | — | `665->668`, `673->682`, `675->674`, `683->686`, `699->703` |
| 9 | Mutation pass | — | — |
| 10 | Measure, close, floor, register, README | — | — |

Totals: 3+0+8+3+10+10+7+0 = **41 statements**; 4+2+7+2+5+5+5+5 = **35 arcs**.

---

## File structure

| File | Responsibility |
|---|---|
| `tests/test_shared_post_url.py` | **Created by Task 1.** Every test this round writes. `edit_post`'s two URL-type dispatches, the old-file teardown, and the poll/event arms of its tail. |
| `tests/test_shared_post_edit.py` | Read-only. Source of `_seed`, `_api_input`, `_web_form`, `_Field`, `_OMIT`, `_make_admin`. |
| `tests/test_shared_post_upload.py` | Read-only. Source of `chdir_upload` and `make_upload`. |
| `tests/test_ap_update_post_tails.py` | Read-only reference. `_opengraph_page` (`:2266`) and `_unreadable_page` (`:2285`) are the worked pattern for serving an opengraph page under `http_mock`. |
| `app/shared/post.py` | **No production change is planned.** Touched only by Task 9's mutations, each restored. |
| `coverage_floors.ini` | Task 10 raises `app/shared/post.py`. |
| `tests/README.md` | Task 10 extends fact 229 and adds facts from 230. |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | Task 10 registers findings from D478 and moves the next-free marker. |

---

## The harness, established by reading before any task starts

Every task depends on these. They were verified against the source at `544a7eb1`; each task re-verifies the ones it uses and pastes the proof.

### 1. Entry is a direct call with `user=`

`edit_post` opens with `if not user:` on both branches (`:252` SRC_API, `:316` SRC_WEB), so passing `user=` skips `authorise_api_user` and `current_user` alike. No request context, no login, no token.

```python
edit_post(_api_input(url=URL), s.post, POST_TYPE_LINK, SRC_API,
          user=s.user, from_scratch=True)
```

Signature (`app/shared/post.py:250`):

```python
def edit_post(input, post: Post, type, src, user=None, auth=None, uploaded_file=None, from_scratch=False, hash=None):
```

### 2. `from_scratch=True` switches off the whole `:421-459` block

`:421` is `if not from_scratch:`. Passing `from_scratch=True` skips the notification cleanup, the poll-vote deletes, the teardown at `:435-451`, the tag clear and `:459`'s commit. **Tasks 1, 2 and 4-8 pass `from_scratch=True` to isolate their target. Task 3 is the only task that passes `from_scratch=False`,** because the teardown is its target.

### 3. `is_image_url` issues a real HEAD, and `edit_post` can call it twice

`is_image_url` (`app/utils.py:247`) calls `mime_type_using_head` (`:333`), which does `httpx_client.head(url, timeout=5)`. Two call sites in `edit_post`:

- `:410` — `is_image_url(post.url)`, reached only when `:403`'s `if post.url:` is true and `:404`/`:406`/`:408` are all false.
- `:601` — `is_image_url(url)`, reached only when `:565`'s `if url and (from_scratch or url_changed):` is true.

`mime_type_using_head` is decorated `@cache.memoize(timeout=10)`, but `TestConfig.CACHE_TYPE = 'NullCache'` (`tests/conftest.py:68`) makes the memoize inert, so each call really issues its HEAD.

respx's unmatched-request error is neither `httpx.HTTPError` nor `httpx.InvalidURL`, so it escapes `:345`'s handler and fails the test loudly rather than silently returning `''`. **Count your HEADs.** Registering a route you do not reach also fails, because `http_mock` is `assert_all_called=True` (`tests/conftest.py:342`).

### 4. **THE INVERTED CONVENTION — the round's central harness fact**

`tests/README.md` fact 229 point 3 records sub-project 38's rule: every full `edit_post()` call needs

```python
http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
```

and **no GET route**, because an image content type makes `is_image_url` return `True`, `:601` takes its branch, and `:601` is the only one of the four arms in the `if`/`elif`/`elif`/`else` chain that does NOT call `opengraph_parse` — so a registered GET would go uncalled and fail at teardown.

**Tasks 5, 6 and 7 need exactly the opposite** and must not follow fact 229 literally:

```python
http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'text/html'})
```

`'.html'` is not in `is_image_url`'s `common_image_extensions` list (`app/utils.py:248-249`), so `:273` returns `False`, `:601` is false, and control reaches `:619`/`:630`/`:641`. Those arms DO call `opengraph_parse`, so a GET route is then **required**, not optional.

**Tasks 1 and 4 need both conventions in the same file** — Task 4's `:612` test needs the image HEAD, its `:660` test needs the non-image HEAD. Keep them per-test; do not build a module-level fixture that picks one.

### 5. `opengraph_parse` needs a 200 **and** `text/html`

`opengraph_parse` (`app/utils.py:2998`) delegates to `parse_page` (`:3165`), which GETs the page via `get_request` and returns `False` unless the response is status 200 (`:3195-3196`) **and** carries `text/html` in its Content-Type (`:3198-3199`). Either missing makes `opengraph` falsy.

Copy the two helpers from `tests/test_ap_update_post_tails.py:2266` and `:2285` into the new file, re-cited to this round's line numbers:

```python
def _opengraph_page(http_mock, url, **tags):
    """Serve `url` as an HTML page carrying `tags` as opengraph <meta> elements.

    A keyword cannot carry a ':', so each tag is named with '_' and translated:
    `og_image_url=...` becomes `<meta property="og:image:url" ...>`.
    """
    meta = ''.join(f'<meta property="{name.replace("_", ":")}" content="{value}">'
                   for name, value in tags.items())
    http_mock.get(url).respond(200, headers={'Content-Type': 'text/html'},
                               text=f'<html><head>{meta}</head><body></body></html>')


def _unreadable_page(http_mock, url):
    """Serve `url` as a 404, so `parse_page` returns False at app/utils.py:3195-3196.

    False rather than None, and the difference matters to Task 9: forcing the
    leading `if opengraph` true reaches `False.get('og:image', '')` and raises
    AttributeError, which is a crash-kill and not an assertion-kill.
    """
    http_mock.get(url).respond(404)
```

**`parse_page` falls back to the `<title>` element** (`app/utils.py:3221-3224`), so a page with no meta tags at all still returns a non-empty dict if it has a title — and `<html><head></head><body></body></html>` has neither, which is what makes `_opengraph_page(http_mock, URL)` with no tags return `{}`, a falsy dict. Use `_unreadable_page` when you want `opengraph` falsy; it returns `False`, which is what the existing arcs were covered with.

### 6. `fixup_url` returns `(url, url)` for ordinary urls

`fixup_url` (`app/utils.py:3311`) sets `thumbnail_url = embed_url = url` and only diverges for YouTube domains and for peertube urls whose last 25 characters begin `/w/` (`:3330`). For every url this round uses, **`thumbnail_url == embed_url == url`**, so the GET route for `opengraph_parse` is registered on the submitted url itself.

**This is a false-witness hazard for Task 6.** `:640` sets `post.url = url` and `:652` sets `post.url = embed_url`; when they are equal, `post.url` cannot distinguish the loops arm from the generic arm. Task 6's discriminator must be something else — see its brief.

### 7. `url_to_thumbnail_file` writes to a relative path and decodes real bytes

Only `:646` (the generic arm) calls it. `app/utils.py:3065` builds `'app/static/media/posts/' + ...`, relative to the working directory, and `:3085` opens the downloaded bytes with `Image.open`. **Task 7 therefore needs `chdir_upload` even though it uploads nothing,** and its og:image GET must return genuine image bytes, not a placeholder string.

The `File` it returns is built with `file_path=` (`app/utils.py:3155`), **not** `source_url=`. The pixelfed and loops arms build theirs with `source_url=filename` (`:625`, `:637`). That difference is Task 7's discriminator against Tasks 5 and 6.

### 8. `make_image_sizes` executes rather than enqueues

Celery is eager (`tests/conftest.py:106`). `:614` and `:616` call `make_image_sizes` directly, so turning off `cache_remote_images_locally` does **not** help — that setting gates only the Event block's call in `app/activitypub/util.py`. The technique that works here is a **bodiless 404 on the source url**: `make_image_sizes_async` retries only when `'/api/v3/image_proxy'` is in the url, and proceeds only on status 200, so it returns having done nothing. This is recorded in `tests/test_shared_post_edit.py`'s module docstring at `:44-52`.

### 9. `S3_PUBLIC_URL` defaults to `''`, which makes `:446`'s third conjunct trivially true

`config.py:108` is `S3_PUBLIC_URL = os.environ.get('S3_PUBLIC_URL') or ''`. So `f'https://{current_app.config["S3_PUBLIC_URL"]}'` is the literal string `'https://'`, and **every** https url starts with it. Under the default config that conjunct can never be false.

**Task 3 must set `S3_PUBLIC_URL` to a real value** (`'s3.example.com'`) in the tests that exercise `:446`, or the conjunct's false-witness is impossible and Task 9's mutation of that operand is structurally void rather than genuinely killed.

### 10. `:387`'s second disjunct is structurally subsumed by its first

`Community.is_moderator` and `Community.is_owner` (`app/models.py:736-747`) both iterate `self.moderators()`:

```
716    def moderators(self):
717        return CommunityMember.query.filter((CommunityMember.community_id == self.id) &
718                                            (or_(
719                                                CommunityMember.is_owner,
720                                                CommunityMember.is_moderator
721                                            )
722                                            ).filter(CommunityMember.is_banned == False).all()

736    def is_moderator(self, user=None):
740            return any(moderator.user_id == user.id for moderator in self.moderators())

742    def is_owner(self, user=None):
747            return any(moderator.user_id == user.id and moderator.is_owner for moderator in self.moderators())
```

`moderators()` admits a row on `is_owner OR is_moderator`. So any row that satisfies `is_owner` is already in the list `is_moderator` searches, and `is_moderator(user)` tests the *weaker* predicate over the *same* list.

**`is_owner(user)` true therefore implies `is_moderator(user)` true, and `:387`'s second disjunct can never be the one that decides the compound.** Task 2 must verify this by construction — build a `CommunityMember` with `is_owner=True, is_moderator=False` and paste both predicates' return values — and Task 9 must classify that operand's mutation as **structurally void**, with this argument, rather than reporting it as a surviving mutant. Note the asymmetry: the reverse does not hold, so a moderator-only witness (`is_moderator=True, is_owner=False`) is achievable and is required.

The line count is misleading: `moderators()` at `:722` reads `.filter(CommunityMember.is_banned == False)`, so a banned owner is in neither list. That is not a route to separating the two disjuncts either.

### 11. `_seed` creates no `CommunityMember` rows

`_seed` (`tests/test_shared_post_edit.py:189`) makes an instance, a user, a community and a post, and nothing else. `make_community` (`tests/factories.py:124`) hardcodes `user_id=1`, but **`is_owner()` never reads `Community.user_id`** — it reads the `CommunityMember.is_owner` flag, per note 10. So for a bare `_seed()`, `moderators()` is empty and both predicates are already False.

`make_community_member(user, community, is_moderator=False)` (`tests/factories.py:384`) always sets `is_owner=False`; a test needing an owner row sets the attribute on the returned object and commits.

### 12. `scheduled_for` is unreachable from the API branch

`:281` is a bare `scheduled_for = None` on the SRC_API branch; only `:337` (`scheduled_for = input.scheduled_for.data`, SRC_WEB) can make it truthy. **Task 2's scheduled tests must use `_web_form`, not `_api_input`.** A scheduled test written against the API branch would take `:413`'s false arm and witness nothing — false-witness mechanism 4.

### 13. The federating tail already works under this harness

`:741`/`:743` call `task_selector` and Celery is eager, so federation runs inline. Every existing test in `tests/test_shared_post_edit.py` already goes through it: a `_seed()` community has no remote followers, so there is nothing to deliver, and anything that did escape would hit the session-scoped `block_outbound_http` router (`tests/conftest.py:262-332`), which `post_request` records as an `ActivityPubLog` failure rather than raising. No task needs to do anything about this.


---

### Task 1: Create the file, run the probes, and close the `post.url` dispatch

**Files:**
- Create: `tests/test_shared_post_url.py`
- Read: `app/shared/post.py:387-460`, `app/utils.py:247-330`, `tests/test_shared_post_edit.py:1-240`
- Read: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` (register search)

**Interfaces:**
- Consumes: `_seed`, `_api_input` from `tests/test_shared_post_edit.py`.
- Produces: the module and its docstring; `_opengraph_page` and `_unreadable_page` (Tasks 5-7 use them); the recorded probe answers every later task relies on.

**Target:** statements 405, 407, 409; arcs `404->405`, `406->407`, `408->409`, `410->413`.

- [ ] **Step 1: Search the register before probing anything**

Sub-project 36 spent a probe, a Major review finding, five documentation sites and a final-review correction re-deriving a fact D295 already recorded. Grep first, report what you found, then probe only what the register does not answer.

```bash
cd /home/blentz/git/pyfedi
for term in opengraph_parse url_to_thumbnail_file fixup_url is_image_url \
            mime_type_using_head calculate_cross_posts domain_from_url \
            is_video_hosting_site pixelfed loops.video; do
  echo "=== $term ==="
  grep -n "$term" docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md | head -6
done
```

Write what each hit says into your report BEFORE running any probe. If the register already answers a probe below, say so and skip it.

- [ ] **Step 2: Probe the inverted HEAD convention**

This is the round's central harness risk. Prove the inversion works before eight tasks depend on it.

```bash
cd /home/blentz/git/pyfedi
cat > /tmp/probe_head.py <<'PY'
import respx, httpx
from app.utils import is_image_url
with respx.mock(assert_all_called=True) as router:
    router.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'text/html'})
    print('text/html  ->', is_image_url('https://example.com/page.html'))
with respx.mock(assert_all_called=True) as router:
    router.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    print('image/png  ->', is_image_url('https://example.com/page.html'))
PY
```

Run it inside the container, inside an app context. The harness for a bare script is `tests/README.md:403-405`:

```bash
podman-compose -f compose.test.yaml exec -T test-runner python -c "
from app import create_app
from tests.conftest import TestConfig
app = create_app(TestConfig)
with app.app_context():
    exec(open('/tmp/probe_head.py').read())
"
```

**Expected:** `text/html -> False`, `image/png -> True`. Paste the real output. If `text/html` returns True, stop and report — the whole plan's Region B strategy rests on this.

- [ ] **Step 3: Re-verify the pixelfed scheme-less finding**

The spec registers a divergence rather than fixing it, on evidence gathered during scoping. Confirm that evidence still holds at this commit, because Task 5 pins the behaviour it describes.

```python
# same container-python harness as Step 2
from urllib.parse import urlparse
from app.utils import is_image_url

print(urlparse('pixelfed.uno/p/1'))
print('startswith https://pixelfed.uno/ ->', 'pixelfed.uno/p/1'.startswith('https://pixelfed.uno/'))
print('startswith pixelfed.uno         ->', 'pixelfed.uno/p/1'.startswith('pixelfed.uno'))
print('is_image_url (no route, real UnsupportedProtocol) ->', is_image_url('pixelfed.uno/p/1'))
```

**Expected:** `scheme='' netloc='' path='pixelfed.uno/p/1'`; `False`; `True`; `False`. The last one is the load-bearing part — `httpx.UnsupportedProtocol` is an `httpx.HTTPError`, so `mime_type_using_head`'s handler at `app/utils.py:345` catches it and returns `''`, sending `is_image_url` to extension sniffing, which finds no image extension. Paste the real output.

- [ ] **Step 4: Create the file with its docstring, imports and the two page helpers**

```python
"""`edit_post`'s two URL-type dispatches and the tail that closes the module.

SCOPE. Sub-project 39, the last 41 statements and 35 branch arcs in
`app/shared/post.py`. Every one of them is in `edit_post`; every other
function in the module is at zero. Three regions:

  - `:387-460` -- the permission compound, the type dispatch on `post.url`
    (the EXISTING url), the scheduled-post gate, and the old-file teardown.
  - `:565-661` -- the type dispatch on `url` (the NEWLY SUBMITTED one), which
    additionally builds thumbnails and `File` rows.
  - `:662-703` -- five arms of the poll and event tail whose lines all run.

ENTRY is a direct call. `edit_post` opens with `if not user:` on both
branches (`:252` SRC_API, `:316` SRC_WEB), so passing `user=` skips
`authorise_api_user` and `current_user` alike -- no request context, no
login, no token. Every test here does that.

`from_scratch=True` SWITCHES OFF `:421-459`. That block holds the
notification cleanup, the poll-vote deletes, the teardown at `:435-451`, the
tag clear and `:459`'s commit. Every test here passes `from_scratch=True`
EXCEPT the teardown tests, which are the ones aimed at that block.

THE HEAD REQUEST, AND WHY THIS FILE INVERTS THE UPLOAD FILE'S RULE.
`is_image_url` (app/utils.py:247) issues an httpx HEAD through
`mime_type_using_head`, and `edit_post` can call it twice -- `:410` on
`post.url`, `:601` on `url`. `tests/README.md` fact 229 point 3 records the
rule `tests/test_shared_post_upload.py` follows: a HEAD reporting
`image/png` and NO GET route, because an image content type makes `:601`
true and `:601` is the only one of the four arms in the
`if`/`elif`/`elif`/`else` chain at `:601`/`:619`/`:630`/`:641` that does not
call `opengraph_parse`.

THIS FILE NEEDS THE OPPOSITE on the arms it targets. A HEAD reporting
`text/html` makes `is_image_url` false (`.html` is not in
`common_image_extensions`, app/utils.py:248-249), control reaches `:619`
onward, `opengraph_parse` runs, and a GET route becomes REQUIRED rather than
forbidden. Both conventions appear in this file; they are chosen per test,
never by a module-level fixture.

`http_mock` is `assert_all_called=True` (tests/conftest.py:342), so a
registered route that is never reached fails the test at teardown, and
respx's unmatched-request error is neither `httpx.HTTPError` nor
`httpx.InvalidURL` -- it escapes `app/utils.py:345`'s handler rather than
being swallowed as `''`. Count your HEADs and your GETs.

`fixup_url` (app/utils.py:3311) RETURNS `(url, url)` for every url here. It
diverges only for YouTube domains and for peertube urls whose last 25
characters begin `/w/` (`:3330`). So `thumbnail_url == embed_url == url`,
which is why the GET for `opengraph_parse` is registered on the submitted
url itself -- and why `post.url` alone cannot tell `:640` (`post.url = url`)
apart from `:652` (`post.url = embed_url`). See TestLoopsArm for what does.
"""

from datetime import datetime, timedelta
from io import BytesIO

from PIL import Image

from app import db
from app.constants import (
    POST_STATUS_SCHEDULED, POST_TYPE_ARTICLE, POST_TYPE_EVENT,
    POST_TYPE_IMAGE, POST_TYPE_LINK, POST_TYPE_POLL, POST_TYPE_VIDEO,
    SRC_API, SRC_WEB,
)
from app.models import Domain, Event, File, Poll, PollChoice
from app.shared.post import edit_post
from tests.factories import make_community_member, make_user
from tests.test_shared_post_edit import _api_input, _make_admin, _seed, _web_form
from tests.test_shared_post_upload import chdir_upload  # noqa: F401


def _opengraph_page(http_mock, url, **tags):
    """Serve `url` as an HTML page carrying `tags` as opengraph <meta> elements.

    `:621`/`:632`/`:642`'s `opengraph_parse` (app/utils.py:2998-3007)
    delegates to `parse_page` (app/utils.py:3165-3225), which GETs the page
    and requires BOTH a 200 (app/utils.py:3195-3196) and 'text/html' in the
    Content-Type (app/utils.py:3198-3199) before it parses; either missing
    makes it return False. The header is load-bearing, not decoration.

    A keyword cannot carry a ':', so each tag is named with '_' and
    translated: `og_image_url=...` becomes `<meta property="og:image:url" ...>`.

    With NO tags this still returns a page `parse_page` reads successfully and
    finds nothing in -- an EMPTY dict, which is falsy. That is a different
    input from `_unreadable_page` below, which returns False, and the two are
    not interchangeable for the mutation pass.
    """
    meta = ''.join(f'<meta property="{name.replace("_", ":")}" content="{value}">'
                   for name, value in tags.items())
    http_mock.get(url).respond(200, headers={'Content-Type': 'text/html'},
                               text=f'<html><head>{meta}</head><body></body></html>')


def _unreadable_page(http_mock, url):
    """Serve `url` as a 404, so `parse_page` returns False at
    app/utils.py:3195-3196 and `:622`/`:633`/`:643`'s leading `if opengraph`
    is False.

    False rather than None, and the difference matters to Task 9's mutation
    pass: forcing the first conjunct true reaches `False.get('og:image', '')`
    and raises AttributeError, which is a crash-kill and not an
    assertion-kill.
    """
    http_mock.get(url).respond(404)
```

**This import list is the minimum Tasks 1-8 need as written. Add to it as a task requires; remove nothing another task uses.** `chdir_upload` carries `# noqa: F401` because it is a pytest FIXTURE -- importing the name is what registers it, and no line of code references it directly. Do not "clean up" that import.

Copy the two helper bodies verbatim from `tests/test_ap_update_post_tails.py:2266` and `:2285`, then re-derive every line number in their docstrings against this round's files before you commit — the originals cite `app/activitypub/util.py:3491-3494`, which is the federated twin, not this function.

- [ ] **Step 5: Write the four dispatch tests plus the positive control**

```python
class TestExistingUrlTypeDispatch:
    """`:403-411` -- the type dispatch on `post.url`, the url the post ALREADY
    has, tested before `:565` ever looks at the submitted one.

    Every test here passes `url=None` in the input, so `:565`'s
    `if url and (from_scratch or url_changed):` and `:660`'s `elif url and ...`
    are both false and the whole tail from `:565` is skipped. That leaves
    `:398`'s `post.type = type` and this block as the ONLY writers of
    `post.type`, which is what makes `post.type` a witness here at all.
    """

    def test_an_existing_pixelfed_url_retypes_the_post_as_image(self, db_session):
        """`:404` true -> `:405`. Arc 404->405, statement 405.

        No `http_mock`: `:404`'s match short-circuits the elif chain before
        `:408`'s `is_video_url` (pure) and `:410`'s `is_image_url` (which would
        issue a HEAD), so this path makes no outbound request at all.

        `type=POST_TYPE_ARTICLE` is passed so `:398` writes ARTICLE and only
        `:405` can produce IMAGE.
        """
        s = _seed(url='https://pixelfed.social/p/someone/1')

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_IMAGE

    def test_an_existing_loops_url_retypes_the_post_as_video(self, db_session):
        """`:404` false, `:406` true -> `:407`. Arc 406->407, statement 407."""
        s = _seed(url='https://loops.video/v/abc123')

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_VIDEO

    def test_an_existing_video_extension_retypes_the_post_as_video(self, db_session):
        """`:408`'s `is_video_url(post.url)` true -> `:409`. Arc 408->409,
        statement 409.

        Still no `http_mock`: `is_video_url` (app/utils.py:294) is pure
        urlparse plus an extension test and issues no request. The url is
        deliberately NOT a pixelfed or loops host, so `:404` and `:406` are
        both false and `:408` is the line that decides.
        """
        s = _seed(url='https://example.com/clip.mp4')

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_VIDEO

    def test_an_existing_image_url_retypes_the_post_as_image(self, db_session, http_mock):
        """`:410`'s `is_image_url(post.url)` true -> `:411`.

        THE POSITIVE CONTROL for the test below, and the reason both exist.
        The false-arm test can only assert that `post.type` is still what
        `:398` wrote, which is also what a broken `:410` that never ran would
        leave -- false-witness mechanism 1. This test differs from it in ONE
        byte of the HEAD's Content-Type and produces a different type, so the
        pair proves `:410` is actually deciding.
        """
        http_mock.head('https://example.com/page.html').respond(
            200, headers={'Content-Type': 'image/png'})
        s = _seed(url='https://example.com/page.html')

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_IMAGE

    def test_an_existing_url_that_is_neither_video_nor_image_leaves_the_type_alone(
            self, db_session, http_mock):
        """`:410` false -> `:413`. Arc 410->413.

        Identical to the test above except the HEAD reports `text/html`
        instead of `image/png`, so `is_image_url` returns False at
        app/utils.py:273 (`.html` is not in `common_image_extensions`) and no
        arm of `:403-411` fires.
        """
        http_mock.head('https://example.com/page.html').respond(
            200, headers={'Content-Type': 'text/html'})
        s = _seed(url='https://example.com/page.html')

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_ARTICLE
```

- [ ] **Step 6: Run the file**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_post_url.py -v
echo "exit=$?"
```

Expected: 5 passed. Report the collected count from pytest's own output, not from this plan.

- [ ] **Step 7: Confirm the four arcs and three statements closed**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_post_url.py \
  --cov=app.shared.post --cov-branch \
  --cov-report=json:/tmp/t1.json -q
echo "exit=$?"
```

Note the **dotted** `--cov=app.shared.post`. A path form collects nothing, writes no JSON and exits 0.

Then read the JSON inside the container and check the four arcs are absent from `missing_branches` and 405/407/409 absent from `missing_lines`. **Check each arc as a PAIR**; inspecting a global min and max proves nothing about the arcs in between.

- [ ] **Step 8: Verify no duplicate test names, then commit**

```bash
cd /home/blentz/git/pyfedi
grep -oE "^    def (test_[a-z0-9_]+)" tests/test_shared_post_url.py | sort | uniq -d
git status --porcelain
```

The `grep` must print nothing. The `0-9` in the character class matters: the older `[a-z_]` form truncates at the first digit and silently reports no duplicates. `git status` must show only the new test file — no change to `app/`.

```bash
git add tests/test_shared_post_url.py
git commit -F <message-file>
```

Subject: `test: cover edit_post's type dispatch on the post's existing url`

---

### Task 2: The permission compound and the scheduled-post gate

**Files:**
- Modify: `tests/test_shared_post_url.py`
- Read: `app/shared/post.py:386-418`, `app/models.py:716-747`, `tests/factories.py:384-394`

**Interfaces:**
- Consumes: `_seed`, `_api_input`, `_web_form`, `_make_admin`; `make_community_member`, `make_user`.
- Produces: the verified subsumption argument for `:387`'s second disjunct, which Task 9 needs.

**Target:** arcs `387->389` and `415->418`. No statements — both are false arms.

- [ ] **Step 1: Verify the subsumption by construction before writing anything**

`:387` is `if post.community.is_moderator(user) or post.community.is_owner(user) or user.is_admin():` — three disjuncts, scored by coverage.py as one arc pair. The plan's harness note 10 argues the SECOND can never decide the compound. Prove it, do not take it on trust:

```python
# container-python harness, inside an app context and a db session
from tests.test_shared_post_edit import _seed
from tests.factories import make_community_member
from app import db

s = _seed()
m = make_community_member(s.user, s.community, is_moderator=False)
m.is_owner = True
db.session.commit()
print('is_owner    ->', s.community.is_owner(s.user))
print('is_moderator->', s.community.is_moderator(s.user))
```

**Expected:** both `True`. `moderators()` (`app/models.py:716-722`) admits a row on `is_owner OR is_moderator`, and `is_moderator` (`:740`) tests only `user_id` over that same list. Paste the output. If `is_moderator` comes back `False`, the subsumption is wrong, the plan's note 10 is wrong, and Task 9 must treat the second operand as a live mutation — say so loudly in your report.

- [ ] **Step 2: Write the permission tests**

Four tests. The all-false one closes the arc; the other three are the per-operand witnesses Task 9's compound rule requires, and they must exercise the operands ALONE — two conditions moved only in lockstep cannot detect a swap between them, however precise the assertions (false-witness mechanism 5).

```python
class TestStickyPermission:
    """`:387`'s three-disjunct compound and the `post.sticky` write it guards.

    THE WITNESS IS A SEEDED `True`. `:388` is
    `post.sticky = False if src == SRC_API else input.sticky.data`, so under
    SRC_API the permitted path always writes False -- and False is also what a
    freshly made post already carries. Asserting `post.sticky is False` on a
    default post witnesses nothing at all (false-witness mechanism 1). Every
    test here seeds `post.sticky = True` first, so the permitted arm CHANGES it
    and the refused arm LEAVES it.

    ONLY TWO OF THE THREE OPERANDS CAN BE WITNESSED ALONE. `moderators()`
    (app/models.py:716-722) admits a `CommunityMember` row on
    `is_owner OR is_moderator`, and `is_moderator(user)` (`:740`) then tests
    only `user_id` over that list -- so a row with `is_owner=True,
    is_moderator=False` makes BOTH predicates true, and the second disjunct
    can never be the one that decides. Task 2 verified this by construction.
    `test_an_owner_may_set_sticky` documents the subsumption rather than
    isolating the operand, because isolating it is impossible.
    """

    def _seed_sticky(self):
        s = _seed()
        s.post.sticky = True
        db.session.commit()
        return s

    def test_a_plain_member_may_not_set_sticky(self, db_session):
        """All three disjuncts false -> `:389`. Arc 387->389.

        The user is a second, unrelated account: `_seed()`'s own user would
        still be a stranger here (a bare `_seed` creates no `CommunityMember`
        rows at all, so `moderators()` is empty), but building the outsider
        explicitly keeps the test honest if `_seed` ever gains one.
        """
        s = self._seed_sticky()
        outsider = make_user(s.instance, 'outsider', local=True)

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=outsider, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.sticky is True

    def test_a_moderator_may_set_sticky(self, db_session):
        """First disjunct alone: `is_moderator=True, is_owner=False`, so
        `is_moderator(user)` is true and `is_owner(user)` is false. This is the
        one operand that CAN be isolated."""
        s = self._seed_sticky()
        make_community_member(s.user, s.community, is_moderator=True)

        assert s.community.is_owner(s.user) is False

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.sticky is False

    def test_an_owner_may_set_sticky(self, db_session):
        """Second disjunct, WHICH CANNOT BE ISOLATED -- an owner row satisfies
        `is_moderator` too. The inline assertion records that, so a reader does
        not mistake this for an isolating witness."""
        s = self._seed_sticky()
        member = make_community_member(s.user, s.community, is_moderator=False)
        member.is_owner = True
        db.session.commit()

        assert s.community.is_moderator(s.user) is True  # the subsumption

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.sticky is False

    def test_an_admin_may_set_sticky(self, db_session):
        """Third disjunct alone: no `CommunityMember` row exists, so the first
        two are false and `user.is_admin()` is the only thing that can be
        true."""
        s = self._seed_sticky()
        _make_admin(s.user)

        assert s.community.is_moderator(s.user) is False
        assert s.community.is_owner(s.user) is False

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.sticky is False
```

`_make_admin` needs a `Role` row whose id IS `ROLE_ADMIN`, because `Site.admins()` joins on `user_role.c.role_id == ROLE_ADMIN` — that is D295, and `tests/test_shared_post_edit.py:219-240` already implements it. Import it; do not re-implement it. If `user.is_admin()` turns out to read something other than that role, report what it reads and adjust — the assertion above must hold before the test is worth anything.

- [ ] **Step 3: Write the scheduled-post tests**

```python
class TestScheduledGate:
    """`:413-416` -- the scheduled-post gate.

    MUST USE THE WEB BRANCH. `:281` is a bare `scheduled_for = None` on the
    SRC_API branch, so `:413`'s `if scheduled_for:` can never be true through
    `_api_input`; only `:337`'s `scheduled_for = input.scheduled_for.data`
    (SRC_WEB) can make it truthy. A scheduled test written against the API
    branch would take `:413`'s false arm and witness nothing -- false-witness
    mechanism 4.

    `:414` reads `post.timezone`, which `:401` has just written from
    `input.timezone.data`; `_web_form` defaults it to 'UTC'.
    """

    def test_a_future_schedule_marks_the_post_scheduled(self, db_session, http_mock):
        """`:415` true -> `:416`. THE POSITIVE CONTROL for the test below:
        without it, `post.status` merely still holding its seeded value proves
        nothing about whether `:415` ran."""
        http_mock.head(url__regex=r'.*').respond(
            200, headers={'Content-Type': 'text/html'})
        s = _seed()
        future = datetime.utcnow() + timedelta(days=2)

        edit_post(_web_form(scheduled_for=future), s.post, POST_TYPE_LINK,
                  SRC_WEB, user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.status == POST_STATUS_SCHEDULED

    def test_a_schedule_already_in_the_past_does_not_mark_the_post_scheduled(
            self, db_session, http_mock):
        """`:415` false -> `:418`. Arc 415->418.

        Differs from the control above in the DATE alone.
        """
        http_mock.head(url__regex=r'.*').respond(
            200, headers={'Content-Type': 'text/html'})
        s = _seed()
        past = datetime.utcnow() - timedelta(days=2)

        edit_post(_web_form(scheduled_for=past), s.post, POST_TYPE_LINK,
                  SRC_WEB, user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.status != POST_STATUS_SCHEDULED
```

**The web form submits a url.** `_web_form`'s defaults include `link_url='https://example.com/page'` (`tests/test_shared_post_edit.py:152`), so `url` is truthy, `:565` opens with `from_scratch=True`, and `:601`'s `is_image_url` issues a HEAD. That is why these two tests carry `http_mock` while Task 1's do not. **Determine which of `link_url` and `video_url` `:318-330` actually reads for `POST_TYPE_LINK` by reading the SRC_WEB branch, and register the route for the url it really uses** — do not assume. With `text/html` the call falls through to `:641`'s generic arm, which calls `opengraph_parse` and therefore needs a GET too; register it with `_unreadable_page`, or use a HEAD of `image/png` and no GET (fact 229's convention) if the simpler shape suits. Either is fine here, since this class does not target Region B. Say which you chose and why.

- [ ] **Step 4: Run and confirm the two arcs closed**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_post_url.py -v
echo "exit=$?"
./run_tests.sh tests/test_shared_post_url.py \
  --cov=app.shared.post --cov-branch --cov-report=json:/tmp/t2.json -q
echo "exit=$?"
```

`387->389` and `415->418` must both be absent from `missing_branches`.

- [ ] **Step 5: Commit**

```bash
cd /home/blentz/git/pyfedi
grep -oE "^    def (test_[a-z0-9_]+)" tests/test_shared_post_url.py | sort | uniq -d
git status --porcelain
git add tests/test_shared_post_url.py
git commit -F <message-file>
```

Subject: `test: cover edit_post's sticky permission compound and scheduled gate`

The body must record the subsumption finding from Step 1 in prose — it is the round's first register candidate.

---

### Task 3: The old-file teardown, including the S3 deletion compound

**Files:**
- Modify: `tests/test_shared_post_url.py`
- Read: `app/shared/post.py:421-460`, `app/utils.py:1561-1596`, `app/utils.py:4317-4319`, `app/shared/tasks/maintenance.py`

**Interfaces:**
- Consumes: `_seed`, `_api_input`, `chdir_upload`.
- Produces: nothing later tasks depend on.

**Target:** statements 438, 439, 440, 441, 447, 448, 449, 451; arcs `437->438`, `439->440`, `439->441`, `444->446`, `446->447`, `448->449`, `448->451`.

**This is the only task that passes `from_scratch=False`.** The whole block lives under `:421`'s `if not from_scratch:`.

- [ ] **Step 1: Read the block and the two collaborators it reaches**

```bash
cd /home/blentz/git/pyfedi
awk 'NR>=434 && NR<=452 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
grep -n "def delete_from_disk" app/models.py
awk 'NR>=1561 && NR<=1596 {printf "%d\t%s\n",NR,$0}' app/utils.py
grep -n "def delete_from_s3" app/shared/tasks/maintenance.py
```

Paste `File.delete_from_disk`'s body into your report. The `439->440` test's witness depends on what it actually removes.

- [ ] **Step 2: Write the image-teardown tests**

```python
class TestOldImageTeardown:
    """`:435-441` -- removing the post's old `File` when the url changes.

    `from_scratch=False` on every test here: the block lives under `:421`'s
    `if not from_scratch:`. The gate above it, `:435`'s
    `if url != post.url or uploaded_file:`, is opened by submitting `url=None`
    against a post that HAS a url.
    """

    def test_a_url_change_deletes_the_old_image_from_disk(self, db_session, chdir_upload):
        """`:437` true -> `:438`, `:439` true -> `:440`, then `:441`.
        Arcs 437->438 and 439->440; statements 438, 439, 440, 441.

        `chdir_upload` is here so the `File`'s `file_path` is a real path under
        pytest's `tmp_path` rather than anywhere in the repository, and so the
        witness can be the FILE ITSELF disappearing. Asserting only that
        `post.image_id` is None afterward would witness `:441`, which runs on
        BOTH arms of `:439` -- false-witness mechanism 1.
        """
        s = _seed(url='https://example.com/clip.mp4')
        target = chdir_upload / 'app' / 'static' / 'media' / 'posts' / 'ab' / 'cd'
        target.mkdir(parents=True)
        on_disk = target / 'old.png'
        Image.new('RGB', (8, 8), (1, 2, 3)).save(on_disk, format='PNG')
        old = File(source_url='https://example.com/old.png',
                   file_path=str(on_disk.relative_to(chdir_upload)))
        db.session.add(old)
        db.session.commit()
        s.post.image_id = old.id
        db.session.commit()
        assert on_disk.exists()

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=False)

        db.session.refresh(s.post)
        assert not on_disk.exists()
        assert s.post.image_id is None

    def test_a_dangling_image_id_is_cleared_without_a_delete(self, db_session, chdir_upload):
        """`:437` true, `:439` FALSE -> `:441`. Arc 439->441.

        `post.image_id` points at a row that does not exist, so
        `File.query.get` returns None and `:440` is skipped.

        THE POSITIVE CONTROL is the test above, which uses the same mechanism
        -- a real file under `chdir_upload` -- and shows it being removed.
        Without that pair, "nothing was deleted" is indistinguishable from a
        broken fixture that never created anything (false-witness mechanism 3).
        The unrelated file written here is the in-test half of the same
        control: it must SURVIVE.
        """
        s = _seed(url='https://example.com/clip.mp4')
        target = chdir_upload / 'app' / 'static' / 'media' / 'posts' / 'ef' / 'gh'
        target.mkdir(parents=True)
        bystander = target / 'unrelated.png'
        Image.new('RGB', (8, 8), (4, 5, 6)).save(bystander, format='PNG')
        s.post.image_id = 999999
        db.session.commit()

        edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=False)

        db.session.refresh(s.post)
        assert s.post.image_id is None
        assert bystander.exists()
        assert db.session.get(File, 999999) is None
```

Confirm `999999` is genuinely absent before relying on it — `db_session` truncates and resets sequences (`tests/conftest.py:131-132`), so nothing should reach that id, but assert it rather than assume it.

- [ ] **Step 3: Write the S3-deletion tests**

`:446` is a **three-conjunct compound** scored as one arc pair:

```
446   if post.type == POST_TYPE_VIDEO and store_files_in_s3() and post.url.startswith(f'https://{current_app.config["S3_PUBLIC_URL"]}'):
```

Two things make it awkward, and both must be handled explicitly.

**`S3_PUBLIC_URL` defaults to `''`** (`config.py:108`), so the third conjunct's literal is `'https://'` and **every** https url satisfies it. Under the default config that operand cannot be falsified, and Task 9's mutation of it would be structurally void rather than killed. Every test in this class sets a real value.

**`:447` is a function-body import** — `from app.shared.tasks.maintenance import delete_from_s3` re-executes on every call, so patching `post_module.delete_from_s3` has no effect. Patch the attribute **on the maintenance module**.

```python
class _RecordingDeleteFromS3:
    """Stand-in for the Celery task at `:447`, recording both call shapes.

    `:449` calls it directly and `:451` calls `.delay(...)`, so the double
    needs both, kept in SEPARATE lists -- a single combined list could not
    tell the two arcs apart, which is the whole point of the pair.
    """

    def __init__(self):
        self.direct = []
        self.delayed = []

    def __call__(self, urls):
        self.direct.append(urls)

    def delay(self, urls):
        self.delayed.append(urls)


S3_HOST = 's3.example.com'
S3_VIDEO_URL = f'https://{S3_HOST}/posts/ab/cd/vid.mp4'


class TestS3VideoTeardown:
    """`:442-451` -- deleting the old video from S3 when the url changes.

    THE THIRD CONJUNCT IS UNFALSIFIABLE UNDER THE DEFAULT CONFIG.
    `config.py:108` makes `S3_PUBLIC_URL` `''`, so
    `f'https://{S3_PUBLIC_URL}'` is `'https://'` and every https url starts
    with it. Every test here sets a real host, so the operand can be both
    satisfied and refused.

    `:447`'s import is INSIDE the function body and re-runs on every call, so
    `app.shared.tasks.maintenance.delete_from_s3` is the name to patch;
    patching `app.shared.post.delete_from_s3` would be rebound and ignored.
    """

    def _configure(self, app, monkeypatch, recorder, s3_on=True):
        monkeypatch.setitem(app.config, 'S3_PUBLIC_URL', S3_HOST)
        monkeypatch.setitem(app.config, 'S3_ACCESS_KEY', 'k' if s3_on else '')
        monkeypatch.setitem(app.config, 'S3_ACCESS_SECRET', 's' if s3_on else '')
        monkeypatch.setitem(app.config, 'S3_ENDPOINT', 'e' if s3_on else '')
        monkeypatch.setattr('app.shared.tasks.maintenance.delete_from_s3',
                            recorder, raising=True)

    def test_a_video_on_s3_is_queued_for_deletion(self, db_session, app, monkeypatch):
        """All three conjuncts true, `:448` false -> `:451`.
        Arcs 444->445 (already covered), 446->447 and 448->451;
        statements 447, 448, 451.
        """
        rec = _RecordingDeleteFromS3()
        self._configure(app, monkeypatch, rec)
        s = _seed(url=S3_VIDEO_URL)
        assert app.debug is False

        edit_post(_api_input(), s.post, POST_TYPE_VIDEO, SRC_API,
                  user=s.user, from_scratch=False)

        assert rec.delayed == [[S3_VIDEO_URL]]
        assert rec.direct == []

    def test_debug_mode_deletes_from_s3_inline(self, db_session, app, monkeypatch):
        """`:448` true -> `:449`. Arc 448->449, statement 449.

        Differs from the test above in `DEBUG` alone. `app.debug` reads
        `config['DEBUG']`, so setting the config item moves the branch.
        """
        rec = _RecordingDeleteFromS3()
        self._configure(app, monkeypatch, rec)
        monkeypatch.setitem(app.config, 'DEBUG', True)
        s = _seed(url=S3_VIDEO_URL)

        edit_post(_api_input(), s.post, POST_TYPE_VIDEO, SRC_API,
                  user=s.user, from_scratch=False)

        assert rec.direct == [[S3_VIDEO_URL]]
        assert rec.delayed == []

    def test_a_non_video_post_on_s3_is_not_deleted(self, db_session, app, monkeypatch, http_mock):
        """FIRST conjunct false, the other two true. Witnesses the operand
        alone.

        The url is an image rather than a video, so `:408` is false and `:410`
        issues a HEAD -- hence `http_mock`. Under `image/png` `:411` writes
        POST_TYPE_IMAGE, which is also what makes the first conjunct false.
        """
        rec = _RecordingDeleteFromS3()
        self._configure(app, monkeypatch, rec)
        image_url = f'https://{S3_HOST}/posts/ab/cd/pic.png'
        http_mock.head(image_url).respond(200, headers={'Content-Type': 'image/png'})
        s = _seed(url=image_url)

        edit_post(_api_input(), s.post, POST_TYPE_IMAGE, SRC_API,
                  user=s.user, from_scratch=False)

        assert rec.direct == [] and rec.delayed == []

    def test_a_video_is_not_deleted_when_s3_is_not_configured(self, db_session, app, monkeypatch):
        """SECOND conjunct false, the other two true. `store_files_in_s3()`
        (app/utils.py:4317-4319) is false with the keys empty, while
        `S3_PUBLIC_URL` stays set so the third conjunct would still match."""
        rec = _RecordingDeleteFromS3()
        self._configure(app, monkeypatch, rec, s3_on=False)
        s = _seed(url=S3_VIDEO_URL)

        edit_post(_api_input(), s.post, POST_TYPE_VIDEO, SRC_API,
                  user=s.user, from_scratch=False)

        assert rec.direct == [] and rec.delayed == []

    def test_a_video_hosted_elsewhere_is_not_deleted_from_s3(self, db_session, app, monkeypatch):
        """THIRD conjunct false, the other two true. The video is real and S3
        is configured; the url simply is not on the S3 host."""
        rec = _RecordingDeleteFromS3()
        self._configure(app, monkeypatch, rec)
        s = _seed(url='https://elsewhere.example.com/posts/vid.mp4')

        edit_post(_api_input(), s.post, POST_TYPE_VIDEO, SRC_API,
                  user=s.user, from_scratch=False)

        assert rec.direct == [] and rec.delayed == []

    def test_a_hostless_url_skips_the_domain_decrement(self, db_session, app, monkeypatch):
        """`:444` false -> `:446`. Arc 444->446.

        `domain_from_url` returns None when `parsed_url.hostname` is falsy
        (app/utils.py:1583/1595-1596), which a relative url produces.

        THIS ARC CANNOT SHARE AN INPUT WITH `446->447`: the S3 conjunct needs
        a url starting `https://<host>`, which necessarily HAS a hostname and
        therefore makes `:444` true. The two arcs need different urls, and
        that is why they are different tests.
        """
        rec = _RecordingDeleteFromS3()
        self._configure(app, monkeypatch, rec, s3_on=False)
        s = _seed(url='/relative/clip.mp4')

        edit_post(_api_input(), s.post, POST_TYPE_VIDEO, SRC_API,
                  user=s.user, from_scratch=False)

        assert rec.direct == [] and rec.delayed == []
        db.session.refresh(s.post)
```

The last test's assertion set is thin because `:446` short-circuits on its second conjunct. Add a positive assertion that no `Domain` row was created or decremented for the relative url — read `domain_from_url`'s create path (`app/utils.py:1590-1593`) and assert on `Domain.query.count()` before and after, so the test witnesses `:444`'s false arm rather than merely not crashing.

- [ ] **Step 4: Run and confirm all seven arcs and eight statements closed**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_post_url.py -v
echo "exit=$?"
./run_tests.sh tests/test_shared_post_url.py \
  --cov=app.shared.post --cov-branch --cov-report=json:/tmp/t3.json -q
echo "exit=$?"
```

Check all seven arcs as pairs and all eight statements individually.

- [ ] **Step 5: Confirm nothing was written into the repository**

`chdir_upload` redirects relative writes, but this task also drives `delete_from_disk`. Prove the repository's media tree is untouched:

```bash
cd /home/blentz/git/pyfedi
find app/static/media -type f | wc -l
git status --porcelain
```

Record the count. It must match what it was before the task ran, and `git status` must show only the test file.

- [ ] **Step 6: Commit**

```bash
cd /home/blentz/git/pyfedi
grep -oE "^    def (test_[a-z0-9_]+)" tests/test_shared_post_url.py | sort | uniq -d
git add tests/test_shared_post_url.py
git commit -F <message-file>
```

Subject: `test: cover edit_post's old-file teardown and its s3 video deletion`

---

### Task 4: The event banner arm and the video-hosting-site arm

**Files:**
- Modify: `tests/test_shared_post_url.py`
- Read: `app/shared/post.py:600-618`, `:659-661`, `app/utils.py:316-329`

**Interfaces:**
- Consumes: `_seed`, `_api_input`.
- Produces: `_RecordingMakeImageSizes`, which Task 5 and Task 6 may reuse.

**Target:** statements 613, 614, 661; arcs `612->613`, `660->661`.

**These two tests need OPPOSITE HEAD conventions**, which is why they are one task: putting them side by side makes the inversion impossible to miss.

- [ ] **Step 1: Confirm `make_image_sizes` is patchable at module level**

```bash
cd /home/blentz/git/pyfedi
grep -n "make_image_sizes" app/shared/post.py | head
```

It must appear in an `import` at the top of `app/shared/post.py`, not inside `edit_post`'s body. If it is a function-body import (as `:447`'s `delete_from_s3` is), patch it on its defining module instead and say so.

- [ ] **Step 2: Write the event-banner tests**

The distinction between `:614` and `:616` is the ARGUMENTS, not the effect — `make_image_sizes(post.image_id, 170, 2000, ...)` against `make_image_sizes(post.image_id, 512, 1200, ...)`. Recording the call is the only way to witness it, and it is what makes Task 9's mutations of those literals killable.

```python
class _RecordingMakeImageSizes:
    """Records `make_image_sizes` calls instead of running the real pipeline.

    `:614` and `:616` differ ONLY in their size arguments (170/2000 against
    512/1200), so an assertion on the resulting `File` cannot tell them apart
    -- false-witness mechanism 1, since both arms set `post.image_id` the same
    way at `:608`. The arguments are the witness.

    This also removes the need for the bodiless-404 technique
    (tests/test_shared_post_edit.py:44-52): under eager Celery the real
    `make_image_sizes` EXECUTES, and would otherwise issue a GET this test has
    no reason to serve.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


PAGE_URL = 'https://example.com/thing'


class TestImageArmEventBanner:
    """`:601`'s true arm, and `:612`'s fork inside it.

    THIS CLASS FOLLOWS FACT 229's CONVENTION, not this file's inverted one: a
    HEAD reporting `image/png` and NO GET route. `:601` true is the whole
    point, and `:601` is the one arm of the four that never calls
    `opengraph_parse`, so a GET route here would go unreached and fail
    `http_mock`'s `assert_all_called=True` at teardown.
    """

    def test_an_event_with_an_image_url_keeps_no_url_and_gets_a_banner(
            self, db_session, http_mock, monkeypatch):
        """`:612` true -> `:613`, `:614`. Arc 612->613; statements 613, 614.

        Two independent witnesses, because either alone is weak: `post.url`
        ends up None (only `:613` writes that; `:618` writes the url), and
        `make_image_sizes` is called with the banner sizes 170/2000 (only
        `:614` passes those; `:616` passes 512/1200).
        """
        rec = _RecordingMakeImageSizes()
        monkeypatch.setattr('app.shared.post.make_image_sizes', rec)
        http_mock.head(PAGE_URL).respond(200, headers={'Content-Type': 'image/png'})
        s = _seed()

        edit_post(_api_input(url=PAGE_URL), s.post, POST_TYPE_EVENT, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.url is None
        assert s.post.image_id is not None
        assert len(rec.calls) == 1
        assert rec.calls[0][0][1:3] == (170, 2000)

    def test_a_non_event_with_an_image_url_keeps_the_url_and_gets_a_thumbnail(
            self, db_session, http_mock, monkeypatch):
        """`:612` false -> `:616`, `:617`, `:618`. THE POSITIVE CONTROL for the
        test above: it differs in the post TYPE alone and produces the opposite
        value on both witnesses."""
        rec = _RecordingMakeImageSizes()
        monkeypatch.setattr('app.shared.post.make_image_sizes', rec)
        http_mock.head(PAGE_URL).respond(200, headers={'Content-Type': 'image/png'})
        s = _seed()

        edit_post(_api_input(url=PAGE_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.url == PAGE_URL
        assert s.post.type == POST_TYPE_IMAGE
        assert rec.calls[0][0][1:3] == (512, 1200)
```

If `monkeypatch.setattr('app.shared.post.make_image_sizes', rec)` raises because the name is absent, Step 1 told you where it really lives — patch there and record the correction.

- [ ] **Step 3: Write the video-hosting-site test**

```python
class TestVideoHostingSiteArm:
    """`:660-661` -- the `elif` after `:565`, reached only when `:565` is FALSE
    while `url` is still truthy.

    `:565` is `if url and (from_scratch or url_changed):`, so closing it with a
    url present means `from_scratch=False` AND `url_changed` false. `:435` sets
    `url_changed` only when `url != post.url or uploaded_file`, so the post is
    seeded with the SAME url that is submitted, and no file is uploaded.

    THIS CLASS USES THE INVERTED CONVENTION -- a HEAD reporting a non-image
    type -- and for a reason the previous class does not share: `:403`'s
    `if post.url:` is true here (the post has a url), so `:410`'s
    `is_image_url(post.url)` runs and issues a HEAD. `text/html` keeps
    `:403-411` from retyping the post, leaving `:661` the only writer of
    `post.type`.
    """

    def test_a_youtube_url_that_did_not_change_still_retypes_the_post_as_video(
            self, db_session, http_mock):
        """`:660` true -> `:661`. Arc 660->661, statement 661.

        `type=POST_TYPE_ARTICLE` is submitted, so `:398` writes ARTICLE and
        only `:661` can produce VIDEO. `is_video_hosting_site`
        (app/utils.py:316-329) matches on the 'https://youtube.com' prefix and
        issues no request of its own.
        """
        youtube = 'https://youtube.com/watch?v=abc123'
        http_mock.head(youtube).respond(200, headers={'Content-Type': 'text/html'})
        s = _seed(url=youtube)

        edit_post(_api_input(url=youtube), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=False)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_VIDEO

    def test_a_plain_url_that_did_not_change_leaves_the_type_alone(
            self, db_session, http_mock):
        """`:660` false -> `:663`. THE POSITIVE CONTROL: identical except the
        host is not a video-hosting site, so the post keeps the ARTICLE type
        `:398` wrote."""
        plain = 'https://example.com/watch-this'
        http_mock.head(plain).respond(200, headers={'Content-Type': 'text/html'})
        s = _seed(url=plain)

        edit_post(_api_input(url=plain), s.post, POST_TYPE_ARTICLE, SRC_API,
                  user=s.user, from_scratch=False)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_ARTICLE
```

**`is_video_hosting_site` also matches `'videos/watch' in url` (PeerTube, `app/utils.py:326-327`).** That is a second, independent way into `:661`. Note it in your report; Task 9 decides whether it needs its own witness.

- [ ] **Step 4: Run, confirm both arcs and three statements closed, commit**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_post_url.py -v
echo "exit=$?"
./run_tests.sh tests/test_shared_post_url.py \
  --cov=app.shared.post --cov-branch --cov-report=json:/tmp/t4.json -q
echo "exit=$?"
grep -oE "^    def (test_[a-z0-9_]+)" tests/test_shared_post_url.py | sort | uniq -d
git add tests/test_shared_post_url.py
git commit -F <message-file>
```

Subject: `test: cover edit_post's event banner arm and video hosting site arm`

---

### Task 5: The pixelfed arm

**Files:**
- Modify: `tests/test_shared_post_url.py`
- Read: `app/shared/post.py:600-629`, `app/utils.py:2998-3007`, `:3165-3225`

**Interfaces:**
- Consumes: `_seed`, `_api_input`, `_opengraph_page`, `_unreadable_page`.
- Produces: the pinned behaviour the spec's register-not-fix finding describes.

**Target:** statements 620-629; arcs `619->620`, `622->623`, `622->628`, `624->625`, `624->628`.

**This task uses the INVERTED convention.** `http_mock.head(...)` must report a non-image type so `:601` is false, and a GET route is then REQUIRED because `:621`'s `opengraph_parse` will run.

- [ ] **Step 1: Write the class docstring pinning the divergence**

```python
PIXELFED_URL = 'https://pixelfed.social/p/alice/1'


class TestPixelfedArm:
    """`:619-629` -- the pixelfed arm of the `:601`/`:619`/`:630`/`:641` chain.

    THE HARNESS IS INVERTED relative to tests/test_shared_post_upload.py. That
    file's rule (tests/README.md fact 229 point 3) is a HEAD reporting
    `image/png` and NO GET route, because an image content type makes `:601`
    true and `:601` never calls `opengraph_parse`. Reaching `:619` requires the
    opposite on both halves: a HEAD reporting a NON-image type so `:601` is
    false, and a GET route because `:621` WILL call `opengraph_parse`.

    A REGISTERED DIVERGENCE, PINNED HERE AND DELIBERATELY NOT FIXED. `:404`
    matches `'https://pixelfed.social/'` and `'https://pixelfed.uno/'` -- both
    with a scheme and a trailing slash. `:619` matches
    `'https://pixelfed.social'` (no trailing slash) and `'pixelfed.uno'` (NO
    SCHEME AT ALL). The scheme-less disjunct is REACHABLE, not dead:
    `is_image_url('pixelfed.uno/p/1')` is False, because
    `httpx.UnsupportedProtocol` is an `httpx.HTTPError` and
    `mime_type_using_head`'s handler (app/utils.py:345) returns '', sending
    `is_image_url` to extension sniffing, which finds no image extension in a
    path urlparse reports as the WHOLE string.

    The two sites also read DIFFERENT VALUES: `:403` tests `post.url`, the
    url the post already has, while `:619` tests `url`, the newly submitted
    one, which `:618`/`:628`/`:640`/`:652` have not yet written. So this is not
    one value checked twice with different strictness; it is two classifiers of
    the same kind of thing applied at different points, disagreeing. Whether
    scheme-less input should be accepted at all is a product question, and this
    round has no standing to answer it. Both branches are pinned as they
    behave today; the divergence is registered.
    """
```

- [ ] **Step 2: Write the five tests**

```python
    def test_a_pixelfed_url_is_typed_as_an_image_and_keeps_its_url(
            self, db_session, http_mock):
        """`:619` true -> `:620`; `:622` true -> `:623`; `:624` true -> `:625`,
        `:626`, `:627`; then `:628`, `:629`.
        Arcs 619->620, 622->623, 624->625; statements 620-629.

        FOUR witnesses, because no one of them is unique to this arm:
          - `post.type` is IMAGE -- also what `:617` writes, but `:601` is
            false here, which the absence of any second HEAD confirms.
          - `post.body` ends with the 'Source: ' suffix -- `:629` is the ONLY
            line in the function that appends it, so this is the arm's
            signature.
          - a `File` exists whose `source_url` is the og:image, not the post
            url -- `:602`'s File would carry the post url instead.
          - the File's `alt_text` is the og:title, which `:625` alone supplies.
        """
        http_mock.head(PIXELFED_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, PIXELFED_URL,
                        og_image='https://cdn.example.com/shot.jpg',
                        og_title='A photo')
        s = _seed()

        edit_post(_api_input(url=PIXELFED_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_IMAGE
        assert s.post.url == PIXELFED_URL
        assert s.post.body.endswith('\n\nSource: ')
        file = db.session.get(File, s.post.image_id)
        assert file.source_url == 'https://cdn.example.com/shot.jpg'
        assert file.alt_text == 'A photo'

    def test_a_pixelfed_url_falls_back_to_og_image_url(self, db_session, http_mock):
        """`:622`'s SECOND disjunct alone, and `:623`'s `or` fallback.

        The page carries `og:image:url` and no `og:image`, so
        `opengraph.get('og:image', '') != ''` is False and the block is
        admitted by the second disjunct; `:623`'s
        `opengraph.get('og:image') or opengraph.get('og:image:url')` then
        returns None on its left operand and falls through to the right.

        Without this test the two disjuncts move only in lockstep and a swap
        between them is undetectable -- false-witness mechanism 5.
        """
        http_mock.head(PIXELFED_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, PIXELFED_URL,
                        og_image_url='https://cdn.example.com/fallback.jpg',
                        og_title='Fallback')
        s = _seed()

        edit_post(_api_input(url=PIXELFED_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        file = db.session.get(File, s.post.image_id)
        assert file.source_url == 'https://cdn.example.com/fallback.jpg'

    def test_a_pixelfed_url_with_an_unreadable_page_still_keeps_its_url(
            self, db_session, http_mock):
        """`:622` false -> `:628`. Arc 622->628.

        `_unreadable_page` makes `parse_page` return False at
        app/utils.py:3195-3196, so `opengraph` is falsy and the whole File
        block is skipped -- but `:620`, `:628` and `:629` still run.

        THE POSITIVE CONTROL for 'no File' is the first test in this class,
        which builds one through the same mechanism. `post.type`, `post.url`
        and the 'Source: ' suffix are asserted here too, so this is not a
        bare emptiness assertion (false-witness mechanism 3).
        """
        http_mock.head(PIXELFED_URL).respond(200, headers={'Content-Type': 'text/html'})
        _unreadable_page(http_mock, PIXELFED_URL)
        s = _seed()

        edit_post(_api_input(url=PIXELFED_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_IMAGE
        assert s.post.url == PIXELFED_URL
        assert s.post.body.endswith('\n\nSource: ')
        assert s.post.image_id is None
        assert File.query.count() == 0

    def test_a_site_relative_og_image_is_not_turned_into_a_file(
            self, db_session, http_mock):
        """`:624` false -> `:628`. Arc 624->628.

        Here `opengraph` IS truthy and `:623` DID produce a filename -- the
        difference from the test above is that the filename starts with '/',
        so `:624`'s `not filename.startswith('/')` is false.

        The two tests assert the same absence, and are told apart by which
        mutation kills them rather than by their assertions: forcing `:624`
        true makes THIS test build a File (a kill), while forcing `:622` false
        makes the FIRST test lose one. Task 9 must confirm both.
        """
        http_mock.head(PIXELFED_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, PIXELFED_URL, og_image='/relative/shot.jpg',
                        og_title='Relative')
        s = _seed()

        edit_post(_api_input(url=PIXELFED_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.image_id is None
        assert File.query.count() == 0
        assert s.post.url == PIXELFED_URL

    def test_a_scheme_less_pixelfed_uno_url_takes_the_same_arm(
            self, db_session, http_mock):
        """`:619`'s SECOND disjunct, which carries no scheme at all.

        PINS A REGISTERED DIVERGENCE. `:404` would NOT match this string --
        it requires 'https://pixelfed.uno/' -- so the same input is classified
        differently depending on which of the two dispatches sees it. The test
        records today's behaviour; it does not endorse it. See the class
        docstring.
        """
        bare = 'pixelfed.uno/p/bob/2'
        http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, bare, og_image='https://cdn.example.com/uno.jpg')
        s = _seed()

        edit_post(_api_input(url=bare), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_IMAGE
        assert s.post.body.endswith('\n\nSource: ')
```

**The last test's routing is uncertain and must be settled by running it, not by guessing.** A scheme-less string may never reach httpx as a matchable url at all — `mime_type_using_head` raises `httpx.UnsupportedProtocol` before any transport sees it, which is exactly what Task 1's Step 3 probe showed. If respx therefore never matches the HEAD route, `assert_all_called=True` will fail the test and tell you so. Drop the HEAD route in that case and record why; likewise check whether `opengraph_parse` reaches the GET, since `get_request` refuses some URIs outright via `is_invalid_get_request_uri` (`app/utils.py:132-134`). **Adjust the routes to what actually happens and paste the evidence.**

- [ ] **Step 3: Run, confirm the five arcs and ten statements, commit**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_post_url.py -v
echo "exit=$?"
./run_tests.sh tests/test_shared_post_url.py \
  --cov=app.shared.post --cov-branch --cov-report=json:/tmp/t5.json -q
echo "exit=$?"
grep -oE "^    def (test_[a-z0-9_]+)" tests/test_shared_post_url.py | sort | uniq -d
git add tests/test_shared_post_url.py
git commit -F <message-file>
```

Subject: `test: cover edit_post's pixelfed arm and pin its host-matching divergence`

---

### Task 6: The loops.video arm

**Files:**
- Modify: `tests/test_shared_post_url.py`
- Read: `app/shared/post.py:630-640`

**Interfaces:**
- Consumes: `_seed`, `_api_input`, `_opengraph_page`, `_unreadable_page`.

**Target:** statements 631-640; arcs `630->631`, `633->634`, `633->640`, `635->636`, `635->640`.

Structurally parallel to Task 5, with one extra line — `:636`'s `filename.replace('.jpg', '.720p.mp4')` — and one fewer, since there is no body suffix.

- [ ] **Step 1: Understand what CANNOT be a witness here**

`:640` is `post.url = url` and the generic arm's `:652` is `post.url = embed_url`. `fixup_url` (`app/utils.py:3311-3312`) returns `(url, url)` for every url in this round, so **`embed_url == url` and `post.url` is identical under both arms.** Asserting on it would witness nothing about which arm ran — false-witness mechanism 1, in its subtlest form.

Two things DO discriminate:

1. **`:636`'s `.720p.mp4` rewrite.** `:637` stores `source_url=filename` AFTER the replacement, so a `File.source_url` ending `.720p.mp4` can only have come from `:636`. Neither `:625` nor `:646` rewrites anything.
2. **`:631` sets `POST_TYPE_VIDEO` unconditionally.** The generic arm decides the type at `:654-657` and would write `POST_TYPE_LINK` for a url with no video extension. So a loops url with a non-video path typed VIDEO is the arm's signature.

Assert both in the main test.

- [ ] **Step 2: Write the four tests**

```python
LOOPS_URL = 'https://loops.video/v/clip9'


class TestLoopsArm:
    """`:630-640` -- the loops.video arm.

    Same inverted harness as TestPixelfedArm: a non-image HEAD so `:601` is
    false, plus a GET because `:632` calls `opengraph_parse`.

    `post.url` IS NOT A WITNESS HERE. `:640` writes `url` and the generic
    arm's `:652` writes `embed_url`, and `fixup_url` (app/utils.py:3311-3312)
    returns `(url, url)` for every url in this file -- so the two arms leave
    `post.url` identical. The discriminators are `:636`'s `.720p.mp4` rewrite,
    which nothing else in the function performs, and `:631`'s unconditional
    POST_TYPE_VIDEO, which the generic arm would not produce for a url whose
    path carries no video extension.
    """

    def test_a_loops_url_is_typed_as_video_and_rewrites_the_thumbnail_to_mp4(
            self, db_session, http_mock):
        """`:630` true -> `:631`; `:633` true -> `:634`; `:635` true -> `:636`,
        `:637`, `:638`, `:639`; then `:640`.
        Arcs 630->631, 633->634, 635->636; statements 631-640.
        """
        http_mock.head(LOOPS_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, LOOPS_URL,
                        og_image='https://cdn.loops.example/thumb.jpg',
                        og_title='A clip')
        s = _seed()

        edit_post(_api_input(url=LOOPS_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_VIDEO
        file = db.session.get(File, s.post.image_id)
        assert file.source_url == 'https://cdn.loops.example/thumb.720p.mp4'
        assert file.alt_text == 'A clip'

    def test_a_loops_url_falls_back_to_og_image_url(self, db_session, http_mock):
        """`:633`'s SECOND disjunct and `:634`'s `or` fallback, witnessed alone
        so a swap between the two disjuncts is detectable (mechanism 5)."""
        http_mock.head(LOOPS_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, LOOPS_URL,
                        og_image_url='https://cdn.loops.example/alt.jpg')
        s = _seed()

        edit_post(_api_input(url=LOOPS_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        file = db.session.get(File, s.post.image_id)
        assert file.source_url == 'https://cdn.loops.example/alt.720p.mp4'

    def test_a_loops_url_with_an_unreadable_page_is_still_typed_as_video(
            self, db_session, http_mock):
        """`:633` false -> `:640`. Arc 633->640.

        `:631` has already run, so the type assertion is a real positive
        witness rather than a bare absence; the File assertions are controlled
        by the first test in this class, which builds one the same way.
        """
        http_mock.head(LOOPS_URL).respond(200, headers={'Content-Type': 'text/html'})
        _unreadable_page(http_mock, LOOPS_URL)
        s = _seed()

        edit_post(_api_input(url=LOOPS_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_VIDEO
        assert s.post.url == LOOPS_URL
        assert s.post.image_id is None
        assert File.query.count() == 0

    def test_a_site_relative_loops_thumbnail_is_not_turned_into_a_file(
            self, db_session, http_mock):
        """`:635` false -> `:640`. Arc 635->640.

        `opengraph` is truthy and `:634` produced a filename; the '/' prefix is
        the only thing stopping it. Told apart from the test above by which
        mutation kills it, exactly as in TestPixelfedArm.
        """
        http_mock.head(LOOPS_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, LOOPS_URL, og_image='/thumbs/clip9.jpg')
        s = _seed()

        edit_post(_api_input(url=LOOPS_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_VIDEO
        assert s.post.image_id is None
        assert File.query.count() == 0
```

**`:636`'s `.replace('.jpg', '.720p.mp4')` is unconditional and global.** A filename with no `.jpg` passes through unchanged, and one containing `.jpg` more than once has every occurrence replaced. Note both in your report — Task 9 mutates that line and needs to know which variants are viable.

- [ ] **Step 3: Run, confirm the five arcs and ten statements, commit**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_post_url.py -v
echo "exit=$?"
./run_tests.sh tests/test_shared_post_url.py \
  --cov=app.shared.post --cov-branch --cov-report=json:/tmp/t6.json -q
echo "exit=$?"
grep -oE "^    def (test_[a-z0-9_]+)" tests/test_shared_post_url.py | sort | uniq -d
git add tests/test_shared_post_url.py
git commit -F <message-file>
```

Subject: `test: cover edit_post's loops.video arm and its thumbnail rewrite`

---

### Task 7: The generic opengraph arm

**Files:**
- Modify: `tests/test_shared_post_url.py`
- Read: `app/shared/post.py:641-657`, `app/utils.py:3010-3160`, `:5494` (`is_invalid_get_request_uri`)

**Interfaces:**
- Consumes: `_seed`, `_api_input`, `_opengraph_page`, `chdir_upload`.

**Target:** statements 644-650; arcs `643->644`, `645->646`, `645->652`, `647->648`, `647->652`.

**This is the only Region B task that writes to disk.** `:646`'s `url_to_thumbnail_file` (`app/utils.py:3010`) downloads the og:image, writes it under `'app/static/media/posts/' + ...` — **relative to the working directory** (`app/utils.py:3065`) — and decodes it with `Image.open` (`:3085`). So this task needs `chdir_upload` even though it uploads nothing, and its og:image response must carry **genuine image bytes**.

- [ ] **Step 1: Determine what makes `url_to_thumbnail_file` return None**

Read `app/utils.py:3010-3160` and list every path that returns None. At minimum: `is_invalid_get_request_uri(filename)` true (`:3011-3012`), the GET raising (`:3016-3017`), a status other than 200 (`:3019`), a content type that does not start with `'image'` (`:3021`), and an unsanitizable SVG (`:3056-3059`). Pick the cheapest one that needs no extra config and state which you chose.

- [ ] **Step 2: Write the four tests**

```python
GENERIC_URL = 'https://news.example.com/article'
THUMB_URL = 'https://cdn.example.com/lead.png'


def _png_bytes():
    """Genuine PNG bytes, small. `url_to_thumbnail_file` opens what it
    downloads with `Image.open` (app/utils.py:3085) and re-encodes it, so a
    placeholder string would raise rather than produce a File."""
    buf = BytesIO()
    Image.new('RGB', (8, 8), (7, 8, 9)).save(buf, format='PNG')
    return buf.getvalue()


class TestGenericOpengraphArm:
    """`:641-657` -- the `else` arm, the only one that fetches the thumbnail
    itself rather than just recording its url.

    `chdir_upload` IS REQUIRED HERE even though nothing is uploaded:
    `url_to_thumbnail_file` writes to 'app/static/media/posts/...'
    (app/utils.py:3065), a path relative to the working directory, which in the
    container is the bind-mounted repository root. Without the redirect every
    test in this class would leave a file in the source tree under a random
    `gibberish(15)` name that nothing ever removes.

    THREE GETs ARE IN PLAY, not one: `:642`'s `opengraph_parse` fetches the
    PAGE, and `:646`'s `url_to_thumbnail_file` fetches the OG:IMAGE. Both are
    registered separately, and `http_mock`'s `assert_all_called=True` means a
    test that does not reach one must not register it.

    THIS ARM'S FILE IS BUILT DIFFERENTLY. `:646` returns a `File` constructed
    with `file_path=` (app/utils.py:3155), while `:625` and `:637` construct
    theirs with `source_url=`. That difference is what tells this arm apart
    from TestPixelfedArm and TestLoopsArm.
    """

    def test_a_generic_url_downloads_the_opengraph_thumbnail(
            self, db_session, http_mock, chdir_upload):
        """`:643` true -> `:644`; `:645` true -> `:646`; `:647` true -> `:648`,
        `:649`, `:650`; then `:652`.
        Arcs 643->644, 645->646, 647->648; statements 644-650.
        """
        http_mock.head(GENERIC_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, GENERIC_URL, og_image=THUMB_URL,
                        og_title='A headline')
        http_mock.get(THUMB_URL).respond(200, headers={'Content-Type': 'image/png'},
                                         content=_png_bytes())
        s = _seed()

        edit_post(_api_input(url=GENERIC_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.url == GENERIC_URL
        assert s.post.type == POST_TYPE_LINK
        file = db.session.get(File, s.post.image_id)
        assert file.alt_text == 'A headline'
        assert file.file_path is not None
        assert file.source_url is None

    def test_a_site_relative_og_image_skips_the_download(
            self, db_session, http_mock, chdir_upload):
        """`:645` false -> `:652`. Arc 645->652.

        NO GET IS REGISTERED FOR THE THUMBNAIL, and that is itself part of the
        witness: `url_to_thumbnail_file` is never called, so registering one
        would fail `assert_all_called=True` at teardown. Had `:645` been
        inverted, respx would instead raise on an unmatched request. The test
        fails loudly either way round.
        """
        http_mock.head(GENERIC_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, GENERIC_URL, og_image='/assets/lead.png')
        s = _seed()

        edit_post(_api_input(url=GENERIC_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.image_id is None
        assert File.query.count() == 0
        assert s.post.url == GENERIC_URL

    def test_a_thumbnail_that_is_not_an_image_yields_no_file(
            self, db_session, http_mock, chdir_upload):
        """`:647` false -> `:652`. Arc 647->652.

        `url_to_thumbnail_file` returns None when the response's content type
        does not start with 'image' (app/utils.py:3021). The GET IS reached
        here -- which is what separates this from the test above, where it is
        not -- so it is registered, and `assert_all_called=True` proves it ran.
        """
        http_mock.head(GENERIC_URL).respond(200, headers={'Content-Type': 'text/html'})
        _opengraph_page(http_mock, GENERIC_URL, og_image=THUMB_URL)
        http_mock.get(THUMB_URL).respond(200, headers={'Content-Type': 'text/plain'},
                                         text='not an image')
        s = _seed()

        edit_post(_api_input(url=GENERIC_URL), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.image_id is None
        assert File.query.count() == 0

    def test_a_video_extension_on_a_generic_url_is_typed_as_video(
            self, db_session, http_mock, chdir_upload):
        """`:654`'s FIRST disjunct -> `:655`. The arcs here are already
        covered; the test exists so Task 9 can kill that operand's mutation.

        `:654` is a four-disjunct compound scored as one arc pair:
        `is_video_url(url) or url.endswith('.mp4') or url.endswith('.webm')
        or is_video_hosting_site(embed_url)`.
        """
        video_url = 'https://cdn.example.com/movie.mp4'
        http_mock.head(video_url).respond(200, headers={'Content-Type': 'text/html'})
        _unreadable_page(http_mock, video_url)
        s = _seed()

        edit_post(_api_input(url=video_url), s.post, POST_TYPE_LINK, SRC_API,
                  user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        assert s.post.type == POST_TYPE_VIDEO
```

- [ ] **Step 3: Analyse `:654`'s four disjuncts and record what Task 9 needs**

Do not write these witnesses blind. Work out, and paste the reasoning for, which operands can be isolated:

- `is_video_url(url)` (`app/utils.py:294-313`) lowercases and tests `urlparse(url).path` against `['.mp4', '.webm']`.
- `url.endswith('.mp4')` tests the RAW string, case-sensitively, including any query or fragment.

So the second disjunct is true while the first is false only when the raw string ends `.mp4` but the PATH does not — a **fragment** does that (`'https://x.example/a#b.mp4'` has an empty path and a `b.mp4` fragment). Conversely the first is true while the second is false for an uppercase extension or a trailing query. Confirm both by running `is_video_url` on your candidates before writing anything, and paste the output.

**`:654` IS D476's THIRD SITE.** It tests `.mp4` and `.webm` explicitly and NOT `.mov`, exactly as `is_video_url` does (`app/utils.py:295`), while `edit_post`'s own upload block admits `.mov` at `:465`. A `.mov` submission is therefore accepted as an upload and then typed `POST_TYPE_LINK` here. **Record it against D476; do not fix it** -- a missing equivalence class is invisible to both coverage and mutation, which is what made D476 worth registering.

`is_video_hosting_site(embed_url)` reads `embed_url`, not `url` — the only operand in the compound that does. With `fixup_url` returning `(url, url)` they are equal, so **this operand cannot be isolated from the others by value alone** in this file. Say so plainly if that is what you find; Task 9 needs the argument, not a guess.

- [ ] **Step 4: Confirm nothing was written into the repository**

```bash
cd /home/blentz/git/pyfedi
find app/static/media -type f | wc -l
git status --porcelain
```

The count must be unchanged from before the task. `chdir_upload` is what guarantees it; this check is what proves it.

- [ ] **Step 5: Run, confirm the five arcs and seven statements, commit**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_post_url.py -v
echo "exit=$?"
./run_tests.sh tests/test_shared_post_url.py \
  --cov=app.shared.post --cov-branch --cov-report=json:/tmp/t7.json -q
echo "exit=$?"
grep -oE "^    def (test_[a-z0-9_]+)" tests/test_shared_post_url.py | sort | uniq -d
git add tests/test_shared_post_url.py
git commit -F <message-file>
```

Subject: `test: cover edit_post's generic opengraph arm and thumbnail download`

---

### Task 8: The poll and event tail — five arcs, no statements

**Files:**
- Modify: `tests/test_shared_post_url.py`
- Possibly modify: `app/shared/post.py:673` (a `# pragma: no branch` comment only — see Step 2)
- Read: `app/shared/post.py:298-314`, `:343-357`, `:662-703`

**Interfaces:**
- Consumes: `_seed`, `_api_input`, `_web_form`.

**Target:** arcs `665->668`, `673->682`, `675->674`, `683->686`, `699->703`. **Zero statements** — every line in this region already runs; five specific arms do not.

- [ ] **Step 1: Re-measure before writing anything**

Tasks 5 and 6 assign `post.image = file` at `:626`/`:638`, and `:663-665` runs immediately afterward, so `665->668` may already have closed as a side effect. Measure the tree as it stands:

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_post_url.py tests/test_shared_post_edit.py \
  tests/test_shared_post_upload.py tests/test_shared_post_make.py \
  tests/test_shared_post_lifecycle.py \
  --cov=app.shared.post --cov-branch --cov-report=json:/tmp/t8-before.json -q
echo "exit=$?"
```

Report which of the five arcs are still missing. **Write a test for all five regardless of what the measurement says.** An arc closed incidentally has no test naming it and nothing guarding it; Task 9 would then find a hole there, which is precisely the failure mode D470 records.

- [ ] **Step 2: Settle `673->682` — it is probably unreachable, and that changes what you write**

`:673` is `if 'choices' in poll_data:`. Both entry points build `poll_data` as a **fresh dict that always carries the key**:

```
303            parsed_poll = {
304                'mode': poll_data.get('mode', 'single'),
305                'local_only': poll_data.get('local_only', False),
306                'choices': poll_data.get('choices', [])
307            }
...
349            poll_data = {
350                'mode': input.mode.data,
351                'local_only': input.local_only.data,
352                'choices': poll_choices
353            }
```

`poll_data` is a local built inside `edit_post` and is not a parameter, so **no caller and no direct test call can supply a dict without the key.** `:315`'s bare `else:` means any `src` other than `SRC_API` takes the web branch, which sets it too.

**Verify this yourself before acting on it.** Read `:298-314` and `:343-357` with numbered output, paste both, and state whether any path leaves the key out. If you find one, write the test and ignore the rest of this step.

**If the arm is unreachable, the disposition is the one this repository already uses** — four sites carry it:

```
app/shared/tasks/pages.py:270    if not community.local_only:  # pragma: no branch -- see proof in test_shared_tasks_send_post.py
app/shared/tasks/pages.py:312    if 'name' in page:  # pragma: no branch -- see proof in test_shared_tasks_send_post.py
app/shared/tasks/pages.py:333    if not community.local_only:  # pragma: no branch -- see proof in test_shared_tasks_send_post.py
app/shared/tasks/follows.py:188  if not feed.instance.gone_forever:  # pragma: no branch -- see proof in test_shared_tasks_follows.py
```

So append to `:673`:

```python
        if 'choices' in poll_data:  # pragma: no branch -- see proof in test_shared_post_url.py
```

It is a comment on an existing line: no behaviour changes and `wc -l app/shared/post.py` stays **1193**. Put the proof in the test file as a class docstring, not only in the commit message — the pragma points at the file and a reader must find it there. Then register the finding; it is the same shape as D450 (`report_post:875`), which was registered and left rather than deleted.

**Do not delete the branch.** Removing a guard because no current caller can trip it is a behaviour change this round has no standing to make, and `:673` guards a `poll_data` shape a future caller could easily produce.

- [ ] **Step 3: Probe how `665->668` actually arises**

`:663-665` is:

```
663    if url and post.image:
664        file = File.query.get(post.image_id)
665        if file:
666            file.alt_text = image_alt_text
```

`:663` reads the **relationship** and `:664` reads the **foreign key**. For `:665` to be false they must disagree — `post.image` truthy while `File.query.get(post.image_id)` returns nothing. That happens when `post.image_id` is still `None` at `:664` because the assignment at `:626`/`:638` has not been flushed, and `post.image_id` is evaluated as an argument BEFORE `.get()` runs its autoflush.

**`File.query.get(None)` is the load-bearing unknown.** Probe it before designing the test:

```python
# container-python harness, inside an app context
from app.models import File
print(repr(File.query.get(None)))
```

If it returns `None`, `665->668` arises naturally from the pixelfed and loops arms and your test builds on one of them. If it RAISES, then Tasks 5 and 6 would have crashed — they did not, so report the contradiction and find the real mechanism before writing anything. Either way, paste the output.

- [ ] **Step 4: Write the five tests**

```python
class TestPollAndEventTail:
    """`:662-703` -- five arms whose LINES all run and whose branches do not.

    This region contributed zero missing statements and five missing arcs, so
    nothing here is about reaching new code; it is entirely about reaching the
    other side of decisions the existing tests only ever take one way.

    `:673`'s FALSE ARM IS UNREACHABLE and carries a `# pragma: no branch` for
    that reason. Both entry points build `poll_data` as a fresh dict that
    always has the key -- `:306` is `'choices': poll_data.get('choices', [])`
    on the API branch and `:352` is `'choices': poll_choices` on the web
    branch -- and `poll_data` is a local, not a parameter, so no caller and no
    direct test call can supply a dict without it. `:315`'s bare `else:` means
    any `src` that is not SRC_API takes the web branch, which sets it too.
    Registered, not deleted: the guard describes a `poll_data` shape a future
    caller could produce.
    """

    def test_a_blank_choice_is_skipped_and_the_next_one_is_still_added(
            self, db_session, http_mock):
        """`:675` false -> back to `:674`. Arc 675->674, the LOOP-BACK.

        A single bad choice cannot produce this arc -- the loop would have no
        next iteration to return to. The blank entry must be FOLLOWED by
        another, which is what makes the second choice's presence the witness:
        it can only have been added by an iteration that ran after the skip.
        """
        http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'text/html'})
        s = _seed()

        edit_post(_api_input(url=None, poll={'choices': [
            {'choice_text': '   ', 'sort_order': 1},
            {'choice_text': 'kept', 'sort_order': 2},
        ]}), s.post, POST_TYPE_POLL, SRC_API, user=s.user, from_scratch=True)

        db.session.refresh(s.post)
        texts = {c.choice_text for c in PollChoice.query.filter_by(post_id=s.post.id).all()}
        assert texts == {'kept'}

    def test_editing_a_post_that_already_has_a_poll_reuses_the_existing_row(
            self, db_session, http_mock):
        """`:683` FALSE -> `:686`. Arc 683->686.

        The create path always takes `:683`'s true arm, so this needs an edit
        of a post that already carries a `Poll`. The witness is the row's
        IDENTITY plus the count: a second call must leave exactly one `Poll`
        whose id is the one seeded, and must have applied the new mode to it.
        Asserting the mode alone would pass just as well against a freshly
        created row (false-witness mechanism 1).
        """
        http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'text/html'})
        s = _seed()
        existing = Poll(post_id=s.post.id, mode='single')
        db.session.add(existing)
        db.session.commit()
        existing_id = existing.id

        edit_post(_api_input(url=None, poll={'mode': 'multiple', 'choices': [
            {'choice_text': 'a', 'sort_order': 1}]}),
            s.post, POST_TYPE_POLL, SRC_API, user=s.user, from_scratch=True)

        polls = Poll.query.filter_by(post_id=s.post.id).all()
        assert len(polls) == 1
        assert polls[0].id == existing_id
        assert polls[0].mode == 'multiple'

    def test_editing_a_post_that_already_has_an_event_reuses_the_existing_row(
            self, db_session, http_mock):
        """`:699` FALSE -> `:703`. Arc 699->703. The event twin of the test
        above, with the same identity-plus-count witness."""
        http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'text/html'})
        s = _seed()
        existing = Event(post_id=s.post.id)
        db.session.add(existing)
        db.session.commit()
        existing_id = existing.id

        edit_post(_api_input(url=None, event={
            'start': '2030-01-01T09:00:00Z', 'end': '2030-01-01T10:00:00Z',
            'timezone': 'UTC', 'max_attendees': 5}),
            s.post, POST_TYPE_EVENT, SRC_API, user=s.user, from_scratch=True)

        events = Event.query.filter_by(post_id=s.post.id).all()
        assert len(events) == 1
        assert events[0].id == existing_id
        assert events[0].max_attendees == 5
```

The `665->668` test's shape depends on Step 3's probe; write it from what the probe showed, give it a docstring that names the mechanism, and assert something that could only hold on that arm — at minimum that `image_alt_text` was NOT applied to the `File` the arm failed to find, with the true-arm case as its positive control.

**`url=None` in the poll and event tests keeps `:565` closed** so no GET is needed, but `:403`'s `if post.url:` is false too (`_seed` defaults `url=None`), so the wildcard HEAD route above may go uncalled and fail `assert_all_called=True`. **Run it and drop the route if it is not reached** — do not carry a route on the assumption that something requests it.

- [ ] **Step 5: Run, confirm, and verify the pragma changed nothing else**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_post_url.py -v
echo "exit=$?"
wc -l app/shared/post.py
git diff -- app/shared/post.py
```

`wc -l` must report **1193**. The diff must show at most one line, differing only by an appended comment.

- [ ] **Step 6: Commit**

```bash
cd /home/blentz/git/pyfedi
grep -oE "^    def (test_[a-z0-9_]+)" tests/test_shared_post_url.py | sort | uniq -d
git add tests/test_shared_post_url.py app/shared/post.py
git commit -F <message-file>
```

Subject: `test: cover edit_post's poll and event tail arms`

If the pragma was added, say so in the body and give the proof in prose — a reader must be able to check the claim without rerunning coverage.

---

### Task 9: The mutation pass

**Files:**
- Modify (temporarily, one line at a time): `app/shared/post.py`
- Modify: `tests/test_shared_post_url.py` (only where a mutation survives and a test must be strengthened)

**Interfaces:**
- Consumes: every test written by Tasks 1-8.

**Target:** no new coverage. This task's product is evidence that the coverage is real.

- [ ] **Step 1: Derive the statement list mechanically, and scope the pass by it**

**Scope by the STATEMENT list, not the arc table.** Sub-project 38's statement-scoped pass found 10 holes an arc-scoped pass would have missed — every one of them a branchless statement (D470), after all 39 of that round's arcs were witnessed and six tasks had already passed review.

```bash
cd /home/blentz/git/pyfedi
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import ast
src = open('app/shared/post.py').read()
tree = ast.parse(src)
fn = next(n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == 'edit_post')
lines = sorted({n.lineno for n in ast.walk(fn) if isinstance(n, ast.stmt)})
print(len(lines)); print(lines)
"
```

Intersect that with this round's target regions — `:387-460`, `:565-661`, `:662-703` — and mutate every statement in the intersection. Subtract nothing.

- [ ] **Step 2: Derive the compound list mechanically too**

**Do not hand-list compounds.** Sub-project 38's controller listed five; the AST reported seven, and the hand list both included a non-compound and omitted two real ones.

```bash
cd /home/blentz/git/pyfedi
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import ast
src = open('app/shared/post.py').read()
fn = next(n for n in ast.walk(ast.parse(src))
          if isinstance(n, ast.FunctionDef) and n.name == 'edit_post')
for n in ast.walk(fn):
    if isinstance(n, ast.BoolOp):
        print(n.lineno, type(n.op).__name__, len(n.values))
"
```

**One mutation per operand**, not one per line. Expect at least `:387` (3 disjuncts), `:435` (2), `:446` (3), `:603` (2), `:622` (2), `:633` (2), `:643` (2), `:654` (4), `:663` (2), `:669` (2), `:696` (2) — but take the list the AST prints, not this one.

- [ ] **Step 3: Run each mutation under the standing protocol**

For every mutation, in this order:

```bash
cd /home/blentz/git/pyfedi
awk 'NR==<LINE> {printf "%d\t%s\n",NR,$0}' app/shared/post.py          # read it first
sed -n '<LINE>s/<OLD>/<NEW>/p' app/shared/post.py                       # dry run: prints the result
sed -i '<LINE>s/<OLD>/<NEW>/' app/shared/post.py                        # apply, line-scoped
./run_tests.sh tests/test_shared_post_url.py -q                         # observe
echo "exit=$?"
git checkout -- app/shared/post.py                                      # restore
git diff --quiet -- app/ && echo CLEAN || echo DIRTY
wc -l app/shared/post.py
```

**`git diff -- app/` must be empty and `wc -l` must be 1193 before you move to the next mutation, and before any point at which you might stop and report.** A background scanner sampling the working tree mid-mutation will file a false finding; during a mutation window HEAD is the artifact, not the tree.

Never pipe the test run — `pytest` exits 1 on a session timeout and a pipeline eats the status. If you must pipe, read `${PIPESTATUS[0]}`.

- [ ] **Step 4: Apply the survivor rules honestly**

- **A crash kill is not a kill** unless a viable non-crashing variant of the same fault also dies. If forcing `:622`'s first conjunct true makes `False.get('og:image', '')` raise `AttributeError`, that proves the tests run the line, not that they check its meaning. Find a variant that returns a wrong value instead.
- **An operator can be structurally void.** `:387`'s second disjunct is one: an `is_owner` row satisfies `is_moderator` too, because `moderators()` (`app/models.py:716-722`) admits on `is_owner OR is_moderator` and `is_moderator` (`:740`) tests only `user_id` over that same list. Task 2 verified this by construction. Record it as **structurally void with the argument**, not as a surviving mutant.
- **An arc being equivalent does not make every mutation of its line equivalent.** Sub-project 36 pre-registered an arc as equivalent and then found the mutation swapped which SET was tested — which is not equivalent at all. State which claim you are making, per mutation.
- **Non-failures are evidence.** A test whose docstring names a branch and stays green under that branch's mutation does not guard it. Strengthen the test; do not annotate the survivor away.

- [ ] **Step 5: Two mandatory mutations beyond the statement list**

Three consecutive rounds have found a real hole this way, because every test supplied a permitted input:

1. **Neutralise `:387`'s whole compound** so it never refuses — `if True:`. If every permission test stays green, no test asserts the check happened; only that the path which would have performed it was taken.
2. **Neutralise `:446`'s S3 guard** so it always fires. If Task 3's four false-conjunct tests stay green, they are not witnessing their operands.

- [ ] **Step 6: Handle `:654`'s four disjuncts with the subsumption analysis, not by guessing**

`is_video_url(url)` lowercases and tests `urlparse(url).path`; `url.endswith('.mp4')` tests the raw string case-sensitively, query and fragment included. Neither subsumes the other:

- first true, second false: `'https://x.example/a.MP4'` or `'https://x.example/a.mp4?t=1'`
- second true, first false: `'https://x.example/a#b.mp4'` — the fragment is not part of the path

`is_video_hosting_site(embed_url)` is the only operand reading `embed_url`, and `fixup_url` returns `(url, url)` here, so it cannot be isolated by value alone in this file. **Verify each candidate by running the helper before relying on it, and paste the output.** Where an operand genuinely cannot be isolated, say so with the argument.

- [ ] **Step 7: Write the report**

For every mutation: the line, the exact `sed`, what the pass predicted, what happened, and the classification — killed, structurally void, equivalent-with-argument, or a hole. For every hole, the test that now closes it. **Paste the proof beside every claim.**

Then confirm the tree:

```bash
cd /home/blentz/git/pyfedi
git status --porcelain
git diff -- app/
wc -l app/shared/post.py
./run_tests.sh tests/test_shared_post_url.py -q
echo "exit=$?"
```

- [ ] **Step 8: Commit any test strengthening**

```bash
cd /home/blentz/git/pyfedi
git add tests/test_shared_post_url.py
git commit -F <message-file>
```

Subject: `test: close the holes the edit_post mutation pass found`

If the pass found no holes, make no commit and say so — a clean pass is a result, not a failure to produce one.

---

### Task 10: Close the module

**Files:**
- Modify: `coverage_floors.ini`
- Modify: `tests/README.md`
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`

**Interfaces:**
- Consumes: every prior task.

- [ ] **Step 1: Measure the module over the five post test files**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_post_url.py tests/test_shared_post_edit.py \
  tests/test_shared_post_upload.py tests/test_shared_post_make.py \
  tests/test_shared_post_lifecycle.py tests/test_shared_post_interactions.py \
  tests/test_shared_post_moderation.py \
  --cov=app.shared.post --cov-branch --cov-report=json:/tmp/final.json -q
echo "exit=$?"
```

Confirm the file list against `ls tests/test_shared_post_*.py` — do not take it from this plan.

- [ ] **Step 2: Verify zero missing, checking arcs PAIRWISE**

Read `/tmp/final.json` inside the container and print `missing_lines` and `missing_branches` for `app/shared/post.py` in full. Both must be empty.

**Check every arc as a pair.** Inspecting a global minimum and maximum proves nothing about the arcs between them — that is how an earlier round convinced itself a region was closed when it was not.

Record `summary.percent_covered` exactly as reported, to full precision.

- [ ] **Step 3: Raise the floor to the measured value, rounded down**

```bash
cd /home/blentz/git/pyfedi
grep -n "app/shared/post.py" coverage_floors.ini
```

Currently `app/shared/post.py = 93`. Set it to `floor(percent_covered)`. Take the number from the JSON, never from this plan.

- [ ] **Step 4: Run the FULL suite in the foreground, unpiped, with the floors check chained**

This is the controller's job, not an implementer's. One pytest session at a time.

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh --cov=app --cov-branch --cov-report=json:/tmp/full.json \
  && podman-compose -f compose.test.yaml exec -T test-runner \
       python tests/check_coverage_floors.py /tmp/full.json
echo "exit=$?"
```

**Do not pipe it.** `pytest` exits 1 on a session timeout and `run_tests.sh` propagates it; a pipeline discards that. If a failure appears, run `./run_tests.sh --down` and retry once before believing it — this suite's timeouts have been memory pressure, not the tests.

All 20 floors must be met. Report the collected counts from pytest's own output.

- [ ] **Step 5: Extend `tests/README.md` fact 229 and add facts from 230**

**Fact 229 must be extended, not left as it stands.** Its point 3 currently states an absolute rule — a HEAD reporting `image/png` and no GET route — that is correct for `tests/test_shared_post_upload.py` and wrong for the arms this round targets. Rewrite point 3 so it states the rule **and its inverse**, names which arms each serves, and cites both test files. A later round that reads only the old form cannot reach `:619`, `:630` or `:641`.

New facts from **230**. At minimum:

- The inverted convention as a fact in its own right, with the content types and the arms.
- `fixup_url` returning `(url, url)` for ordinary urls, and the consequence that `post.url` cannot distinguish `:640` from `:652`.
- `url_to_thumbnail_file` writing to a working-directory-relative path, so `chdir_upload` is required by a test that uploads nothing.
- `S3_PUBLIC_URL` defaulting to `''`, making `:446`'s third conjunct trivially true.
- `scheduled_for` being unreachable from the API branch (`:281`).
- `:387`'s second disjunct being subsumed by its first through `moderators()`.

- [ ] **Step 6: Register findings from D478 and move the marker**

Open `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`, whose marker currently reads **"Next free number: D478."** Register at least:

- The **pixelfed host-matching divergence** between `:404` and `:619`, with the probe evidence, the fact that the two sites read different variables at different points, and the explicit ruling that it is registered rather than fixed because `:619`'s disjunct is reachable and the product question is not this round's to answer.
- **`:387`'s structurally void second disjunct**, with the `moderators()` argument.
- **`:673`'s unreachable false arm** and its pragma, if Task 8 confirmed it — cross-referencing D450, which is the same shape.
- Anything Task 9's mutation pass turned up.

Cross-reference D450, D463, D465, D470, D476 and D477 where they bear. A round that records only what worked leaves the next round to rediscover what did not — that is D477's own lesson.

Move the marker to the next free number.

- [ ] **Step 7: State plainly that the module is closed, and what that means for the campaign**

`app/shared/post.py` was the campaign's target for six consecutive sub-projects (34 through 39). With it closed, **no scheduled round owns anything.** Record in the register that the open findings are the natural next target: D421, D442, D450, D463, D465, D476 — six defects across five files, none owned by any round.

- [ ] **Step 8: Commit**

```bash
cd /home/blentz/git/pyfedi
git add coverage_floors.ini tests/README.md \
        docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md
git commit -F <message-file>
```

Subject: `test: close app/shared/post.py and raise its coverage floor`

The body must give the measured `percent_covered`, the floor before and after, the full-suite result, and the fact that fact 229 was corrected rather than merely extended.
