# Sub-project 82 slice C: editing, flair, embeds and the inline reply

**Status:** slice C complete. EIGHT production defects fixed (D1089–D1096).
**Branch:** `blentz`
**Predecessor:** slice B, which closed `show_post` and fixed D1084, D1086 and
D1087. **66 floors, unchanged** — `app/post/routes.py`'s floor is taken when
the module closes.

## Scope

`post_edit` (93 uncovered), `post_set_flair` (51), `post_embed_code` (44),
`add_reply_inline` (42), `post_embed` and `post_flair_list`.

## THE PRODUCTION CHANGES

### The embed pages had none of the post page's access checks (D1089)

`/post/<id>/embed` **is** the post, in a frame, and `/post/<id>/embed_code`
names it in its title, its breadcrumbs and the snippet it hands out. Neither
made the private-community test, and neither made the unpublished test that
slice B added to `show_post`. Measured:

```
PROBE ai1 embed anon: 200 | body: True | title: True
PROBE ai2 embed_code anon: 200 | title: True
PROBE ai3 embed scheduled: 200 | body: True
```

Fact 478 for the fourth time in three slices. The unpublished test is now a
helper, `refuse_unpublished_post(post)`, and `show_post` was changed to call it
rather than keep its own copy — the same reason `refuse_private_community`
became one in slice A: **four copies is how this keeps happening**.

### A GET that cleared a post's flair and its NSFW mark (D1090)

`post_set_flair` accepts GET and POST. Its htmx branch is entered on the
`HX-Request` header alone and **writes**: it rebuilds `post.flair` and sets
`nsfw`, `nsfl` and `ai_generated` from `request.form` — which a GET does not
carry, so every one of them became `False` and the flair list became empty.
Measured, against a post that went in marked nsfw and flaired:

```
PROBE aj1 GET set_flair status=200 | nsfw now=False | flair now=[]
```

D955's shape. The detector in `tests/test_mutating_get_routes.py` did not see
it: the view *does* contain `form.validate_on_submit()`, further down, and that
is the heuristic for "a form gates the write". The htmx branch returns before
reaching it. The branch now requires `request.method == 'POST'`; GET still
renders the form.

### `HX-Current-Url` read straight into an `in` test (D1091)

`post_flair_list` and `post_set_flair` both did `curr_url =
request.headers.get("HX-Current-Url")` and then `if "/post/" in curr_url`. A
request without the header was

```
TypeError: argument of type 'NoneType' is not iterable
```

— `app/post/routes.py:1736`, measured. D992's shape: a 500 where a page was
possible.

### A reply grafted onto another community's conversation (D1092)

`add_reply_inline` tests every permission against `post.community` and then
attaches the new reply to `in_reply_to`, with nothing relating the two.
Measured:

```
PROBE ak1 status=200 | replies made=1 | child post_id=1 parent post_id=2
```

A reply was created under a **public** post whose parent is a comment in a
**private** community's post. D1077's family, ninth site, and the only one that
writes new content across the boundary rather than reading across it.

### Two smaller ones in the same function

* **D1093** — `language_id = int(request.form.get('language_id'))`. A form
  field is whatever the caller sends; absent is `TypeError: int() argument
  must be a string, a bytes-like object or a real number, not 'NoneType'`
  (`app/post/routes.py:1005`, measured). It now falls back to the account's
  language and then the site's.
* **D1094** — the `PostReplyValidationError` arm returned
  `'<div id="reply_to_{comment_id}" ...'` **without the `f`**, so the id was
  the literal string, htmx had no element to swap, and the refusal was
  rendered into nothing.

### Two more, found by writing the rows rather than by probing

* **D1095** — the NSFL arm set `form.nsfw.render_kw`, one line below the NSFW
  arm it was copied from, so an NSFL community left its own box editable and
  locked the wrong one.
* **D1096** — the community's forced NSFW/NSFL values were set **above** the
  repopulation that overwrites them with the post's, so the disabled box
  rendered unchecked: the page said "not NSFW" for a post in a community where
  NSFW is not optional.

Both were found by a row asserting what the page should show, not by a probe —
the first round in this sub-project where writing the assertion was what
turned them up.

## Success criteria

- The seven functions at `[]` on the **full-suite** run.
- D1089–D1096 land with their pins inverted (15 rows red without them).
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1089**; `tests/README.md` facts from **515**.
- 23 mutants, **all killed on the measuring pass** — the second clean pass of
  the campaign.
