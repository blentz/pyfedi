# Sub-project 80 slice B: the moderation tools that are not banning — and a class of cross-community confusion

**Status:** slice B complete. EIGHT production defects fixed (D969-D976); the
design anticipated four. See the findings ledger, D969-D979.
**Branch:** `blentz`, at `fb5f5eaef`
**Predecessor:** slice A, which fixed D955-D959 and added the banned-user
ratchet. **64 floors.**

## Scope

The wiki, flair and report handlers:

| function | gaps |
|---|---|
| `community_wiki_view_revision` | 36 |
| `community_wiki_view` | 35 |
| `community_wiki_edit` | 28 |
| `community_flair_edit` | 28 |
| `community_moderate_report_resolve` | 26 |
| `community_moderate_report_ignore` | 22 |
| `community_wiki_add` | 18 |
| plus `community_moderate_report_escalate`, `community_flair_delete`, `community_wiki_revert_revision`, `community_wiki_list`, `community_wiki_revisions`, `community_flair` | |

## THE PRODUCTION CHANGES

### The class: a child resource fetched by bare id, authorized through a parent from the URL

Five routes take two ids — a community (or actor) and a resource — check the
caller's authority over the **community**, and then act on the **resource**
without ever asking whether the two are related. So a moderator of any
community on the instance can act on any other community's wiki pages and
flair, by passing their ids.

Measured, as a moderator of `mine` acting on resources owned by `theirs`:

```
PROBE f1 flair belongs to community 2   mod moderates 1
PROBE f1 status 302 | other community's flair still exists? False
PROBE f2 status 302 | other community's flair text now: HIJACKED
PROBE w1 status 200 | other community's wiki body now: HIJACKED
```

### P1 — `CommunityWikiPage.can_edit` never looks at its own community (D969)

```python
def can_edit(self, user: User, community: Community):
    if self.who_can_edit == 0:
        if user.is_admin() or user.is_staff() or community.is_moderator(user):
```

`community` is an argument and `self.community_id` is never consulted, so the
method answers "may this user edit **some** page of **that** community" — a
different question from the one every caller is asking. Seven call sites, four
of them routes.

**Fix in `can_edit`, not in the routes**: one implementation of the question,
which is fact 368's rule and what D924 cost when it was ignored.

### P2 — the flair routes fetch by bare id (D970)

`community_flair_edit` does `db.session.get(CommunityFlair, flair_id)` and
`community_flair_delete` deletes `CommunityFlair.id == flair_id` outright —
neither constrains `community_id`. The delete also cascades to `post_flair`,
`rss_feed` and `CommunityFlairBlock` rows belonging to the other community.

### P3 — the wiki revision routes fetch the revision by bare id (D971)

`community_wiki_view_revision` and `community_wiki_revert_revision` both scope
the **page** correctly (`filter_by(slug=slug, community_id=community.id)`) and
then fetch the **revision** with `db.session.get(CommunityWikiPageRevision,
revision_id)`, never checking `revision.wiki_page_id == page.id`. Viewing
discloses another page's content; reverting **writes it into this page**.

### P4 — eight more routes still have no banned check (D972)

`community_wiki_add`, `community_wiki_edit`, `community_wiki_revert_revision`,
`community_moderate_report_escalate`, `community_moderate_report_resolve`,
`community_moderate_report_ignore`, `community_flair_edit` and
`community_flair_delete`.

### P5 — slice A's ratchet does not catch P4, and that is the important part (D973)

`test_no_state_changing_route_answers_a_banned_user` flags a rule only when it
answers **200**. Every route above redirects on success, so the ratchet passed
while eight routes were unguarded — it was measuring the response, not whether
the work happened.

**Fix:** fingerprint the state a banned user's request could change, and assert
it is identical afterwards. Row counts alone are not enough, because these
routes UPDATE as much as they INSERT; the fingerprint takes the contents of the
tables this blueprint writes. `user.last_seen` is excluded and named, because
`app/request_hooks.py:110` updates it on every request.

A ratchet that cannot fail for the defect it was written for is worse than no
ratchet, because it is also a claim.

## What the slice found beyond the design

- **D974** -- all three report handlers fell off the end without returning, so
  a non-moderator got a 500 instead of a 401 and an already-handled report was
  a 500 on all three.
- **D975** -- "Ignore" never marked the report it was given; a report about a
  USER stayed `REPORT_STATE_NEW` forever.
- **D976** -- "Ignore" has no form and accepted GET. D955's shape, one slice
  later. Escalate and Resolve are safe as GET links because both render a
  confirmation form first; this one never did.
- **D973** -- the strengthened ratchet found a ninth unguarded route,
  `community_report`, which has no authorization construct at all and so was
  invisible to the survey that found the other eight.

## Success criteria

- The twelve functions at `[]` on the **full-suite** run.
- No floor yet on `app/community/routes.py`.
- P1-P5 land with their pins inverted, including a row per cross-community
  pairing.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D969**; `tests/README.md` facts from **406**.
