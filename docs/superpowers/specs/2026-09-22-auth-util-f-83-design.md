# Sub-project 83 slice F: the rest of `app/auth/util.py`, and the floors

**Status:** slice F complete. FIVE production defects fixed (D1159–D1163), two
dead arms removed (D1164, D1165), one equivalent mutant recorded (D1166).
**This is the last slice of sub-project 83.**
**Branch:** `blentz`
**Predecessor:** slice E, which closed `app/auth/onboarding.py` and fixed
D1152–D1158. **70 floors, rising to 72.**

## Scope

Everything left in `app/auth/util.py`: where a visitor is from
(`ip2location`, `get_country`), who is told about a registration
(`no_admins_logged_in_recently`, `notify_admins_of_registration`,
`create_registration_application`), the steps a registration goes through, and
where a login lands. Plus the last four lines of `app/auth/routes.py`.

`app/auth/util.py` was **75%** and is now **100%**; `app/auth/routes.py`
closes at **100%** too.

## THE PRODUCTION CHANGES

### An ipinfo.io outage took authentication down with it (D1159)

`ip2location` called `get_request(url)` with nothing around it.
`get_request` normalises every transport failure to `httpx.HTTPError` — and
its only caller here is `get_country`, which runs on **every registration,
every login and every OAuth callback**. Measured:

```
PROBE az1 outcome: HTTPError: boom
```

The retry makes it worse: `get_request`'s arms sleep 3–10 seconds before a
second attempt, so a slow service held the request open too. A country nobody
could look up is a country we do not know, which is exactly what the empty
answer already means everywhere else in the function.

### A private address had no city, and the code read one (D1160)

```
PROBE az2 outcome: KeyError: 'city'
```

ipinfo answers `{"ip": ..., "bogon": true}` for a private or reserved
address — no city, region, country or timezone. Only `127.0.0.1` is rewritten
to a public address on the way in, so every LAN address reached this. Same
blast radius as D1159.

### An admin who had never logged in (D1161)

```
PROBE az3 outcome: TypeError: '>' not supported between instances of 'NoneType'
and 'datetime.datetime'
```

`last_seen` is nullable. The caller is `handle_abandoned_open_instance`, on
the registration page, so the exception closed registration by crashing it —
the opposite of the safety's purpose.

### Told twice about one application (D1162)

Two loops, one notification each: `Site.admins()` then `Site.staff()`. The
founder of a small instance is usually both.

```
PROBE az4 notifications: 2 | unread counter: 2
```

### Every registration wrote the directory twice (D1163)

`register_new_user` calls `sync_user_with_ldap(user, form.password.data)`, and
`finalize_user_registration` — four lines later, down the same path — called
`sync_user_to_ldap(user.user_name, user.email, form.password.data.strip())`
with its own copy of the try/except the helper already carries. Measured:

```
PROBE az5 sync_user_to_ldap calls: 2
```

One bind, search and modify against the directory, then the whole thing again.

### Two arms that could not be taken (D1164, D1165)

* `resend_email` tested `if user:` eight lines below `if user is None:
  return`;
* `verify_email` wrapped its **entire body** in `if token != '':` with no else
  arm, so had it ever been false the view would have returned None — a 500.
  It never was: Flask's default converter does not match an empty path
  segment, so `/verify_email/` is a 404.

## D1166, an equivalent mutant

Dropping `not current_user.finished_onboarding` from
`check_user_finished_onboarding` leaves the function writing `True` over a
column that is already `True`. No caller can see the difference; the only
effect is a redundant commit. Recorded rather than chased.

## The mutation pass

31 mutants, 30 killed on the measuring pass. The survivor is D1166.

## Sub-project 83 in total

Six slices closed the authentication surface:

| module | before | after |
|---|---|---|
| `app/auth/routes.py` | 25.7% | **100%** |
| `app/auth/util.py` | 20.4% | **100%** |
| `app/auth/oauth_util.py` | 11.9% | **100%** |
| `app/auth/onboarding.py` | 15.1% | **100%** |
| `app/ldap_utils.py` | 10.8% | **100%** |

**35 production defects, D1129–D1165.** Everything an attacker reaches before
holding an account: login, registration, the verification link, the reset
link, three OAuth providers and the directory.

## Success criteria

- `app/auth/util.py` and `app/auth/routes.py` at **100%**, both floored.
- D1159–D1163 land with their pins inverted.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1159**; `tests/README.md` facts from **545**.
