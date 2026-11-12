# gpodder.net sync for podcast apps (plan W4)

Plan `docs/superpowers/plans/2026-11-07-api-completeness.md`, W4. Podcast apps (AntennaPod, Kasts, gPodder) keep a
person's subscriptions and listening position in step across their devices through a gpodder.net server. PieFed
serves that API, so a member uses their PieFed account as the sync server, and a PieFed podcast community they follow
in the app is a community they have joined.

## 1. Rulings (owner, 2026-11-12)

- **G1 login: app passwords.** A person makes a named sync password in settings, shown once, revocable. The account
  password is never taken: it works for accounts with no password (passkey, OAuth sign-up), and revoking one app
  revokes only that app.
- **G2 subscriptions: the list, and joining communities.** The uploaded feed URLs are kept per person for syncing.
  A URL that is a PieFed community's own feed also joins that community, and its removal leaves it.
- **G3 episode actions: kept, and a play marks a post read.** Actions are kept per person for syncing. A `play` of an
  episode that is a PieFed post marks that post read for the person.
- **G4 API: gpodder.net v2 only.** No Nextcloud gpoddersync.

## 2. What the apps call (spike, AntennaPod `net/sync/gpoddernet/GpodnetService.java`)

| Call | Body / query | Answer |
|---|---|---|
| `POST /api/2/auth/{username}/login.json` | HTTP Basic | 200, and a `sessionid` cookie |
| `POST /api/2/auth/{username}/logout.json` | | 200 |
| `GET /api/2/devices/{username}.json` | | `[{id, caption, type, subscriptions}]` |
| `POST /api/2/devices/{username}/{device_id}.json` | `{caption?, type?}` | 200 |
| `GET /api/2/subscriptions/{username}/{device_id}.json?since=T` | | `{add, remove, timestamp}` |
| `POST /api/2/subscriptions/{username}/{device_id}.json` | `{add, remove}` | `{timestamp, update_urls}` |
| `GET /api/2/episodes/{username}.json?since=T` (`podcast`, `device`, `aggregated` optional) | | `{actions, timestamp}` |
| `POST /api/2/episodes/{username}.json` | `[{podcast, episode, guid?, action, timestamp?, started?, position?, total?, device?}]` | `{timestamp, update_urls}` |

AntennaPod sends Basic only to `login.json` and carries the cookie after it (an in-memory jar, a new login each sync).
Kasts and gPodder send Basic on every call. Every call therefore takes either.

## 3. Login (G1)

- **An app password is an OAuth token** of an app the person names, made from settings: `/user/settings/apps` gains a
  "New app password" form (a name, up to 50 characters). It registers an OAuth app of that name with no redirect URI
  and issues it a token with no refresh token and the scope `gpodder`, which the page shows once. The token is stored
  hashed as every token is (`app.oauth_server.tokens`), listed on the same page as an authorised app, and revoked by
  its button. A password change ends it, as it ends every token (`usable_token`).
- **The scope `gpodder` is reached by nothing else.** It is not in `app.oauth_scopes.SCOPES`, so no OAuth client can
  ask for it, and no Mastodon, PeerTube or Lemmy route covers it, so an app password is no Bearer token anywhere.
- **Basic:** the user part is the person's user name (case ignored), the password an unrevoked usable `gpodder` token
  of theirs on this domain. The `{username}` in the path must be the same person, or the answer is 401 (gpodder.net's).
- **The session cookie** `sessionid` (HttpOnly, Secure off an onion, SameSite=Lax, path `/api/2/`) is the token's id
  signed with the app's secret (itsdangerous, salt `gpodder-session`), good for a day. Each request re-reads the token,
  so revoking it ends the session at once. `logout.json` deletes the cookie.
- **Failures** answer 401 with `WWW-Authenticate: Basic realm="PieFed"`, and are counted per address (Flask-Limiter's
  key, onion requests included) and account named in the path: past 20 in an hour from the first, every sign-in
  from that address at that account is refused 429
  with `Retry-After`, checked before a password is tried, so none is guessed rapidly (owner 2026-11-12). Good calls
  are never counted, as Kasts and gPodder send Basic on every one. By account too, since onion clients without a
  session share one address key: guessing at one account locks out that account alone (security review 2026-11-12). A Redis counter of its own, not `@limiter.limit`,
  which the onion guard allows only on routed views (tests/test_onion_guard_limits.py). A private instance's login gate leaves these routes to their own check
  (`has_own_login_gate`), as `require_oauth` does.

## 4. Devices and subscriptions (G2)

- **Devices** are rows per person (`gpodder_device`: user, device id up to 64 of `[\w.-]`, caption, type, last
  seen). A device is made by its first configure or subscription upload. `type` is one of gpodder.net's (desktop,
  laptop, mobile, server, other).
- **Subscriptions are the person's, not a device's.** Every device of a person reads and writes one list, as a
  gpodder.net sync group holding all of them: an app on a new phone sees what the old one subscribed to.
  `gpodder_subscription` holds (user, URL, subscribed, changed at); a removal keeps the row with subscribed false, so
  `since` can report it. At most 2000 URLs per person, each an http(s) URL up to 2000 characters, and as many
  removals kept, the oldest pruned beyond that, so adding and removing ever new URLs grows nothing without bound
  (security review 2026-11-12). An upload lists at most 2000 of each.
- **Upload** sanitises each URL as gpodder.net does (trimmed; not http(s) is dropped and reported in `update_urls` as
  `[url, ""]`), applies removes after adds, and answers the server time as `timestamp`. A URL in both is refused 400.
- **Pull** answers the URLs added and removed since `since` (seconds, inclusive: a repeat is harmless, a miss is not),
  and the server time.
- **Joining (G2):** an added URL that is a community's own feed here joins it for the person through
  `app.shared.community.join_community(community_id, SRC_API, Authorised(user))`, as PeerTube subscriptions do (bans,
  deleted communities and federation as there). A removed one leaves it through `leave_community`. A community's own
  feed is `https://<any host this server answers>/community/<actor>/feed`, where `<actor>` names a community the person
  may see (`actor_to_community`, not private, not hidden here); a refusal of the join (a ban), or a moderator's
  leave, is logged and the URL is kept in the list, so syncing never fails for it. Joins made on the web do not flow
  back into the list: the list is what the apps put there.

## 5. Episode actions (G3)

- **Kept** in `gpodder_episode_action` (user, podcast URL, episode URL, guid, action, device, timestamp as the app
  sent it, started, position, total, received at). Actions are `download`, `delete`, `play`, `new` (gpodder.net's
  `flattr` is dropped). Only the newest per (user, episode URL, action) is kept: each upload replaces the older one,
  which is what an app syncing a position reads; an older one sent late changes nothing. At most 20000 kept per
  person, the oldest received pruned, and at most 1000 actions per upload.
- **Pull** answers the actions received since `since`, narrowed by `podcast` and `device` if given; `aggregated=true`
  answers only the newest action per episode. Answered in gpodder.net's shape (timestamps `YYYY-MM-DDTHH:MM:SS`, UTC).
- **Marking read (G3):** a `play` whose episode is a post here marks it read for the person
  (`app.shared.post.mark_post_read`). The episode is a post when its `guid` is a post's ActivityPub id (our feed's
  `<guid>` is `post.profile_id()`), or its URL is a post's media URL (our feed's `<enclosure>` is the stored
  `post.url`, shown on the storefront's host). Only a post the person may read (`app.mastodon.timelines.readable`)
  is marked.

## 6. Out of scope

- gpodder.net's directory, suggestions, tags, toplists, settings, favorites and device sync groups: no app of the
  three needs them for sync. Any other `/api/2/` call answers 404.
- Nextcloud gpoddersync (G4).
- Web pages listing the synced list or actions: the apps are their view.

## 7. Phases

- **W4a** app passwords: the scope, the settings form, Basic and the session cookie, `login.json` and `logout.json`.
- **W4b** devices and subscriptions, with joining and leaving (G2).
- **W4c** episode actions, with marking read (G3).
- **W4d** the harness: AntennaPod's driver logs in to PieFed's gpodder sync with an app password, subscribes to the
  seeded podcast community's feed, and PieFed has it joined; its `login` step stops being inapplicable.

Each phase is TDD, one commit per defect, with the full gates; two cold harness runs of 21/21 before asking to merge.
