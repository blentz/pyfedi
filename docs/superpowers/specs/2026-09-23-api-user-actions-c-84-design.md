# Sub-project 84 slice C: the actions of `app/api/alpha/utils/user.py`

**Status:** slice C complete. FIVE production defects fixed (D1175–D1177,
D1179–D1181), three registered and pinned (D1178, D1182, D1183).
**Branch:** `blentz`
**Predecessor:** slice B, which closed the feed API and fixed D1173, D1174.
**74 floors, unchanged** — `app/api/alpha/utils/user.py` is 1,031 lines and
its floor is taken when the module closes.

## Scope

The action half of the module: `post_user_block`, `put_user_subscribe`,
`post_user_follow`, `post_user_unfollow`, `post_user_set_note`,
`post_user_set_flair`, `post_user_ban`, `post_user_unban`,
`post_user_verify_credentials`, `post_user_logout`, `post_user_register` and
`get_user_captcha`.

The reading half (`get_user`, the lists, the replies and media endpoints) is
slice D; `put_user_save_user_settings` and the notification group are slice E.

## THE PRODUCTION CHANGES

### Two ids that went straight into a foreign key (D1175, D1176)

```
PROBE bf1 outcome: IntegrityError: (psycopg2.errors.ForeignKeyViolation) ... user_flair_community_id_fkey
PROBE bf3 outcome: IntegrityError: (psycopg2.errors.ForeignKeyViolation) ... user_note_target_id_fkey
```

`community_id` and `person_id` are whatever the caller sent.

### Banning somebody who does not exist (D1177)

`ban_user` does `db.session.get(User, input['person_id'])` and then
`to_ban.banned = True`:

```
PROBE bf5 outcome: AttributeError: 'NoneType' object has no attribute 'banned'
```

Both the ban and the unban endpoint check first now. The guard is in the API
utils rather than in `ban_user`, because `ban_user`'s other caller is the web
form, which resolves the user before it gets there.

### A banned account's credentials verified (D1179)

The API's own login refuses a banned account with `incorrect_login`.
`post_user_verify_credentials` answered 200:

```
PROBE bf6 outcome: accepted
```

Two consequences. A client that asks this endpoint whether a password is good
was told yes for an account that cannot log in; and the pair of answers
distinguished **a banned account from a wrong password**, which is the
account-state oracle D1131 closed on the web arm. The founder's carve-out
(`user.id != 1`) is the login's own.

### Two endpoints nobody could reach (D1180, D1181)

```python
@user_bp.route('/api/alpha/user/register', methods=['POST'])
```

— and `user_bp` already carries `url_prefix="/api/alpha"`. Every other route
in the file is written relative to it, so these two lived at
`/api/alpha/api/alpha/...`:

```
PROBE bf8 /api/alpha/user/register: 404
PROBE bf8 /api/alpha/api/alpha/user/register: 400
```

Behind them, both utils were `...` stubs, and their routes load a response
schema from what they return — `Schema().load(None)`. They raise
`not implemented` now, so the API answers a 400 with a message rather than a
500.

## Registered and pinned

* **D1178** — an account with `ban users` may ban an **administrator**,
  including user 1. `user_access` answers True for everything for user 1, so
  they cannot be stripped of permissions, but `banned` still stops them
  logging in. Whether staff may ban an admin is a product decision, and a
  rule would have to say what happens to the founder.
* **D1182** — a token carrying no `jti` cannot be revoked, and logout answers
  `{'success': True}` anyway. `encode_jwt_token` always mints one.
* **D1183** — an equivalent mutant: dropping the `Bearer ` prefix check
  changes nothing, because `auth[7:]` then cuts into the token and
  `jwt.decode` raises the same `incorrect_login`.

## What the coverage found without a defect

`post_user_set_flair` lets somebody set flair in a community they have not
joined — and so does the web route (`app/community/routes.py:2790`), which
checks only that the community exists. The two ends agree, so there is
nothing here to fix; the API is not laxer than the product.

## The mutation pass

31 mutants, 28 killed on the measuring pass. Two survivors were rows of mine:

* the flair-length row asserted on the substring `'too long'`, which
  **Postgres also says** — `value too long for type character varying(50)` —
  so it passed with the application's guard removed (fact 553);
* nothing held `UserNote.user_id == user.id` in the replace path, so one
  person editing a note could have overwritten another's about the same
  target, and no row would have noticed.

Both rewritten; the third survivor is D1183.

## Success criteria

- Every function in scope at `[]`.
- D1175–D1177 and D1179–D1181 land with their pins inverted.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1175**; `tests/README.md` facts from **553**.
