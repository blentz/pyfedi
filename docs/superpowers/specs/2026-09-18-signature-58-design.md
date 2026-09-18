# Sub-project 58: `app/activitypub/signature.py`

**Status:** design approved, ready for an implementation plan
**Branch:** `blentz`, at `65a965089`
**Predecessor:** sub-project 57, which closed `app/chat` — 34 floors.

## Goal

Cover the HTTP-signature module — the outbound delivery task, the header and
signature parsing the inbox verifies with, and the small date helpers — repair
the two defect families probed while scoping it, and **take the module's first
floor.**

This is the first sub-project since the campaign's start to target
`app/activitypub` outside `util.py` and `routes.py`, and it is chosen for
consequence rather than size: every inbound activity this server accepts is
verified through this file, and every outbound one is signed by it.

## Targets

From the full-suite JSON at the delivered tree (`app/activitypub/signature.py`
70.66%, 305 statements):

| function | where | missing statements | missing arcs | total |
|---|---|---|---|---|
| `post_request` | `:95-172` | 27 | 18 | 45 |
| `fix_local_community_membership` | `:647-660` | 8 | 4 | 12 |
| `signature_part` | `:224-231` | 7 | 4 | 11 |
| `_get_public_key_instance` | `:273-295` | 3 | 4 | 7 |
| `_get_private_key_instance` | `:245-270` | 3 | 3 | 6 |
| `headers_from_request` | `:310-329` | 3 | 3 | 6 |
| `precheck` | `:380-395` | 3 | 3 | 6 |
| `signed_request` | `:425-515` | 3 | 3 | 6 |
| `parse_ld_date` | `:76-79` | 3 | 2 | 5 |
| `LDSignature.verify_signature` | `:531-563` | 3 | 1 | 4 |
| `parse_signature` | `:332-350` | 3 | 0 | 3 |
| `calculate_digest`, `verify_request`, `send_post_request`, `default_context`, `http_date` | scattered | 4 | 5 | 9 |
| **total** | | **70** | **50** | **120** |

Exactly the campaign's 110-130 band, and the module is taken whole.

## THE TWO PRODUCTION CHANGES

### P1 — three remote-triggerable 500s in `precheck`

```python
header_date = parse_http_date(request.headers["date"])                  # :393
if abs((datetime.now(timezone.utc) - header_date).total_seconds()) > 3600:
    raise VerificationFormatError("Date is too far away")
```

`precheck` is the inbox's first gate (`app/activitypub/routes.py:687-691`) and
its caller catches **`VerificationFormatError` only**. Three inputs escape as
something else:

```
PROBE s1 exception: ValueError Invalid date value or format "not a date"
PROBE s2 exception: TypeError can't subtract offset-naive and offset-aware datetimes
PROBE s3 exception: ValueError Invalid date value or format ""
```

- A malformed `Date` header raises `ValueError` out of `parsedate_to_datetime`.
- An empty one raises the same.
- A **well-formed date with no timezone** parses to a naive datetime, and
  subtracting it from an aware one is a `TypeError`. RFC 7231 requires the
  timezone, so this is a peer's bug — but the answer to a peer's bug is a 400,
  not a 500.

Every one of them is reachable by any host that can reach the inbox, and each
returns a 500 with a traceback where the handler four lines up intends a
logged 400.

**Fix:** parse inside a `try`, raise `VerificationFormatError("Date is not a
valid HTTP date")` for a parse failure, and treat a naive datetime as UTC
before comparing — the reading RFC 7231 mandates anyway. The inverted tests
assert the EXCEPTION TYPE, not merely that something was raised, since the
unrepaired code raises too.

### P2 — `signature_part` truncates values and crashes on malformed input

```python
part_parts = part.split('=')
part_parts[0] = part_parts[0].strip()
if part_parts[0] == key:
    return part_parts[1].strip().replace('"', '')
```

**Probes:**

```
PROBE s4 padded signature: 'YWJj'                      (input was signature="YWJj==")
PROBE s4 no equals exception: IndexError list index out of range
PROBE s4 header absent exception: AttributeError 'NoneType' object has no attribute 'split'
```

- `split('=')` with no maxsplit means **a base64 value loses its padding**, and
  a keyId carrying a query string loses everything after its first `=`.
- A comma-separated part with no `=` at all is an `IndexError`.
- No `Signature` header means `None.split`, an `AttributeError`.

The function has one caller today — `headers_from_request:319,321`, for the
`(created)` and `(expires)` pseudo-headers, whose values are integers — so the
truncation is latent. **The IndexError is not**: a peer's `Signature` header is
attacker-controlled and a single stray comma reaches it.

**Fix, all three:** `split('=', 1)`, skip a part with no `=`, and treat an
absent header as `''` — which is what the function already returns when the key
is not found, so the not-found path stays single.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `:100`, `:102` | **`post_request` crashes on a body that is None or carries no `id`** — `PROBE s5 exception: TypeError argument of type 'NoneType' is not iterable` and `PROBE s6 exception: KeyError 'id'`. The second escapes through `except Exception: session.rollback(); raise`, so the task dies with no log row. The signature's own type hint says `body: dict | None`. | Nothing in `app/` reaches either today, and a guard for the missing id needs a decision about what its log row should say. Covered as behaviour, registered with both probes. |
| R2 | `:141-146` | **A connection failure never retries, while a 500 does.** The inner handler sets `http_status_code = 404`, and the retry block only queues for `429` or `>= 500` — so a peer that is refusing connections is dropped after one attempt while a peer returning 502 is retried for four hours. | The retry policy is a federation decision, not a coverage one. Registered with the two paths quoted. |
| R3 | `:162` | **`datetime.utcnow()` feeds `SendQueue.send_after`** while the rest of the module has moved to timezone-aware `datetime.now(timezone.utc)`. Naive-vs-aware is exactly what P1's `TypeError` is made of. | The column's reader has to be checked before changing what is written to it. |
| R4 | `:319` | **The comment at the `(created)` call site says the author avoided `parse_signature` because "changing HttpSignatureDetails changes everything & I don't have the spoons for that ATM"** — so two parsers for one header exist by acknowledged accident, and P2 repairs the weaker one rather than removing it. | Merging them is a refactor with its own test surface. Registered so the duplication is known to be deliberate. |

## Success criteria

- Every function at `[]`/`[]` on the **full-suite** run, except arcs declared
  unreachable with a named cause and a proof.
- **The module takes its first floor** at the measured figure rounded down.
- Both repairs land with pins inverted, asserting exception TYPES where the
  unrepaired code also raises; `git diff --numstat` names exactly
  `app/activitypub/signature.py`.
- Suite green; floors checked with `&&`; a mutation pass over the module, per
  D602.
- Findings registered from **D762**; `tests/README.md` facts from **310**.
- `tests/test_zz_sig_probe.py` deleted before delivery.
