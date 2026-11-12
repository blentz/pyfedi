# Client-app harness

Checks that third-party apps of the five platforms PieFed interoperates with (Mastodon, Lemmy, Pixelfed, PeerTube,
Castopod) can log in to PieFed, view its content and post to it. It drives the real apps, each the newest release its
project publishes when the run starts, against the interop stack's PieFed (`piefed.test`, built from this tree).
It is optional and local, like `interop/run.sh test`: not part of the unit suite, not wired to CI.

## Run

    interop/run_remote.sh apps          on the remote podman host (remote-gpu), which has KVM; results come back here
    interop/run_remote.sh apps -k web   pytest arguments pass through (here: the web apps only)
    interop/run.sh apps                 on this machine, if it has KVM and the memory

The run brings up the PieFed half of the stack only (Caddy, PieFed's database, Redis, web and Celery), the three web
apps and an Android emulator, then runs `interop/apps/` in the `apps-runner` container. The verdict is
`python3 -m interop.apps.matrix`, which exits 1 on any difference from `matrix.json`. A run takes about ten minutes
from cold (building the images), two or three on a warm stack. `interop/run_remote.sh` needs the remote host to answer
ssh with podman, python3 and tar; it installs podman-compose into a venv there.

Results land in `interop/apps/results/` (gitignored): one JSON per app, `summary.md` (the table below, for this run),
`images.txt` (which build of each web app ran) and a screenshot of any step that failed.

## What runs

| Platform | App | How | Steps |
|---|---|---|---|
| Lemmy | Voyager, Blorp, Photon (web) | Chromium (Playwright) on the app's own image | log in as alice, open `c/apps` and see the seeded post, comment on it |
| Lemmy | Voyager, Thunder, Summit (Android) | the newest APK on the emulator (uiautomator2) | log in, find the seeded post with the app's search, comment on it |
| Lemmy | Jerboa (Android) | the newest APK on the emulator | log in as alice (nodeinfo tells it Lemmy, ruling D3), find the seeded post in her home feed, comment on it |
| Mastodon | Mastodon, Tusky, Moshidon (Android); Phanpy, Elk (web) | protocol probe | |
| Pixelfed | Pixelfed (Android) | the newest APK on the emulator (its native libraries extracted) | log in as alice (Pixelfed's config path tells it Pixelfed, ruling X1), open dora's photo from her profile's grid, post a photo through its camera, message dora and read her answer, send her a photo, and see dora's story in its carousel |
| Pixelfed | PixelDroid (Android) | the newest APK on the emulator | log in as alice (nodeinfo tells its agent Pixelfed, ruling S6), open dora's photo from her profile's grid, post a photo and publish a story through its Create tab (S5) |
| Pixelfed | Fedilab (Android) | the newest APK on the emulator | log in as alice (it speaks Mastodon's API to PieFed), find the seeded post with its search, reply to it |
| PeerTube | NewPipe (Android) | the newest APK on the emulator | add `tube.test` as its PeerTube instance, pick it in the service picker, and play the video tess uploaded there |
| PeerTube | PeerTube (Android) | the newest APK on the emulator | add `tube.test`, log in as tess with her password, open her video from the platform's page and comment on it, upload a clip to her channel through the app's own screens and Android's photo picker |
| PeerTube | TubeLab (Android) | protocol probe | it reads nodeinfo first and logs in only where that says `peertube` (ruling P4: left out) |
| Castopod | AntennaPod (Android) | the newest APK on the emulator | log in to PieFed's gpodder sync with an app password (plan W4), subscribe to `pod_media`'s feed by RSS address, see its episode, and find the feed in alice's synced list |
| Castopod | Pocket Casts, Podcast Addict | the feed itself | what a player needs from it: an item with an audio enclosure, its length and duration |

`tube.test` is a second, public domain of the same PieFed (multi-domain storefronts, seeded by
`interop/seed_piefed.py` with tess and her video community `tube`). `piefed.test` is private (R219), and an app with
no account can browse only a public domain.

Every comment an app posts carries a marker unique to the app and the run, and is looked for afterwards through
PieFed's own API: the app saying it posted is not enough.

The stack is shared: after `interop/run.sh test` or `conformance` PieFed holds their Loops, Mastodon and Pixelfed
posts too, and a feed's first screen shows those. So the Android apps find the seeded post by searching for it, as on
a busy instance, rather than expecting it on a feed's first screen (the web apps open `c/apps` directly by URL).
Taps that an app can swallow while it starts or while its keyboard animates are retried a bounded number of times
(`Device.tap_until`), and tabs are tapped as the lowest element with that label (`Device.tap_tab`), never by a
substring that a feed post could also contain.

**Protocol probes** (`probes.py`) stand in for the apps whose client API PieFed does not serve. Driving those apps'
UIs would only show an error screen, so a probe sends the request the app's login and timeline start with (Mastodon's
`POST /api/v1/apps` and `GET /api/v1/timelines/public`, PeerTube's `GET /api/v1/oauth-clients/local` and
`GET /api/v1/videos`, Lemmy's `POST /api/v3/user/login` and `GET /api/v3/post/list`) and records PieFed's answer.
While those answer 404 the matrix expects `unsupported`; when one starts to work the gate fails with "now passes", and
that app is due a real driver. `tests/test_apps_probes.py` runs the same probes against this tree in the unit suite.

## The matrix

`matrix.json` names every app, its platform, how it is checked, and what it is expected to do. A step with no
expectation must pass. An expectation records a known non-pass and why:

- `unsupported`: PieFed lacks the API the app speaks (Mastodon's, PeerTube's, Lemmy's v3).
- `blocked`: not tried, because an earlier step did not pass (posting without a login).
- `inapplicable`: the app has no such step (a podcast player neither logs in nor posts).
- `failed`: a known failure, with its reason, until it is fixed.

The gate fails when an app did not run, when a step's outcome differs from its expectation (including a non-pass that
now passes), and when an expectation has no reason. `tests/test_apps_matrix.py` checks the gate and the committed
matrix in the unit suite.

## Adding an app

1. Add it to `matrix.json`: `web` (a URL served in the stack, plus its `image`), `apk` (`{"fdroid": "<package>"}`, or
   `{"github": "<owner>/<repo>", "asset": "<regex>"}`, with `"prerelease": true` to take prereleases) with its
   `package`, `probe` (`mastodon`, `peertube` or `lemmy-v3`) or `rss`.
2. For a web app, a service in `compose.interop.yaml` (profile `apps`), its name in Caddy's aliases and
   `interop/Caddyfile`, and its image in `run.sh`'s `WEB_IMAGES` and `APPS_SERVICES`.
3. For a driven app, a driver class in `test_web_apps.py` or `test_android_apps.py`: `login`, `view` and `post`, each
   raising on failure. Find controls by accessible name (web) or by resource id, text or content description
   (Android); a screenshot of a failing step lands in `results/`.

## The emulator, and why it is set up as it is

`android/Containerfile` and `android/boot.sh`: Android 17 (API 37) Google APIs x86_64 under KVM. Each choice answers a
failure met while building the harness:

- **Android 17, not older.** Voyager's Android build refuses a WebView older than Blink 140; the Android 13 image's is
  109 and the Android 16 image's 134. Android 17's is 145.
- **The harness CA as a system CA.** Apps ignore user CAs, so `boot.sh` (as root) lays a tmpfs copy of the system
  store, plus Caddy's root, over `/system/etc/security/cacerts`, then bind-mounts it over the conscrypt APEX's
  `cacerts` inside zygote's mount namespace, so every app started afterwards trusts it.
- **No CT log list.** From Android 17, apps targeting it enforce Certificate Transparency on certificates that chain to
  a system CA, and the harness CA's have no SCTs (Summit: `NOT_ENOUGH_SCTS`). Its switches are read-only flags; the
  documented off state is a device with no log list, so `boot.sh` deletes the downloaded one and blocks its directory.
  A real instance's publicly logged certificate needs none of this.
- **`-gpu guest`.** The emulator's host-side renderer killed the emulator: `swiftshader_indirect` segfaulted it when
  Summit drew its first frame, and `swangle_indirect` hit a deliberate trap in gfxstream's GLES decoder when Thunder
  (Flutter) did. Rendering in the guest avoids gfxstream entirely.
- **x86_64 APKs.** The image has no arm translation; `apks.py` prefers an x86_64 or universal APK.
- **Offline.** The emulator's only network is `apps-offline` (`internal: true`), where Caddy answers the harness
  names. The apps open on their default instance's live feed (lemmy.world, lemmy.zip), which reflowed under the
  harness's taps and differed every run; offline they show an error there instead (Thunder's "Unable to load" page is
  where its Account Settings link leads to the login). The runner, on both networks, fetches the APKs.
- **One adb server**, listening on every interface from the start (`adb -a`): restarting it for the network once the
  device was ready raced the old server's exit.

## Findings (first run, 2026-11-07)

Every PieFed-capable app passes all three steps, and AntennaPod lists the podcast episode; the clients exposed no
PieFed bug. The Mastodon, Pixelfed, PeerTube and Jerboa apps stop at their first call, since PieFed serves none of
their APIs: `docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md` is the proposal for the Mastodon and
Pixelfed ones, with the questions for the owner. Its §7 lists two things the harness does not cover: logged-out
browsing (the harness instance is private, as a fresh site is) and iOS Mlem's reading of the `/api/v3/site` stub.
