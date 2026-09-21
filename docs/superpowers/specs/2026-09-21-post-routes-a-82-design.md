# Sub-project 82 slice A: the comment routes of `app/post/routes.py`

**Status:** slice A complete. TWO production defects fixed (D1077, D1078).
**Branch:** `blentz`
**Predecessor:** sub-project 81, which closed `app/user/routes.py` and took its
floor. **66 floors, unchanged this round** — `app/post/routes.py` is 14.5% at
the start of this sub-project and its floor is taken when the module closes.

## Scope

The eight routes that carry `/post/<post_id>/comment/<comment_id>`: the comment
thread and its ajax twin, the reply options menu, the report form, the edit
form, delete, restore, and the reply form's `in_reply_to`.

## THE PRODUCTION CHANGES

### Two ids in one URL, with nothing tying them together (D1077)

Every one of those eight routes fetched `post` and `post_reply` independently
and never asked whether the reply belongs to the post. Each then made its
authorization test against **`post.community`** — the object the caller chooses
freely — and acted on **`post_reply`**.

So a moderator of any community could restore a deleted reply in another one,
undoing that community's moderators' removal, by pairing their own post id with
its comment id. Measured:

```
PROBE ac2 status: 302 | their reply deleted: False
```

(True before the request, False after.) The delete twin had the same mismatch,
where it surfaced as `Exception: Does not have permission` from `delete_reply`
— a 500 rather than a cross-community deletion, and only because the helper
happened to notice what the route did not.

Fact 447's shape for the third time in this campaign, after D1010 (the RSS
importer's repoint and delete) and D1029 (the per-community theme flip). All
eight sites now check `post_reply.post_id != post.id` before anything else.

### The private-community check the comment pages never made (D1078)

`show_post` refuses a private community's post to anyone who is not a member.
The routes that show the **same content one comment at a time** made no such
check at all. Measured:

```
PROBE ae1 status: 200 | private body visible: True
PROBE ae2 status: 200 | private body visible: True
PROBE ae4 anon status: 200 | private body visible: True
PROBE ae5 status: 200 | private body visible: False | title visible: True
```

ae4 is the worst of them: **no account at all** was needed to read a private
community's conversation. ae5 is the report form, which names the post it is
about, so a non-member learned a private community's post title from it.

This is fact 478 again — the same feature has two ends, and only one of them
was guarded — and the **sixth** surface in this campaign through which
private-community content escaped, after D998, D1005, D1017, D1026 and D1057.

The fix is one helper, `refuse_private_community(post)`, holding the test
`show_post` already makes, called at the four read surfaces
(`continue_discussion`, `continue_discussion_ajax`, `post_reply_options`,
`post_reply_report`). A helper rather than four copies, because four copies is
how D1078 happened.

### How D1078 was found

D1077's probe paired a public post's id with a **private** community's comment
id and watched the private body render. Reading the route afterwards to write
the D1077 row showed the mismatch was not what let it through: there was no
private-community test on that route at all, so the matched-ids case leaked
too. The second probe (ae) measured it directly.

## What the coverage found without a defect

* `make_post_reply` leaves `body_html` unset, and the reply teaser template
  runs the stored HTML through `community_link_to_href`, which answers
  `TypeError: expected string or bytes-like object, got 'NoneType'`. Any row
  that renders a thread has to set it (fact 501).
* `continue_discussion` sets cache headers on what `render_template` returns,
  so a patch that answers a bare string is
  `AttributeError: 'str' object has no attribute 'headers'` (fact 502).
* `languages_for_form` skips code `'und'`, so a form row that has to satisfy
  `language_id`'s `DataRequired` needs a real language row as well (fact 503).
* `post_reply_options` is `@block_bots` only — no `@login_required` — and
  refuses an anonymous visitor a deleted reply's menu but serves it to any
  logged-in reader, because that is where the restore link lives (fact 504).

## The mutation pass

Two passes, 24 mutants; 20 killed on the measuring passes. `was_mod_deletion` survived because
it is only observable in the **federated** payload (D1079); the report page's
`reports == -1` warning survived because the route tests the same column twice
and a submit-only row kills neither (D1080); `Label(field_id=...)` is an
equivalent mutant, because `render_form` takes `for` from `field.id` (D1081,
fact 505). The second pass, over the rows that closed the remaining gaps, left
two more: no row had a length-1 `path`, so the ancestor `child_count` UPDATE's
guard was never exercised (D1082), and the poll row created its choices in sort
order, so dropping the `order_by` changed nothing (D1083 -- **fact 499 for the
second time in two sub-projects**). Three rows added, one equivalent mutant
recorded.

## Success criteria

- The eight routes' rows at `[]` on the **full-suite** run.
- D1077 and D1078 land with their pins inverted (12 rows red without the
  guards).
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1077**; `tests/README.md` facts from **501**.
- Nine functions at `[]`: the eight comment routes plus `refuse_private_community`.
