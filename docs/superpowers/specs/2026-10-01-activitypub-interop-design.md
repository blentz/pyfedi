# ActivityPub interoperability: Mastodon, PeerTube, Pixelfed, Castopod

Status: decisions locked (grill session 2026-10-01). Fork-only work.

## Goal

Full cross-compatibility with Mastodon, PeerTube, Pixelfed and Castopod. "Full" means:

- (a) rendering parity: their content renders well here, ours renders well there;
- (b) wire parity: every activity they emit with a PieFed analogue is ingested, and every PieFed activity they can represent is emitted correctly;
- (c) feature parity: their native features have a PieFed equivalent.

(b) is complete. (a) and (c) are scoped to an MVP; everything past the MVP is iterative post-MVP feature development, enabled by a UI/UX extension mechanism.

## Decisions

### Analysis

- **D1 Scope.** Analyse each platform's wire protocol and UI/UX feature matrix; record gaps, differences and alternative UI/UX choices. Build an extension mechanism so (a) and (c) can grow after the MVP.
- **D2 Source of truth.** Each platform's latest release or development HEAD source code. Captured live JSON fixtures prove each matrix cell; a cell without a fixture is unverified. `FEDERATION.md` files are hints only. One matrix file per platform under `docs/`.
- **D3 Release vs HEAD.** The latest release is the contract. HEAD-only changes get a "pending upstream" note and are built when they reach a release candidate (or earlier by choice). Each matrix file pins the release tag and HEAD commit. A scheduled drift job diffs federation-relevant upstream paths and opens a review ticket on change.

### Content model

- **D4 Person-centric content.** PeerTube channels and Castopod podcasts map to communities. Mastodon and Pixelfed people get a Following feed and profile timelines; their posts stay stored in the synthetic `microblogs` community (no nullable `community_id`). MVP rendering fix: titleless posts render by body, no auto-generated title.
- **D5 Outbound shape.** One canonical object per activity, never varied by recipient (one `id`, relays, refetch, LD signatures). The per-platform matrix drives which fields the canonical object fills so every renderer has something good to show.
- **D6 Visibility storage.** New `visibility` field on `Post` and `PostReply`: `public | unlisted | followers | direct`, filled by the existing object-level classifier `activitypub_visibility` (`app/activitypub/util.py`). Current state: `create_post`, `create_post_reply` and the reply backfill refuse `followers` and `direct` at ingest, so nothing leaks today, but the D4 Following feed would miss followers-only posts. `Post.private` is a microblog "unlisted" marker derived from activity-level addressing, and `PostReply.private` uses a separate `to[0]` rule; both derivations are replaced by `visibility`. The ingest refusal is lifted only after D7 enforcement ships. (D1438, suspected as a leak during the session, was closed as not a defect.)
- **D7 Visibility enforcement.**
  - Unlisted: hidden from All, Popular, community listings and search; shown in the Following feed and the author's profile.
  - Followers: visible only to logged-in local followers of the author; 404 on HTML and ActivityPub URLs for everyone else; excluded from the API, RSS and the sitemap.
  - Direct: becomes a PieFed chat for local recipients; remote participants shown read-only. Full multi-instance DM threads are post-MVP.
  - Outbound: local users post public content only in the MVP.

- **D18 Hidden replies in a thread.** A reply the viewer may not see renders as a placeholder ("Visible to followers only") with no author, body or score. Its visible children stay in the tree. The API returns a stub with `visibility: "followers"` and null content. ActivityPub collections omit hidden replies.
- **D19 No moderator exemption.** Admins, staff and community moderators see followers-only content only through the report queue, which snapshots the reported body. Everywhere else they get the same placeholder or 404 as any other viewer.

- **D20 Replies under hidden posts (execution ruling, 2026-10-02).** A reply is judged by its own visibility, so a public reply under a followers-only post stays visible. Every parent-post field shown with it is neutral unless the viewer can view the parent: the API embedded post, the web "reply to" line and the NNTP subject.
- **D21 Stub shape (amends D18 for the API).** Hidden-reply stubs keep Lemmy's CommentView nesting. `body` is null, `visibility` is `"followers"`, and `creator`, `counts` and `post` are neutral non-null objects with no content, so typed clients don't fail on nulls.
- **D22 Enforcement is not seeing (amends D19).** Moderator and admin removal actions skip the visibility gate. Views stay gated. Removal acknowledgements, the delete-confirmation page and modlog entries for non-open content are neutral to anyone who can't view it. The modlog keeps the target user, and shows it only to admins for non-open entries.
- **D23 Mentions.** Mention notifications obey the visibility predicate. Follow-up: store each object's addressees and add an addressee arm to the predicate, because Mastodon shows followers-only posts to the people they mention.

- **D24 Discovery seeding and Castopod podcasts as communities (2026-10-03).** A new instance gets routes to Mastodon, Pixelfed, PeerTube and Castopod: an admin pre-load of PeerTube channels and Castopod podcasts, and a search index of those plus opt-in Mastodon/Pixelfed people, fed by public directories (SepiaSearch, index.castopod.org (amended 2026-10-05 from Podcast Index), joinmastodon + per-server directories, FediDB + Pixelfed directories). A podcast is a community, an episode is a post, and the authors shown are the podcast's hosts and per-episode guests (credits from RSS `podcast:person`). Suggested follows for users are future work. Design: `docs/superpowers/specs/2026-10-03-discovery-seeding-design.md`.

### Extensibility

- **D8 Content kind.** The unit of extension is a content kind, one registered bundle of:
  1. an ingest mapper (object to Post fields plus a typed `extensions` JSON column);
  2. a web renderer (one context object, falls back to the `post_type` macro);
  3. an API representation (optional field; core fields always carry a usable fallback);
  4. an outbound filler (adds fields to the canonical object, per D5).

  Core Mastodon, Pixelfed, PeerTube and Castopod support is built as built-in kinds on this mechanism.
- **D9 Trust boundary.**
  - Each kind declares an `extensions` schema, validated at ingest. HTML fields go through `allowlist_html`. URL fields are `https` only and checked against domain blocks. Invalid data is dropped and logged, and the post falls back to core fields.
  - Renderers run under strict autoescape. A lint check forbids `|safe` on extension data.
  - All iframes go through one core helper with a sandbox and an origin allowlist.
  - Third-party plugins may register kinds.
  - Most specific matcher wins; ties go to core; collisions are logged.
- **D10 MVP cut.** Primary content unit plus primary interactions, in both directions:

  | Platform | MVP | Post-MVP |
  |---|---|---|
  | Mastodon | Note with media, CW (`summary`) as a collapse, polls (`Question`), custom emoji, hashtags, mentions, boosts, quote posts with the 4.4+ quote-authorization flow | Profile fields and verification, featured hashtags, pinned posts, edit history view |
  | Pixelfed | Multi-image album carousel, alt text, sensitive blur | Stories, collections, Pixelfed Groups |
  | PeerTube | Embedded player through the core iframe helper, channel as community, threaded comments, likes and dislikes | Live streams, playlists, chapters, view counts |
  | Castopod | Episode with native `<audio>` player and show notes, podcast as community, comments | Chapters, transcripts, soundbites, `podcast:` value/funding tags |

### Wire trust and platform detection

- **D11 Forwarded activities.** A forwarded `Create`, `Update` or `Announce` without a valid actor signature is refetched from origin:
  - the object `id` host must equal the actor host;
  - the object is fetched with a signed GET, and everything received inline is discarded;
  - the fetched `attributedTo`/`actor` must match the claimed actor;
  - fetches are rate-limited per forwarding host.

  `Delete`, `Undo`, `Block`, `Flag` and `Like` require the actor's own valid HTTP or LD signature. FEP-8b32 support comes later. This replaces the commented-out fallback in `app/activitypub/routes.py`.
- **D12 Dispatch.** Kind matchers dispatch on object shape only (`type`, `@context` terms, attachment structure, media types), never on `software` or host. `Instance.software`/`version` are allowed only in one registry of named, versioned workarounds, each recording the upstream bug or version it covers. The 24 existing `software` branches and the D478 Pixelfed host checks move into the registry or become shape checks.

### Moderation

- **D13 Moderation.**
  - Only instance admins moderate `microblogs`.
  - Reports we send go to local admins, plus an optional Mastodon-shaped `Flag` (`object: [actor, status…]`) to the author's server, sent from the instance actor.
  - Flags we receive go to admins, and also to community mods when the flagged post is in a local community. Account-only Flags go to admins only.
  - A received user `Block` hides content from the blocking side. A received domain block has no effect.

### Testing

- **D14 Two test layers.**
  1. Contract tests in pytest, blocking PRs. Every matrix cell is a fixture plus a test, marked with `@pytest.mark.interop(platform=..., activity=..., dir=in|out)`. Inbound tests assert on the stored Post, `extensions` and `visibility`; outbound tests use golden snapshots. The matrix file is generated from the markers.
  2. A nightly docker-compose interop harness with PieFed and all four platforms, at the pinned release tags and at HEAD, running scripted scenarios in both directions. Release failures open defects; HEAD failures feed the pending-upstream queue. Captured traffic refreshes the fixtures.

### Rollout and fork maintenance

- **D15 Order.**
  - **Phase 0, foundations:** matrix and drift job, visibility (enforcement before the ingest refusal is lifted), kind registry, `extensions` column, iframe helper, lint check, workaround registry, forwarded-activity refetch, test markers and the harness skeleton. All schema migrations land here.
  - Then **Phase 1** Mastodon, **Phase 2** PeerTube, **Phase 3** Pixelfed, **Phase 4** Castopod.
  - Each built-in kind gets an admin toggle, off by default for one release and then on. A disabled kind falls back to core rendering.
- **D16 Fork-only.** No upstream RFC. Maintained through a sync/rebase process.
- **D17 Rebase hygiene.**
  - New code lives in new packages (`app/kinds/`, `app/interop/workarounds.py`, `tests/interop/`), with thin seams in upstream files: Post creation, the teaser macro and the API serializer.
  - Fork migrations go on their own Alembic branch label, merged with upstream heads at each sync.
  - Public names are namespaced: API additions go under an `extensions` key, and outbound ActivityPub fields use a fork-owned context or existing FEP/vendor terms (`pt:`, `podcast:`, `toot:`), never bare keys.
  - Every rebase runs the contract suite and one harness pass before merging.

## Open items for the analysis phase

- Castopod actor and episode object shapes (its federation docs were not at the expected paths).
- Mastodon quote-authorization flow details at the release tag.
- PeerTube forwarding cases that D11 does not cover.
