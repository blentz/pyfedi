# Sub-project 81 slice B: the profile form, and settings import/export

**Status:** slice B complete. EIGHT production defects fixed (D1041–D1048),
including the last three of D988's eleven mutating GETs.
**Branch:** `blentz`
**Predecessor:** slice A, which fixed D1033–D1040. **65 floors.**

## Scope

| function | gaps |
|---|---|
| `import_settings_task` | 100 |
| `edit_profile` | 87 |
| `export_user_settings` | 85 |
| `user_settings_import_export` | 24 |
| `notifications_all_read`, `remove_avatar`, `remove_cover`, `import_settings` | the rest |

## THE PRODUCTION CHANGES

### P1 — three more mutating GETs (D1041, D1044)

`remove_avatar`, `remove_cover` and `notifications_all_read` all accepted GET
and all mutated — the last three of D988's eleven, and the ones this blueprint
owns. Measured for the third:

```
PROBE v3 status: 302 unread rows: 0
```

for a bare GET. All three are POST-only now. `notifications.html`'s button
moves to the `send_post` pattern; **the "Mark all as read" link in the
notification EMAIL cannot carry a CSRF token at all**, so it now points at the
notifications page, where the action lives behind a POST. That is a deliberate
loss of a convenience, recorded rather than hidden.

### P2 — an unbounded upload into Redis (D1042)

`import_file.stream.read()` with no argument read the whole upload into memory
and stored it in Redis with a one-hour TTL — and this application sets **no
`MAX_CONTENT_LENGTH` at all**:

```
PROBE v1 MAX_CONTENT_LENGTH: None
```

So the size was whatever the client chose to send. Capped at 5 MB, read one
byte past the limit so that a file exactly at it is still accepted.

### P3 — a 500 from a query parameter (D1043)

`?type=abc` on `notifications_all_read`:
`ValueError: invalid literal for int() with base 10: 'abc'`. An unusable
filter now means "all of them", which is what an empty filter already meant.

### P4 — the import file's shape was an assumption (D1045, D1046)

The task iterated whatever it found under each key. A single URL given as a
bare string was iterated **character by character**, each character becoming
an actor lookup:

```
PROBE v4 find_actor_or_create calls: 25 first arg: h
```

And the list length was uncapped, while `find_actor_or_create` defaults to
`create_if_not_found=True` — which reaches `create_actor_from_remote`, an
outbound fetch of a URL the uploader chose. One upload therefore drove one
outbound request per entry, unbounded: D993 and D1025's family again. Both are
answered by one helper, `import_entries`, which refuses a non-list and caps at
500.

### P5 — a 400 for any client but the browser (D1047)

`request.files['profile_file']` raises `BadRequestKeyError` for a submission
that does not carry the field. The rendered form always does; nothing else has
to. D1003's shape, on files rather than form fields.

### P6 — the import task dereferenced a user who had gone (D1048)

The task is queued and runs later. Measured with an **empty** import file:
`AttributeError: 'NoneType' object has no attribute 'id'` — which is what
makes it a nil guard on the user rather than a per-entry one.

## One line marked rather than covered

`export_user_settings`'s `if target:` on a user note cannot take its false arm
while `user_note_target_id_fkey` holds, and the only way to force it from a
test is to patch `db.session.get`, which also breaks the login the request
needs. Marked `# pragma: no cover` with that reasoning.

## Success criteria

- The nine functions at `[]` on the **full-suite** run.
- P1–P6 land with their pins inverted — eight inversions.
- The D989 ratchet down to zero of D988's eleven unfixed.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1041**; `tests/README.md` facts from **470**.
