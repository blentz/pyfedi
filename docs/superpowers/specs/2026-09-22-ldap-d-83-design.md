# Sub-project 83 slice D: `app/ldap_utils.py` and the directory arm of login

**Status:** slice D complete. SEVEN production defects fixed (D1145–D1151).
**Branch:** `blentz`
**Predecessor:** slice C, which closed `app/auth/oauth_util.py` and fixed
D1138–D1144. **68 floors, rising to 69.**

## Scope

`app/ldap_utils.py` in full — binding, reading, writing, and the connection
test — plus `validate_user_ldap_login`, `create_new_user_from_ldap` and the
LDAP arm of `process_login` in `app/auth/util.py`.

`app/ldap_utils.py` was **10.8%** and is now **100%**.

## Why this module

An instance that configures a directory makes it the **first** authenticator:
`process_login` tries LDAP before the local password, so everything here runs
before an attacker holds an account. It had 10.8% coverage.

## THE PRODUCTION CHANGES

### The login name went into a filter and a DN unescaped (D1145, D1146)

```
PROBE ax1 result: 'someone@example.com' | search: '(uid=*)'
PROBE ax2 bind dn: 'uid=bob,ou=admins,dc=example,dc=com'
PROBE ax8 search: '(uid=bob)(uid=*)' | added: 'uid=bob)(uid=*,dc=example,dc=com'
```

`LoginForm.user_name` carries `DataRequired` and nothing else — no charset
rule, because a local name is not what it is matched against. That string was
interpolated into

* `user_filter.format(username=user_name)`, so `*` became `(uid=*)` — **every
  entry in the directory** — and the code takes `entries[0]`, whose address is
  what the local account is then built around;
* `f"{attr}={user_name},{base_dn}"`, so the caller chose which subtree the
  bind was attempted against.

The write side had both too, where the injected name also chose the DN of the
entry it created. `escape_filter_chars` and `escape_rdn`, from `ldap3` itself,
at all four sites.

### An empty password is an anonymous bind (D1147)

```
PROBE ax3 result: 'someone@example.com' | bind password: ''
```

A simple bind carrying an empty password is an **unauthenticated bind**, which
RFC 4513 says a server should treat as anonymous — and an anonymous bind
succeeds. `login_with_ldap(name, '')` therefore came back with an address for
a password nobody checked. `LoginForm`'s own `Length(min=8)` keeps the web arm
off that path; the refusal belongs in `_bind_user`, where every caller passes
through, including `/test_ldap_login` and the write bind.

### The directory arm made none of the local arm's checks (D1148)

```
PROBE ax4 user: <User person_2> | banned: True
```

`validate_user_ldap_login` was a bind and a lookup. A **banned** account logged
in through it. `validate_user_login`'s non-password checks are now
`refuse_if_banned`, called from both ends — and the LDAP arm answers **False**
rather than None when it refuses, so `process_login` stops instead of falling
through to a local password that must not overrule the refusal.

### The directory could mint any local name (D1149, D1150)

```
PROBE ax5 users before: 2 | after: 3 | names: ['founder', 'person', 'person']
PROBE ax6 admin exists: True
PROBE ax7 names: ['founder', 'person', 'we ird/../x']
```

`create_new_user_from_ldap` wrote whatever it was handed:

* a **second row** holding a deleted account's name, because `find_user`
  filters deleted rows out and nothing looked again (D1149);
* `admin`, the name this instance reserves (D1150);
* `we ird/../x`, outside `USER_NAME_CHARSET_RE` — and a local user name is
  interpolated into an actor URL, a webfinger answer and a feed regex (D1150).

`can_be_a_local_user_name` refuses rather than alters: the local name has to be
a **stable function** of the directory name, or the next login would not find
the account this one created. D1139's shape at a third door.

### D1151, the second deleted test

`validate_user_login` closed with a deleted check after the one it opens with —
unreachable, and worded "This account has been deleted." where the first
answers the same message every other failure gets, because saying which
accounts were deleted is the enumeration D1131 closed. Removed with the
extraction.

## The mutation pass

22 mutants, 20 killed on the measuring pass. Both survivors were rows of mine
that asserted the **answer** without asserting that nothing was contacted:
with `LDAP_READ_ENABLE` ignored, the unpatched `ldap3` failed to connect and
returned the same False the row expected. Both now assert the connection was
never constructed. 22/22.

## Success criteria

- `app/ldap_utils.py` at **100%**, and every LDAP arm of `app/auth/util.py`
  covered (that module rises from 63.1% to 73%; its floor is taken when it
  closes in slice E).
- D1145–D1151 land with their pins inverted.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1145**; `tests/README.md` facts from **536**.
