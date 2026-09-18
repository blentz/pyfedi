# Sub-project 59: `app/activitypub/actor.py`

**Status:** design approved, ready for an implementation plan
**Branch:** `blentz`, at `09f143ade`
**Predecessor:** sub-project 58, which closed `app/activitypub/signature.py` at
100.0 — 35 floors.

## Goal

Cover the actor-resolution module — how this server finds, validates, fetches
and creates the remote actors every federation path starts from — repair the
four defects probed while scoping it, **close D739 in its other copy**, and
take the module's first floor.

## Targets

From the full-suite JSON at the delivered tree (`app/activitypub/actor.py`
62.763%, 213 statements):

| function | where | missing statements | missing arcs | total |
|---|---|---|---|---|
| `fetch_actor_from_webfinger` | `:208-247` | 31 | 8 | 39 |
| `fetch_remote_actor_data` | `:150-204` | 26 | 7 | 33 |
| `find_remote_actor` | `:86-131` | 8 | 7 | 15 |
| `create_actor_from_remote` | `:250-276` | 7 | 6 | 13 |
| `schedule_actor_refresh` | `:134-147` | 4 | 6 | 10 |
| `find_actor_by_url` | `:279-332` | 3 | 4 | 7 |
| `validate_remote_actor` | `:39-83` | 3 | 3 | 6 |
| `find_local_user` | `:29-36` | 0 | 1 | 1 |
| **total** | | **82** | **42** | **124** |

Inside the band, and the module is taken whole.

## THE FOUR PRODUCTION CHANGES

### P1 — `find_actor_by_url`'s local branches ignore the kind they were asked for

```python
if f"{server_name}/c/" in actor_url and "/p/" not in actor_url:
    actor = find_local_community(actor_url)
    if actor and community_only:
        return actor
    elif actor and not community_only:
        return actor          # app/activitypub/actor.py:290-294
    return None
```

Both arms return the same object, so `community_only` decides nothing — and
`feed_only` is not consulted at all. The feed branch below has the mirror
problem with `community_only`. Only the user branch honours both.

**Probes:**

```
PROBE a1 feed_only on a community url gave: Community <Community 1>
PROBE a2 community_only on a feed url gave: Feed <Feed localfeed_1>
```

A caller that asked for a feed is handed a community and goes on to treat it as
one. **Fix:** each local branch returns `None` when the caller asked for a
different kind, which is what the remote branch at `:316-319` already does.

### P2 — a webfinger `self` link with no `href` is a crash

```python
for link in webfinger_json.get('links', []):
    if link.get('rel') == 'self':
        type_header = link.get('type', 'application/activity+json')
        actor_data = get_request(link['href'], ...)     # :234
```

`rel` and `type` are read defensively; `href` is not.

**Probe:** `PROBE a3 exception: KeyError 'href'`.

**This is D739's twin.** Sub-project 55 registered the same unguarded read in
`app/feed/util.py` and deferred it, because the error contract for malformed
remote replies is one decision across several modules. **That decision is made
here: both copies are repaired**, because a guard present in one copy and
missing from the other is D712's shape, which this campaign has now met five
times. A link without an `href` is skipped, and the walk continues to the next
link rather than giving up — a peer may advertise several.

**The round's diff therefore names `app/feed/util.py` as well**, for that one
guard and nothing else, and D739 is closed by this round.

### P3 — the webfinger response is leaked on two paths

The response is closed only inside the success branch. A 404, or a 200 with a
content type the function does not accept, returns without closing it:

```
PROBE a4 result: None response closed: False
PROBE a4 wrong content type: None response closed: False
```

Under httpx's connection pooling an unclosed response holds its connection
until garbage collection. **Fix:** close it on every path.

### P4 — `fetch_actor_from_webfinger` sleeps on the request thread

Three `time.sleep(randint(3, 10))` calls sit in the retry paths, and this
function is called from `create_actor_from_remote`, which the inbox reaches
while a peer waits. This is **fact 302 and D738's shape**, registered there and
repaired nowhere.

**Fix here is narrow and does not move the retry off the thread**: the sleeps
stay, and every test patches `app.activitypub.actor.time.sleep` so the suite
does not pay them. What IS repaired is that **the second attempt's failure
returns None rather than raising** — already true — so P4 is a **test-harness
obligation rather than a code change**, and the sleeps are registered as R1.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `:213`, `:236`, and `fetch_remote_actor_data:177`, `:190` | **Four `time.sleep(randint(3, 10))` calls on the request thread**, reachable from the inbox. D738's shape in a second module. Every test patches the sleep (fact 302). | Moving the retries off the request is a scheduling change, and the same one D738 defers. |
| R2 | `:97-104`, `:116-123` | **`find_remote_actor` re-queries for a non-banned copy of a community with the SAME `ap_profile_id`, twice.** `ix_community_ap_profile_id` is unique — `PROBE a5 exception: IntegrityError ... duplicate key value violates unique constraint` — so the re-query cannot answer differently from the query above it: a banned row always re-queries to `None`. Dead code, written twice. | **D758's decision, kept**: 57 registered `chat_report`'s dead `already_notified` set rather than deleting it. The arcs it makes unreachable get a proof, and the unique index IS the proof. |
| R3 | `:332` | **`return None` after an if/else whose both arms return** — unreachable by inspection. | The same shape as D724; proved and registered rather than deleted. |
| R4 | `:39-41` | **`validate_remote_actor`'s docstring sits AFTER its first `return`**, so it is a bare expression statement rather than a docstring and `help()` shows nothing. | Cosmetic, and moving it is a diff in a function this round is otherwise not changing. |

## Success criteria

- Every function at `[]`/`[]` on the **full-suite** run, except arcs declared
  unreachable with a named cause and a proof — R2 and R3 are expected to
  produce exactly such arcs.
- **The module takes its first floor** at the measured figure rounded down.
- The four repairs land with pins inverted; `git diff --numstat` names exactly
  `app/activitypub/actor.py` and `app/feed/util.py`, the second for D739's
  guard alone.
- Suite green; floors checked with `&&`; a mutation pass over the module, per
  D602.
- Findings registered from **D769**; `tests/README.md` facts from **312**.
- D739 marked closed in the register, with the commit that closed it.
- `tests/test_zz_actor_probe.py` deleted before delivery.
