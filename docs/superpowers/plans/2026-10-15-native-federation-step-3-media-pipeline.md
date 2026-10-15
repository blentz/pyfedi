# Native Federation Step 3: Media Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A moderator of a `video_channel` or `podcast` community can upload a video or audio file. PieFed rejects it before it is fully written if it is too large, checks its type by magic bytes, and keeps the post hidden while a sandboxed `ffprobe` validates the file and makes a thumbnail. The post is published and federated only when the file is ready.

**Architecture:**
- **Two new modules.**
  - `app/shared/media.py` holds pure rules: who may upload, size limits, magic-byte sniffing, and the `extensions['media']` record. It also stores the upload.
  - `app/shared/ffmpeg.py` runs `ffprobe`/`ffmpeg` in a sandbox: 60 s timeout, a 512 MiB address-space cap, and the `file` protocol only.
- **A celery task.** `probe_media` in `app/shared/tasks/media.py` runs on the `background` queue. It validates, makes the thumbnail, moves the file to S3 when S3 is on, and then publishes the post the way the scheduled-post publisher does.
- **Processing state.** A processing post has `Post.status = POST_STATUS_PROCESSING` (-3). The existing unpublished-post gates then hide it from listings, from federation and from anyone except the author, mods and admins, with no new visibility code.

**Tech Stack:** Flask 3.1.3 / Werkzeug 3.1.9 (per-request `max_content_length`), SQLAlchemy, Celery, Pillow, ffmpeg/ffprobe from Debian, pytest with eager celery, moto for S3, and the podman interop harness.

**Spec:** `docs/superpowers/specs/2026-10-14-native-federation-design.md`. This plan covers §3.4, §3.5 and §5. Amendments A1–A12 apply. Task 0 adds A13 below, whose rulings bind this plan.

## Global Constraints

- **Branch:** `feat/media-pipeline` off `blentz` at `c82a3d240`. Never push or merge.
- **Commits:**
  - Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, or names the model that actually wrote it.
  - Make one commit per task, or per defect inside a fix round.
- **TDD:** write the failing test, run it red, then implement.
- **Coverage:** 100% line and branch coverage on changed lines, from the changed-line gate at base `c82a3d240` with `--branches`. Floor 100 for each new module in `coverage_floors.ini`.
- **Gates:** run them only from repo-committed tools, never ad-hoc `/tmp` scripts. The coverage chain is the one in `tests/README.md` ("coverage ratchet"):
  ```
  ./run_tests.sh tests/ -q --cov=app --cov-branch --cov-report=json && \
  python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini && \
  python3 tests/check_changed_line_coverage.py coverage.json c82a3d240 --branches
  ```
- **No inline imports:** no new function-level imports unless technically unavoidable. `KEPT` in `tests/test_no_inline_imports.py` stays 85.
- **Slow tests:** a test that is slow or flaky is a bug to fix. The `ffprobe` tests use tiny fixtures and must each finish in under 2 s.
- **Static files:** tests must leave no file under `app/static/media`, `app/static/tmp` or `app/static/posts`. `tests/static_leak_gate.py` fails any test that does.
- **Settings and limits** (spec §3.5): `allow_video_file_uploads` (existing; values `no`, `user 1`, `admins`, `users`), `allow_audio_file_uploads` (same values, default `no`), `max_video_upload_bytes` default `524288000`, `max_audio_upload_bytes` default `209715200`.
- **Accepted types** (spec §5.1, §5.2):
  - Video containers: MP4/MOV (`ftyp`) and WebM (EBML with doctype `webm`). Video codecs: {h264, vp9, av1}.
  - Audio containers: MP3 (ID3 or frame sync), M4A (`ftyp` brand `M4A `/`M4B `) and Ogg (`OggS`). Audio codecs: {aac, mp3, opus, vorbis}.
- **Sandbox:** `ffprobe -v error -print_format json -show_format -show_streams -protocol_whitelist file <path>`, with timeout 60 s, `RLIMIT_AS` 512 MiB, and no network protocols.
- **Media record:** `Post.extensions['media']` has the keys `status, error, uuid, mime, bytes, duration_s, width, height, fps, codec, thumbnail_file_id`. `uuid` is uuid4 and never changes. `db.JSON` is not mutable-tracked, so always assign a new dict.

## Spec amendment A13 (Task 0 writes it; it binds this plan)

The code survey of 2026-10-15 found spec §2, §5.1, §5.3 and §5.5 out of line with the code. Rulings:

1. **Processing state.** A post whose media is processing has `Post.status = POST_STATUS_PROCESSING` (new constant, -3), as well as `extensions.media.status = 'processing'`.
   - Every existing unpublished-post gate then applies unchanged:
     - Listings and feeds filter `status > POST_STATUS_REVIEWING`.
     - `refuse_unpublished_post` (`app/post/routes.py`) shows the post only to its author, the community's moderators and admins/staff, and returns 404 to everyone else.
     - The AP route refuses it with 403, the existing answer for unpublished posts, not the 404 that §5.3 said.
   - A processing post is not listed anywhere, not even for its author. After upload the author is redirected to the post page, which shows a "processing" notice.
2. **Size limit.** The limit is enforced per request. A `before_request` hook sets `request.max_content_length = limit + 1 MiB` on the two upload endpoints.
   - Werkzeug refuses a larger `Content-Length` with 413 before reading anything, and stops a lying or chunked body at the cap.
   - After parsing, the file's exact size is checked against the limit.
   - The global `MAX_CONTENT_LENGTH` (env, default 100 MB) is unchanged; §2's "no `MAX_CONTENT_LENGTH`" was wrong.
3. **Sandbox mechanism.** A `preexec_fn` calls `resource.setrlimit(RLIMIT_AS, 512 MiB)`. No `prlimit` binary is used, which avoids an extra dependency with the same effect.
4. **File storage.**
   - The media file is stored at `Post.url`, under `app/static/media/posts/xx/yy/<gibberish15>.<ext>`, with no `File` row.
   - The thumbnail is an ordinary `File` that is also `post.image`, so listings show it and the existing image delete paths remove it. `extensions.media.thumbnail_file_id` equals `post.image_id`.
5. **Post types.** A video upload is `POST_TYPE_VIDEO`. An audio upload is `POST_TYPE_LINK` with `extensions.media_type = 'audio'`, the same shape inbound `Audio` objects already get.
6. **Who may upload.**
   - Video: moderators of a `video_channel` community whom `allow_video_file_uploads` admits. Audio: moderators of a `podcast` community whom `allow_audio_file_uploads` admits.
   - Video uploads in other communities end. Video links are unaffected.
   - No form sets `Community.kind` before step 4, so step 3 must not be deployed without step 4.
7. **Edits.** Editing a media post cannot replace its file; the edit form shows no upload field for it. A post with a schedule becomes `POST_STATUS_SCHEDULED` instead of published when its media is ready.
8. **Deletion** (corrects §5.5).
   - Soft delete keeps files, as for every post today.
   - Purge (`Post.delete_dependencies`) removes the media file locally or in S3, plus the thumbnail through `post.image`.
   - The old purge path removed only a video stored on S3. Local and audio media are new.
9. **Failure.** `probe_media` runs on the `background` celery queue. A failure sets `extensions.media.status = 'failed'` with an `error`, leaves the post unpublished, and notifies the author with a `Notification` of `notif_type=NOTIF_DEFAULT`, `subtype='media_failed'`.
10. **Container image.** `ffmpeg` goes into the Dockerfile `builder` stage, which `test` inherits, and the `runtime` stage. It is also added to `INSTALL.md`.
    - `deploy/DEPLOY_GUIDE.md` and `INSTALL.md` document the matching reverse-proxy body limit: Caddy `request_body { max_size 510MB }`; nginx `client_max_body_size 510M`.

## Review Focus

1. **A lying `Content-Length`, or chunked transfer, sending more than the limit.** It must get 413 with no file left on disk. Task 4 tests this with the Werkzeug test client streaming an oversized body without a correct length.
2. **A renamed non-media file**, e.g. a PNG or text file with an `.mp4` name, or an MP4 posted to the audio form. It must get a form error, with nothing stored and no post created. Task 3 tests every container against the wrong kind, and Task 4 tests the route.
3. **`ffprobe` hanging or using huge memory** on a hostile file. It is killed at 60 s or 512 MiB, and the post ends `failed`, not stuck in `processing`. Task 2 tests the timeout and rlimit with a stub command; Task 5 tests the task outcome.
4. **The worker crashing mid-task**, e.g. the thumbnail write raising. The post must never be published half-done. It ends `failed` with the reason, and any thumbnail written is removed. Task 5.
5. **A processing post reaching anyone else** through the API post list, a community feed, the AP object, or a `send_post` edit federation. Task 4 asserts each path.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `app/constants.py` | modify | `POST_STATUS_PROCESSING = -3`, `MEDIA_KIND_VIDEO`, `MEDIA_KIND_AUDIO` |
| `app/utils.py` | modify | `upload_access_allows(setting, user)` shared helper; `can_upload_video` and new `can_upload_audio` use it; `upload_file_to_s3(local_path, key, content_type) -> str` |
| `app/admin/forms.py`, `app/admin/routes.py`, `app/templates/admin/misc.html` | modify | three new settings |
| `app/shared/media.py` | create | `can_upload_media`, `upload_byte_limit`, `sniff_media_mime`, `media_of`, `with_media`, `store_uploaded_media`, `media_local_path` |
| `app/shared/ffmpeg.py` | create | `MediaToolError`, `run_ffprobe`, `extract_frame`, `extract_cover`, `_run` (sandbox) |
| `app/shared/tasks/media.py` | create | `validate_probe` (pure), `probe_media` celery task, `publish_ready_post` |
| `app/shared/tasks/__init__.py` | modify | register `probe_media` in `task_selector` |
| `app/__init__.py` | modify | route `app.shared.tasks.media.*` to the `background` queue; `before_request` per-request upload cap |
| `app/community/forms.py` | modify | `CreateAudioForm`; the video form keeps `image_file` |
| `app/community/routes.py`, `app/shared/post.py`, `app/templates/post/post_edit.html` (and the add-post template) | modify | the upload branch goes to `store_uploaded_media` |
| `app/shared/tasks/pages.py` | modify | `send_post` refuses a media post that is not ready |
| `app/models.py` | modify | `Post.delete_dependencies` removes local or S3 media |
| `Dockerfile`, `INSTALL.md`, `deploy/DEPLOY_GUIDE.md`, `Caddyfile` | modify | ffmpeg and the body limit |
| `tests/fixtures/media/*` | create | tiny real media files (made once with ffmpeg, committed) |
| `tests/test_media_*.py`, `interop/test_media_pipeline.py` | create | tests |

---

### Task 0: Branch, spec amendment A13

**Files:**
- Modify: `docs/superpowers/specs/2026-10-14-native-federation-design.md` (§10)

- [ ] **Step 1:** `git checkout -b feat/media-pipeline c82a3d240`
- [ ] **Step 2:** Append amendment **A13** to §10. Copy the ten rulings in "Spec amendment A13" above verbatim, in the style of A11/A12. Add one line marking A3 as superseded by A12 for `Community.kind`.
- [ ] **Step 3:** Commit: `docs: amendment A13, the step 3 media pipeline rulings`.

### Task 1: Settings and `can_upload_audio`

**Files:**
- Modify: `app/utils.py` (`can_upload_video`, ~3864)
- Modify: `app/admin/forms.py:40-45`, `app/admin/routes.py:~386,~443`, `app/templates/admin/misc.html:~36`
- Modify: `app/request_hooks.py:65`, to register `can_upload_audio` as a Jinja global next to `can_upload_video`
- Test: `tests/test_media_settings.py`

**Interfaces:**
- Produces: `upload_access_allows(setting: str, user) -> bool`, `can_upload_video(user=None) -> bool` (unchanged behaviour), `can_upload_audio(user=None) -> bool`.
- Settings: `get_setting('allow_audio_file_uploads', 'no')`, `get_setting('max_video_upload_bytes', 524288000)`, `get_setting('max_audio_upload_bytes', 209715200)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_media_settings.py
import pytest
from app.utils import can_upload_audio, can_upload_video, set_setting
from tests.factories import make_instance, make_user


@pytest.mark.parametrize('value, who, expected', [
    ('no', 'admin', False), ('user 1', 'first', True), ('user 1', 'admin', False),
    ('admins', 'admin', True), ('admins', 'plain', False), ('users', 'plain', True),
])
def test_audio_switch_takes_the_video_values(db_session, value, who, expected):
    ...  # build users: 'first' is id 1, 'admin' has the admin role, 'plain' is an ordinary local user
    set_setting('allow_audio_file_uploads', value)
    assert can_upload_audio(users[who]) is expected


def test_audio_switch_defaults_to_no(db_session):
    assert can_upload_audio(make_user(make_instance('test.piefed.local'), 'u', local=True)) is False


def test_video_switch_is_unchanged(db_session):
    ...  # the same parametrize table against allow_video_file_uploads / can_upload_video
```

The implementer fills the user setup with the factories (`make_user`, `grant_permission` or the admin-role helper used by existing admin tests; find an example with `grep -rn "is_admin_or_staff" tests | head`). Admin form tests:
- `POST /admin/misc` with `allow_audio_file_uploads=admins`, `max_video_upload_mb=100`, `max_audio_upload_mb=50` stores `admins`, `104857600` and `52428800`.
- The GET pre-fills them.
- Model the test on the existing test of `allow_video_file_uploads`; find it with `grep -rn allow_video_file_uploads tests`.

- [ ] **Step 2:** Run `./run_tests.sh tests/test_media_settings.py -q`. Expected: FAIL (`ImportError: can_upload_audio`).

- [ ] **Step 3: Implement**

```python
# app/utils.py, replacing the body of can_upload_video
def upload_access_allows(setting: str, user=None) -> bool:
    """One rule for every upload switch (spec §5.1): 'no', 'user 1', 'admins' or 'users'."""
    upload_access = get_setting(setting, 'no')
    upload_user = user or current_user
    if upload_access == 'no':
        return False
    elif upload_access == 'user 1' and upload_user.get_id() != 1:
        return False
    elif upload_access == 'admins' and not upload_user.is_admin_or_staff():
        return False
    elif upload_access == 'users' and not upload_user.is_authenticated:
        return False
    return True


def can_upload_video(user: User | None = None):
    return upload_access_allows('allow_video_file_uploads', user)


def can_upload_audio(user: User | None = None):
    return upload_access_allows('allow_audio_file_uploads', user)
```

Admin form:
- Add `allow_audio_file_uploads = SelectField(_l('Allow audio file uploads'), choices=video_upload_options, ...)` with the same `render_kw`/validators as the video field.
- Add `max_video_upload_mb = IntegerField(_l('Largest video upload (MB)'), validators=[Optional(), NumberRange(min=1, max=10240)])` and `max_audio_upload_mb` (same, max 10240).
- The route stores `field.data * 1048576` with `set_setting('max_video_upload_bytes', ...)` and pre-fills `get_setting(...) // 1048576`.
- `misc.html` renders the three fields next to the video switch.

- [ ] **Step 4:** Run the test file. Expected: PASS.
- [ ] **Step 5:** Commit: `feat: audio upload switch and media size limits on the misc settings form`.

### Task 2: ffmpeg in the image, media fixtures, the sandboxed runner

**Files:**
- Modify: `Dockerfile` (builder apt line 4-9; runtime apt line 51-57), `INSTALL.md:75-118`
- Create: `tests/fixtures/media/` with `video_h264.mp4`, `video_vp9.webm`, `video_mpeg4.mp4` (codec mpeg4, must be rejected later), `audio.mp3`, `audio_cover.mp3` (with an attached picture), `audio.m4a`, `audio_opus.ogg`, `not_media.mp4` (text bytes), and `README.md` holding the exact ffmpeg commands that made them.
- Create: `app/shared/ffmpeg.py`
- Modify: `coverage_floors.ini` (`app/shared/ffmpeg.py = 100`)
- Test: `tests/test_media_ffmpeg.py`

**Interfaces:**
- Produces:
  - `class MediaToolError(Exception)`
  - `run_ffprobe(path: str) -> dict`, which raises `MediaToolError` on timeout, non-zero exit or bad JSON
  - `extract_frame(path: str, at_s: float, out_path: str) -> None`
  - `extract_cover(path: str, out_path: str) -> None`
  - `PROBE_TIMEOUT_S = 60`, `ADDRESS_SPACE_BYTES = 512 * 1024 * 1024`

- [ ] **Step 1: Make the fixtures.** Each must be under 30 KB, 1 s long and 64x64. Run these in a container that has ffmpeg (the test image after Step 2), from the repo root:

```bash
ffmpeg -f lavfi -i testsrc=size=64x64:rate=10 -f lavfi -i sine=frequency=440 -t 1 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest tests/fixtures/media/video_h264.mp4
ffmpeg -f lavfi -i testsrc=size=64x64:rate=10 -t 1 -c:v libvpx-vp9 tests/fixtures/media/video_vp9.webm
ffmpeg -f lavfi -i testsrc=size=64x64:rate=10 -t 1 -c:v mpeg4 tests/fixtures/media/video_mpeg4.mp4
ffmpeg -f lavfi -i sine=frequency=440 -t 1 -c:a libmp3lame -b:a 32k tests/fixtures/media/audio.mp3
ffmpeg -i tests/fixtures/media/audio.mp3 -f lavfi -i color=red:size=64x64 -frames:v 1 -map 0 -map 1 -c:a copy -c:v mjpeg -disposition:v attached_pic -id3v2_version 3 tests/fixtures/media/audio_cover.mp3
ffmpeg -f lavfi -i sine=frequency=440 -t 1 -c:a aac tests/fixtures/media/audio.m4a
ffmpeg -f lavfi -i sine=frequency=440 -t 1 -c:a libopus tests/fixtures/media/audio_opus.ogg
printf 'this is not a video\n' > tests/fixtures/media/not_media.mp4
```

  If the `.m4a` comes out with an `M4A ` brand, keep it; check with `head -c 12 tests/fixtures/media/audio.m4a | xxd`. If the brand differs, add `-brand M4A`. Record the commands in `tests/fixtures/media/README.md`.

- [ ] **Step 2: Add ffmpeg to the image.**
  - Add `ffmpeg` to the `apt-get install` list in the `builder` stage, which `test` inherits, and in the `runtime` stage.
  - Add `ffmpeg` to the INSTALL.md apt line.
  - Rebuild: `./run_tests.sh tests/test_media_ffmpeg.py -q` builds the test image.

- [ ] **Step 3: Write the failing tests**

```python
# tests/test_media_ffmpeg.py
import json
import os
import subprocess
import pytest
from app.shared import ffmpeg
from app.shared.ffmpeg import MediaToolError, extract_cover, extract_frame, run_ffprobe

FIX = os.path.join(os.path.dirname(__file__), 'fixtures', 'media')


def test_probe_reads_a_real_mp4():
    probe = run_ffprobe(os.path.join(FIX, 'video_h264.mp4'))
    codecs = {s['codec_name'] for s in probe['streams']}
    assert 'h264' in codecs and float(probe['format']['duration']) > 0


def test_probe_of_non_media_raises():
    with pytest.raises(MediaToolError):
        run_ffprobe(os.path.join(FIX, 'not_media.mp4'))


def test_probe_command_is_sandboxed(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(cmd=cmd, **kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout=b'{"streams": [], "format": {}}', stderr=b'')
    monkeypatch.setattr(ffmpeg.subprocess, 'run', fake_run)
    run_ffprobe('/x/y.mp4')
    assert seen['cmd'][:9] == ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format',
                               '-show_streams', '-protocol_whitelist', 'file']
    assert seen['cmd'][-1] == 'file:/x/y.mp4'
    assert seen['timeout'] == 60 and seen['preexec_fn'] is ffmpeg._limit_address_space


def test_probe_timeout_raises(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 60)
    monkeypatch.setattr(ffmpeg.subprocess, 'run', fake_run)
    with pytest.raises(MediaToolError, match='timed out'):
        run_ffprobe('/x/y.mp4')


def test_probe_bad_json_raises(monkeypatch):
    monkeypatch.setattr(ffmpeg.subprocess, 'run',
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout=b'not json', stderr=b''))
    with pytest.raises(MediaToolError, match='unreadable'):
        run_ffprobe('/x/y.mp4')


def test_tool_missing_raises(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(ffmpeg.subprocess, 'run', fake_run)
    with pytest.raises(MediaToolError, match='not installed'):
        run_ffprobe('/x/y.mp4')


def test_rlimit_is_set(monkeypatch):
    calls = []
    monkeypatch.setattr(ffmpeg.resource, 'setrlimit', lambda which, lim: calls.append((which, lim)))
    ffmpeg._limit_address_space()
    assert calls == [(ffmpeg.resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))]


def test_frame_and_cover_are_written(tmp_path):
    frame, cover = tmp_path / 'f.jpg', tmp_path / 'c.jpg'
    extract_frame(os.path.join(FIX, 'video_h264.mp4'), 0.1, str(frame))
    extract_cover(os.path.join(FIX, 'audio_cover.mp3'), str(cover))
    assert frame.read_bytes()[:2] == b'\xff\xd8' and cover.read_bytes()[:2] == b'\xff\xd8'


def test_cover_of_audio_without_picture_raises(tmp_path):
    with pytest.raises(MediaToolError):
        extract_cover(os.path.join(FIX, 'audio.mp3'), str(tmp_path / 'c.jpg'))
```

- [ ] **Step 4:** Run `./run_tests.sh tests/test_media_ffmpeg.py -q`. Expected: FAIL (`ModuleNotFoundError: app.shared.ffmpeg`).

- [ ] **Step 5: Implement**

```python
# app/shared/ffmpeg.py
"""ffprobe and ffmpeg in a sandbox (spec §5.2, A13.3): local files only, 60 s, 512 MiB of address space."""
import json
import resource
import subprocess

PROBE_TIMEOUT_S = 60
ADDRESS_SPACE_BYTES = 512 * 1024 * 1024
_FILE_ONLY = ['-protocol_whitelist', 'file']


class MediaToolError(Exception):
    pass


def _limit_address_space():
    resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_BYTES, ADDRESS_SPACE_BYTES))


def _run(cmd: list) -> bytes:
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=PROBE_TIMEOUT_S,
                              preexec_fn=_limit_address_space, check=False)
    except subprocess.TimeoutExpired:
        raise MediaToolError(f'{cmd[0]} timed out after {PROBE_TIMEOUT_S} s')
    except FileNotFoundError:
        raise MediaToolError(f'{cmd[0]} is not installed')
    if done.returncode != 0:
        raise MediaToolError(f'{cmd[0]} failed: {done.stderr.decode(errors="replace")[:200]}')
    return done.stdout


def run_ffprobe(path: str) -> dict:
    out = _run(['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams',
                *_FILE_ONLY, 'file:' + path])
    try:
        return json.loads(out)
    except ValueError:
        raise MediaToolError('ffprobe output was unreadable')


def extract_frame(path: str, at_s: float, out_path: str) -> None:
    _run(['ffmpeg', '-v', 'error', *_FILE_ONLY, '-ss', f'{at_s:.3f}', '-i', 'file:' + path,
          '-frames:v', '1', '-y', 'file:' + out_path])


def extract_cover(path: str, out_path: str) -> None:
    _run(['ffmpeg', '-v', 'error', *_FILE_ONLY, '-i', 'file:' + path, '-an', '-map', '0:v:0',
          '-frames:v', '1', '-y', 'file:' + out_path])
```

  If `test_cover_of_audio_without_picture_raises` passes only because ffmpeg exits non-zero ("Stream map '0:v:0' matches no streams"), that is the intended behaviour. Check it is so, and keep it.

- [ ] **Step 6:** Run the tests. Expected: PASS. Add `app/shared/ffmpeg.py = 100` to `coverage_floors.ini`.
- [ ] **Step 7:** Commit: `feat: ffmpeg in the image and a sandboxed ffprobe/ffmpeg runner`.

### Task 3: Upload rules, `sniff_media_mime`, the media record

**Files:**
- Modify: `app/constants.py` (`POST_STATUS_PROCESSING = -3` next to the other `POST_STATUS_*`; `MEDIA_KIND_VIDEO = 'video'`, `MEDIA_KIND_AUDIO = 'audio'`)
- Create: `app/shared/media.py` (pure parts only in this task)
- Modify: `coverage_floors.ini` (`app/shared/media.py = 100`)
- Test: `tests/test_media_rules.py`

**Interfaces:**
- Consumes: `can_upload_video`, `can_upload_audio` (Task 1); `Community.kind`, `COMMUNITY_KIND_VIDEO_CHANNEL`, `COMMUNITY_KIND_PODCAST`; `Community.is_moderator(user)`.
- Produces:
  - `media_kind_for(community) -> str | None`: `'video'` for video_channel, `'audio'` for podcast, else None.
  - `can_upload_media(user, community) -> bool`
  - `upload_byte_limit(kind: str) -> int`
  - `sniff_media_mime(head: bytes, kind: str) -> str | None`: needs at least the first 64 bytes; returns one of `video/mp4`, `video/quicktime`, `video/webm`, `audio/mpeg`, `audio/mp4`, `audio/ogg`.
  - `media_of(post) -> dict | None`
  - `with_media(post, **fields) -> None`: merges into `extensions['media']` and assigns a new dict.
  - `SNIFF_BYTES = 64`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_media_rules.py
import os
import pytest
from app.constants import COMMUNITY_KIND_PODCAST, COMMUNITY_KIND_VIDEO_CHANNEL
from app.shared.media import (can_upload_media, media_kind_for, media_of, sniff_media_mime,
                              upload_byte_limit, with_media)
from app.utils import set_setting

FIX = os.path.join(os.path.dirname(__file__), 'fixtures', 'media')


def head(name):
    with open(os.path.join(FIX, name), 'rb') as f:
        return f.read(64)


@pytest.mark.parametrize('name, kind, mime', [
    ('video_h264.mp4', 'video', 'video/mp4'), ('video_vp9.webm', 'video', 'video/webm'),
    ('audio.mp3', 'audio', 'audio/mpeg'), ('audio_cover.mp3', 'audio', 'audio/mpeg'),
    ('audio.m4a', 'audio', 'audio/mp4'), ('audio_opus.ogg', 'audio', 'audio/ogg'),
])
def test_real_files_sniff_to_their_type(name, kind, mime):
    assert sniff_media_mime(head(name), kind) == mime


@pytest.mark.parametrize('name, kind', [
    ('video_h264.mp4', 'audio'), ('audio.mp3', 'video'), ('audio.m4a', 'video'),
    ('audio_opus.ogg', 'video'), ('video_vp9.webm', 'audio'), ('not_media.mp4', 'video'),
    ('not_media.mp4', 'audio'),
])
def test_wrong_kind_or_not_media_is_refused(name, kind):
    assert sniff_media_mime(head(name), kind) is None


@pytest.mark.parametrize('data, kind, mime', [
    (b'\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00', 'video', 'video/quicktime'),
    (b'\x00\x00\x00\x14ftypM4B \x00\x00\x00\x00', 'audio', 'audio/mp4'),
    (b'\xff\xfb\x90\x00' + b'\x00' * 60, 'audio', 'audio/mpeg'),          # bare MPEG frame sync
    (b'\x1a\x45\xdf\xa3' + b'\x00' * 20 + b'matroska', 'video', None),   # Matroska is not WebM
    (b'', 'video', None), (b'\x00\x00\x00\x14ftyp', 'video', None),     # too short for a brand
    (b'\xff\x00', 'audio', None),
])
def test_edge_headers(data, kind, mime):
    assert sniff_media_mime(data, kind) == mime


def test_limits_come_from_settings(db_session):
    assert upload_byte_limit('video') == 524288000 and upload_byte_limit('audio') == 209715200
    set_setting('max_video_upload_bytes', 1000)
    assert upload_byte_limit('video') == 1000


def test_kind_for_community():
    class C:   # plain stand-in: kind is the only attribute read
        kind = COMMUNITY_KIND_VIDEO_CHANNEL
    assert media_kind_for(C) == 'video'
    C.kind = COMMUNITY_KIND_PODCAST
    assert media_kind_for(C) == 'audio'
    C.kind = 'discussion'
    assert media_kind_for(C) is None


def test_only_a_permitted_moderator_of_a_media_community_may_upload(db_session):
    ...  # video_channel community; mod + allow_video 'users' -> True; non-mod -> False;
    ...  # mod + allow_video 'no' -> False; discussion community mod -> False; podcast mod + allow_audio 'users' -> True


def test_media_record_is_merged_into_a_new_dict(db_session):
    ...  # post with extensions {'podcast': {'credits': []}}; with_media(post, status='processing', uuid='u')
    ...  # then with_media(post, status='ready'); assert extensions == {'podcast': {...}, 'media': {'status': 'ready', 'uuid': 'u'}}
    ...  # and the dict object differs from the original (identity check); media_of(post_without) is None
```

The implementer writes the two `...` tests with `tests/factories` (`make_community`, `make_community_member(..., is_moderator=True)`, `make_post`) and `set_setting`. The comments list every case that must be asserted.

- [ ] **Step 2:** Run the file. Expected: FAIL (`ModuleNotFoundError: app.shared.media`).

- [ ] **Step 3: Implement the pure part of `app/shared/media.py`**

```python
# app/shared/media.py
"""Uploaded video and audio (spec §3.4, §5.1, A13): who may upload, how big, what type, and the media record."""
from app.constants import (COMMUNITY_KIND_PODCAST, COMMUNITY_KIND_VIDEO_CHANNEL, MEDIA_KIND_AUDIO,
                           MEDIA_KIND_VIDEO)
from app.utils import can_upload_audio, can_upload_video, get_setting

SNIFF_BYTES = 64
DEFAULT_LIMITS = {MEDIA_KIND_VIDEO: 524288000, MEDIA_KIND_AUDIO: 209715200}
_KIND_OF_COMMUNITY = {COMMUNITY_KIND_VIDEO_CHANNEL: MEDIA_KIND_VIDEO, COMMUNITY_KIND_PODCAST: MEDIA_KIND_AUDIO}
_SWITCH = {MEDIA_KIND_VIDEO: can_upload_video, MEDIA_KIND_AUDIO: can_upload_audio}
_AUDIO_BRANDS = (b'M4A ', b'M4B ')


def media_kind_for(community) -> str | None:
    return _KIND_OF_COMMUNITY.get(community.kind)


def can_upload_media(user, community) -> bool:
    kind = media_kind_for(community)
    return kind is not None and community.is_moderator(user) and _SWITCH[kind](user)


def upload_byte_limit(kind: str) -> int:
    return int(get_setting(f'max_{kind}_upload_bytes', DEFAULT_LIMITS[kind]))


def sniff_media_mime(head: bytes, kind: str) -> str | None:
    """The type the first bytes prove, if it is one `kind` accepts. Extension and client mime are never read."""
    if len(head) >= 12 and head[4:8] == b'ftyp':
        brand = head[8:12]
        if brand in _AUDIO_BRANDS:
            mime = 'audio/mp4'
        elif brand == b'qt  ':
            mime = 'video/quicktime'
        else:
            mime = 'video/mp4'
    elif head[:4] == b'\x1a\x45\xdf\xa3':
        mime = 'video/webm' if b'webm' in head[:SNIFF_BYTES] else None
    elif head[:4] == b'OggS':
        mime = 'audio/ogg'
    elif head[:3] == b'ID3' or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        mime = 'audio/mpeg'
    else:
        mime = None
    if mime is None or not mime.startswith(kind + '/'):
        return None
    return mime


def media_of(post) -> dict | None:
    extensions = post.extensions if isinstance(post.extensions, dict) else {}
    media = extensions.get('media')
    return media if isinstance(media, dict) else None


def with_media(post, **fields) -> None:
    extensions = dict(post.extensions) if isinstance(post.extensions, dict) else {}
    extensions['media'] = {**(media_of(post) or {}), **fields}
    post.extensions = extensions   # new dicts: db.JSON does not track in-place changes
```

  Check the import graph. `app.shared.media` imports `app.utils`; `app.utils` must not import `app.shared.media`, or a cycle forms. `tests/test_import_order.py` and `tests/test_no_inline_imports.py` must stay green.

- [ ] **Step 4:** Run the file. Expected: PASS. Add `app/shared/media.py = 100` to `coverage_floors.ini`.
- [ ] **Step 5:** Commit: `feat: media upload rules, magic-byte sniffing and the media record`.

### Task 4: Upload path, processing state and gating

**Files:**
- Modify: `app/__init__.py` (a `before_request` setting the per-request cap; `task_routes` adds `'app.shared.tasks.media.*': {'queue': 'background'}`)
- Modify: `app/community/forms.py` (`CreateAudioForm`, modelled on `CreateVideoForm` at :378: an `audio_url` link field plus `image_file` with `accept=audio/mpeg,audio/mp4,audio/ogg`)
- Modify: `app/community/routes.py:1215-1310` (`add_post`)
  - Add the `audio` type.
  - Take `image_file` only when `can_upload_media(current_user, community)` and the kind matches the type.
  - Redirect to the post page after a media upload.
- Modify: `app/shared/post.py`
  - `make_post` (:239) and `edit_post` (:345, upload block :576-~700): when `uploaded_file` is given and `media_kind_for(community)` matches the post type, call `store_uploaded_media` instead of the image branch.
  - Edits of a media post ignore `uploaded_file` and add a form error (A13.7).
  - The extension allowlist widening for video (:290-297, :579) is replaced by `can_upload_media`.
- Modify: `app/shared/upload.py:62`: drop the video extension widening from `process_upload`. The user-file and API image upload never take video now.
- Modify: `app/templates/post/post_edit.html:168` and the add-post template: show the upload field when `can_upload_media(current_user, community)`. Register `can_upload_media` as a Jinja global in `app/request_hooks.py`.
- Modify: `app/shared/media.py`: add `store_uploaded_media` and `media_local_path`.
- Modify: `app/shared/tasks/pages.py:159`: `send_post` returns early for a post with a media record whose status is not `ready`.
- Modify: the post page template: a "processing" notice when `media_of(post).status == 'processing'`, and the failure reason when `failed`, shown to the people who can see the post.
- Test: `tests/test_media_upload.py`

**Interfaces:**
- Consumes: Task 3 functions; `POST_STATUS_PROCESSING`.
- Produces:
  - `store_uploaded_media(post, uploaded_file, kind) -> str | None`. It returns an error message, or None on success. On success the file is under `app/static/media/posts/xx/yy/<gibberish15>.<ext>`, `post.url` is its served URL, `post.status == POST_STATUS_PROCESSING`, and `extensions.media == {status: 'processing', uuid, mime, bytes}`.
  - `media_local_path(post) -> str | None`: the local path of `post.url` when it is a local media URL.
  - `probe_media` is queued by `task_selector('probe_media', post_id=post.id)`; Task 5 implements it, so this task registers a stub. Instead, put the `task_selector` call behind a `queue_probe(post_id)` function in `app/shared/media.py` that Task 5 fills. This task's tests monkeypatch `queue_probe`.
- Extension per mime: `{'video/mp4': '.mp4', 'video/quicktime': '.mov', 'video/webm': '.webm', 'audio/mpeg': '.mp3', 'audio/mp4': '.m4a', 'audio/ogg': '.ogg'}`.

- [ ] **Step 1: Write the failing tests.** Use the web test client, logged in as a moderator of a `video_channel` community, with `allow_video_file_uploads='users'`. Every test removes the files it stores (`media_local_path`), or the static leak gate fails it. Tests:

```python
# tests/test_media_upload.py — the cases, each its own test
# 1. valid h264 mp4 upload via POST /c/<name>/submit/video with image_file=(open(fixture,'rb'), 'clip.mp4'):
#    302 to the post page; post.status == POST_STATUS_PROCESSING; post.type == POST_TYPE_VIDEO;
#    media_of(post) has status 'processing', a uuid4 string (uuid.UUID(v).version == 4), mime 'video/mp4',
#    bytes == fixture size; the file exists at media_local_path(post) with identical bytes; queue_probe called once.
# 2. audio: podcast community, allow_audio 'users', audio.mp3 via /submit/audio: type LINK,
#    extensions['media_type'] == 'audio', mime 'audio/mpeg'.
# 3. not_media.mp4 -> 200 with the form error 'not a supported video file'; no Post row; no file under posts/.
# 4. an mp3 to the video form -> refused the same way.
# 5. too large by Content-Length: set max_video_upload_bytes=1000, upload video_h264.mp4 (> 1000 bytes) -> 413; no Post; no file.
# 6. too large without an honest length: build the multipart body by hand and send it with
#    environ_overrides={'wsgi.input_terminated': True} and no CONTENT_LENGTH (chunked) -> 413; no file left.
#    (Werkzeug applies max_content_length to the stream; if the test client cannot express chunked input,
#    call the app with a raw WSGI environ — see werkzeug.test.EnvironBuilder(input_stream=..., content_length=None).)
# 7. size exactly at the limit (set limit to the fixture size) -> accepted; one byte over -> 413.
# 8. non-moderator of the video_channel -> the upload is ignored (the post, if the form allows a url, is created
#    without media) and nothing is stored; the field is not rendered on GET.
# 9. moderator but allow_video 'no' -> same as 8.
# 10. discussion community, moderator, allow_video 'users' -> no upload field, upload ignored.
# 11. editing a ready media post with a new file -> form error, file and media record unchanged.
# 12. gating of a PROCESSING post (create it with store_uploaded_media in the test):
#     - anonymous GET /post/<id> -> 404; author -> 200 with the processing notice; community moderator -> 200; admin -> 200
#     - it is absent from the community page, /api/alpha/post/list, and the home feed for its author
#     - GET /post/<id> with Accept: application/activity+json -> 403
#     - send_post(post.id) sends nothing (patch the delivery function used by send_post and assert not called)
#     - for contrast, after with_media(post, status='ready') and status PUBLISHED, send_post delivers.
```

  Write each as a real test function, with factories and fixtures as in neighbouring tests. Find how existing route tests log in and post multipart with `grep -rn "submit/image" tests | head`.

- [ ] **Step 2:** Run the file. Expected: FAIL.

- [ ] **Step 3: Implement.** The key code:

```python
# app/__init__.py, inside create_app after the blueprints are registered
MEDIA_UPLOAD_SLACK = 1024 * 1024   # form fields around the file

@app.before_request
def cap_media_upload():
    """A13.2: a media upload is refused at its kind's limit, before the body is read past it."""
    if request.method != 'POST' or request.endpoint != 'community.add_post':
        return
    kind = request.view_args.get('type') if request.view_args else None
    if kind in (MEDIA_KIND_VIDEO, MEDIA_KIND_AUDIO):
        request.max_content_length = upload_byte_limit(kind) + MEDIA_UPLOAD_SLACK
```

  Check the endpoint name and the `type` view arg against `app/community/routes.py:1215`. The route may carry `type` in the path as `/<actor>/submit/<type>`; verify it. If this hook does not fit `app/__init__.py`'s import structure without an inline import, put it in `app/request_hooks.py`, which already registers hooks.

```python
# app/shared/media.py (additions)
import os
import shutil
import uuid

from flask import current_app
from app.utils import ensure_directory_exists, gibberish

MEDIA_EXT = {'video/mp4': '.mp4', 'video/quicktime': '.mov', 'video/webm': '.webm',
             'audio/mpeg': '.mp3', 'audio/mp4': '.m4a', 'audio/ogg': '.ogg'}
COPY_CHUNK = 1024 * 1024


def store_uploaded_media(post, uploaded_file, kind) -> str | None:
    stream = uploaded_file.stream
    stream.seek(0)
    mime = sniff_media_mime(stream.read(SNIFF_BYTES), kind)
    if mime is None:
        return f'This is not a supported {kind} file.'
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    if size > upload_byte_limit(kind):   # the request cap includes form slack; this is the exact limit
        return f'This {kind} file is too large.'
    stream.seek(0)
    name = gibberish(15)
    directory = f'app/static/media/posts/{name[0:2]}/{name[2:4]}'
    ensure_directory_exists(directory)
    path = f'{directory}/{name}{MEDIA_EXT[mime]}'
    with open(path, 'wb') as out:
        shutil.copyfileobj(stream, out, COPY_CHUNK)
    post.url = f"{current_app.config['SERVER_URL']}/{path[len('app/'):]}"
    post.status = POST_STATUS_PROCESSING
    with_media(post, status='processing', uuid=str(uuid.uuid4()), mime=mime, bytes=size)
    return None
```

  Use the repo's existing helper for a local path to a served URL (`served_path`, `models.py:479`) instead of the slice above if it gives the same result. Check with a test that the URL is `SERVER_URL + '/static/media/posts/...'`.

  - `media_local_path(post)`: if `post.url` starts with `SERVER_URL + '/static/media/posts/'`, return `'app' + url[len(SERVER_URL):]`; otherwise None.
  - `queue_probe(post_id)`: `task_selector('probe_media', post_id=post_id)`. Task 5 adds the task to the selector. Until then the tests patch `queue_probe`.
  - `make_post`/`edit_post` call `queue_probe` after the commit that stores the post.
  - They must not run the federation (`task_selector('make_post', ...)`) or `notify_about_post` for a `POST_STATUS_PROCESSING` post. Find where scheduled posts skip them, and apply the same condition. If scheduled posts are skipped by `status == POST_STATUS_SCHEDULED`, widen it to `status < POST_STATUS_PUBLISHED`.

```python
# app/shared/tasks/pages.py, first lines of send_post after `post = session.get(Post, post_id)`
    media = media_of(post)
    if media is not None and media.get('status') != 'ready':   # spec §5.3: media federates only when ready
        return
```

  `media_of` is a pure function in `app/shared/media.py`. Import it at module top; if that forms a cycle, break the cycle (project rule), do not inline it.

- [ ] **Step 4:** Run the file, then `./run_tests.sh tests/ -q -k "post or upload or community"`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat: media uploads by moderators of video and podcast communities stay hidden while processing`.

### Task 5: `probe_media`: validate, thumbnail, S3, publish

**Files:**
- Create: `app/shared/tasks/media.py`
- Modify: `app/shared/tasks/__init__.py` (register `'probe_media': probe_media`)
- Modify: `app/utils.py`: add `upload_file_to_s3(local_path: str, key: str, content_type: str) -> str`, which returns the public URL. Model it on the S3 block in `app/shared/post.py:~656`: same session and client config, `ExtraArgs` with `ContentType`, `S3_PUBLIC_ACL` and `S3_STORAGE_CLASS` as that block does. Use it only from the new code.
- Modify: `coverage_floors.ini` (`app/shared/tasks/media.py = 100`)
- Test: `tests/test_media_probe.py`

**Interfaces:**
- Consumes: `run_ffprobe`, `extract_frame`, `extract_cover`, `MediaToolError` (Task 2); `media_of`, `with_media`, `media_local_path` (Tasks 3–4); `upload_file_to_s3`, `store_files_in_s3`; `task_selector`, `notify_about_post`, `get_task_session`, `patch_db_session`.
- Produces:
  - `validate_probe(probe: dict, kind: str) -> dict`, which returns the metadata fields or raises `MediaToolError(reason)`.
  - `probe_media(send_async, post_id)` as a celery task, in the same wrapper shape as `make_post` in `pages.py:64`.
  - `publish_ready_post(post) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_media_probe.py
import pytest
from app.shared.ffmpeg import MediaToolError
from app.shared.tasks.media import validate_probe


def probe(*streams, duration='12.5'):
    return {'streams': list(streams), 'format': {'duration': duration}}


V = {'codec_type': 'video', 'codec_name': 'h264', 'width': 1280, 'height': 720, 'avg_frame_rate': '30000/1001'}
A = {'codec_type': 'audio', 'codec_name': 'aac'}


def test_video_metadata():
    assert validate_probe(probe(V, A), 'video') == {
        'duration_s': 13, 'width': 1280, 'height': 720, 'fps': 30, 'codec': 'h264'}


def test_audio_metadata_has_no_picture_fields():
    assert validate_probe(probe({'codec_type': 'audio', 'codec_name': 'opus'}), 'audio') == {
        'duration_s': 13, 'codec': 'opus'}


def test_audio_with_cover_art_reads_the_audio_stream():
    cover = {'codec_type': 'video', 'codec_name': 'mjpeg', 'disposition': {'attached_pic': 1}}
    assert validate_probe(probe(cover, {'codec_type': 'audio', 'codec_name': 'mp3'}), 'audio')['codec'] == 'mp3'


@pytest.mark.parametrize('bad, reason', [
    (probe({**V, 'codec_name': 'mpeg4'}, A), 'codec'),
    (probe(V, A, duration='0'), 'duration'), (probe(V, A, duration='N/A'), 'duration'),
    (probe({**V, 'width': 0}, A), 'dimensions'), (probe(A), 'no video stream'),
    ({'streams': 'x'}, 'unreadable'),
])
def test_bad_video_is_refused_with_a_reason(bad, reason):
    with pytest.raises(MediaToolError, match=reason):
        validate_probe(bad, 'video')


def test_bad_audio_codec_is_refused():
    with pytest.raises(MediaToolError, match='codec'):
        validate_probe(probe({'codec_type': 'audio', 'codec_name': 'flac'}), 'audio')


def test_cover_art_alone_is_not_audio():
    cover = {'codec_type': 'video', 'codec_name': 'mjpeg', 'disposition': {'attached_pic': 1}}
    with pytest.raises(MediaToolError, match='no audio stream'):
        validate_probe(probe(cover), 'audio')
```

Task tests run eager. Each test builds a processing post with `store_uploaded_media` from a fixture, runs `probe_media(False, post.id)`, and cleans up its files.
1. `video_h264.mp4`:
   - `media.status == 'ready'`, plus `duration_s == 1`, `width == 64`, `height == 64` and `codec == 'h264'`.
   - The `uuid` and `bytes` are unchanged.
   - `post.status == POST_STATUS_PUBLISHED` and `posted_at` is set.
   - `post.image` is a `File` whose thumbnail exists on disk, and `media.thumbnail_file_id == post.image_id`.
   - `task_selector('make_post', post_id=...)` and `notify_about_post` were called once each (patch them).
2. `audio_cover.mp3`: ready, with a thumbnail and no `width`. `audio.mp3`: ready, with `post.image_id` None and no `thumbnail_file_id`.
3. `video_mpeg4.mp4`:
   - `status == 'failed'`, and the `error` mentions the codec.
   - `post.status` is still `POST_STATUS_PROCESSING`. Nothing is federated.
   - The author has one `Notification` with `subtype == 'media_failed'`, and `unread_notifications` went up by 1.
4. `run_ffprobe` patched to raise `MediaToolError('ffprobe timed out after 60 s')`: failed with that reason.
5. Thumbnail writing patched to raise `OSError`:
   - The post fails.
   - The partial thumbnail file and any `File` row are removed.
   - Nothing is published (Review Focus 4).
6. A post with `scheduled_for` set: when ready it becomes `POST_STATUS_SCHEDULED`, not published, and nothing is federated (A13.7).
7. S3 on (`s3_bucket` fixture, S3 config set as in `tests/test_shared_post_upload.py`):
   - The media object is in the bucket under `posts/xx/yy/<name>.<ext>` with the right `ContentType`.
   - The local file is gone, and `post.url` is the `S3_PUBLIC_URL` URL.
8. A missing post id, or a post with no media record: the task returns quietly.
9. A deleted post: the task returns without publishing.
10. A file that has gone from disk: failed with reason "media file is missing".

- [ ] **Step 2:** Run the file. Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# app/shared/tasks/media.py
"""probe_media (spec §5.2, A13): validate an uploaded file in a sandbox, thumbnail it, then publish its post."""
import math
import os

from flask import current_app
from flask_babel import force_locale, gettext

from app import celery
from app.constants import (MEDIA_KIND_AUDIO, MEDIA_KIND_VIDEO, NOTIF_DEFAULT, POST_STATUS_PROCESSING,
                           POST_STATUS_PUBLISHED, POST_STATUS_SCHEDULED)
from app.shared.ffmpeg import MediaToolError, extract_cover, extract_frame, run_ffprobe
from app.shared.media import media_local_path, media_of, with_media

VIDEO_CODECS = {'h264', 'vp9', 'av1'}
AUDIO_CODECS = {'aac', 'mp3', 'opus', 'vorbis'}


def _fps(rate: str) -> int | None:
    try:
        num, den = (int(x) for x in rate.split('/'))
        return round(num / den) if den else None
    except (AttributeError, ValueError):
        return None


def validate_probe(probe: dict, kind: str) -> dict:
    streams = probe.get('streams')
    if not isinstance(streams, list):
        raise MediaToolError('ffprobe output was unreadable')
    try:
        duration = float(probe.get('format', {}).get('duration'))
    except (TypeError, ValueError):
        duration = 0.0
    if not duration > 0:
        raise MediaToolError('the file has no duration')
    if kind == MEDIA_KIND_VIDEO:
        video = next((s for s in streams if s.get('codec_type') == 'video'
                      and not s.get('disposition', {}).get('attached_pic')), None)
        if video is None:
            raise MediaToolError('the file has no video stream')
        if video.get('codec_name') not in VIDEO_CODECS:
            raise MediaToolError(f"video codec {video.get('codec_name')} is not h264, vp9 or av1")
        width, height = video.get('width') or 0, video.get('height') or 0
        if width <= 0 or height <= 0:
            raise MediaToolError('the video has no dimensions')
        meta = {'duration_s': math.ceil(duration), 'width': width, 'height': height, 'codec': video['codec_name']}
        fps = _fps(video.get('avg_frame_rate'))
        if fps:
            meta['fps'] = fps
        return meta
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    if audio is None:
        raise MediaToolError('the file has no audio stream')
    if audio.get('codec_name') not in AUDIO_CODECS:
        raise MediaToolError(f"audio codec {audio.get('codec_name')} is not aac, mp3, opus or vorbis")
    return {'duration_s': math.ceil(duration), 'codec': audio['codec_name']}
```

  The `duration_s` rounding is `math.ceil`, so a 12.5 s clip is 13 and a 1 s fixture is 1. The tests above assume this; keep the two consistent.

The task body, in the wrapper shape of `pages.py:64`:
- Open a task session (`get_task_session`, `patch_db_session`).
- Load the post. Return if it is missing, deleted, has no media record, or its media status is not `processing`.
- Find the path with `media_local_path(post)`. If the path is missing, the post fails with "media file is missing".
- Probe and validate; the kind comes from `media_of(post)['mime']`'s prefix.
- **Thumbnail:**
  - Video: `extract_frame(path, duration * 0.1, tmp_jpg)`.
  - Audio: `extract_cover(...)` only when a stream has `disposition.attached_pic == 1`.
  - Write into `app/static/tmp/<gibberish>.jpg`, then turn it into a `File` exactly as `edit_post`'s uploaded-image branch does: Pillow open, `exif_transpose`, `thumbnail(MEDIA_IMAGE_MAX_DIMENSION)`, `save_image_without_metadata` in `MEDIA_IMAGE_FORMAT`, under `app/static/media/posts/xx/yy/`, a `File` row, then `make_image_sizes(file.id, 512, 1200, 'posts', community.low_quality)`.
  - Set `post.image = file`.
  - Always remove the tmp jpg (`try/finally`).
- **S3**, when `store_files_in_s3()`: `upload_file_to_s3(path, key, mime)`, set `post.url` to the returned URL, then `os.unlink(path)`. The S3 part runs only after validation succeeds.
- `with_media(post, status='ready', **meta, thumbnail_file_id=post.image_id or omitted)`, then `publish_ready_post(post)`.
- **Failure:** any `MediaToolError`, or `OSError` from thumbnail/S3 work:
  - Roll back the partial work: delete a created `File` row and its files via `File.delete_from_disk`.
  - `with_media(post, status='failed', error=str(e))`, then commit.
  - Notify the author:
    ```python
    with force_locale(get_recipient_language(author.id)):
        Notification(user_id=author.id, title=gettext('Your upload could not be processed: %(reason)s', reason=...),
                     url=f'/post/{post.id}', author_id=author.id, notif_type=NOTIF_DEFAULT, subtype='media_failed',
                     targets={'post_id': post.id})
    ```
    Then `author.unread_notifications += 1`.
  - If notification subtypes need a template in `app/templates/user/notifs/`, add `media_failed.html` modelled on a simple existing one.

```python
def publish_ready_post(post) -> None:
    """The scheduled-post publisher's sequence (app/cli.py publish_scheduled_posts): a scheduled upload waits for it."""
    if post.scheduled_for:
        post.status = POST_STATUS_SCHEDULED
        db.session.commit()
        return
    post.status = POST_STATUS_PUBLISHED
    post.posted_at = utcnow()
    db.session.commit()
    task_selector('make_post', post_id=post.id)
    notify_about_post(post)
```

  Import `db`, `utcnow`, `task_selector` and `notify_about_post` at module top. If one forms a cycle, restructure; do not inline.

- [ ] **Step 4:** Fill `queue_probe` in `app/shared/media.py` to call `task_selector('probe_media', post_id=post_id)`, and register the task. Add an end-to-end test: upload `video_h264.mp4` through the route with eager celery. The post is published and the media is ready in one request. Run the file and the Task 4 file. Expected: PASS.
- [ ] **Step 5:** Commit: `feat: probe_media validates uploads in a sandbox, thumbnails them and publishes the post`.

### Task 6: Purge removes media

**Files:**
- Modify: `app/models.py` `Post.delete_dependencies` (:3453, the video S3 branch ~:3492)
- Test: `tests/test_media_delete.py`

- [ ] **Step 1: Write the failing tests.** Each case is a real test that creates a ready media post through `store_uploaded_media` plus `probe_media`, then runs the purge.
  - Local video: after `post.delete_dependencies()` and `db.session.delete(post)`, neither the media file nor the thumbnail's files exist.
  - Local audio: the same.
  - S3 video and S3 audio (`s3_bucket`): the media object is deleted from the bucket.
  - S3 media whose URL another post also references (`s3_object_is_referenced_elsewhere`): it stays.
  - A soft delete (`delete_post`) leaves the files, matching existing behaviour (A13.8).

- [ ] **Step 2:** Run. Expected: the local cases FAIL, because there is no local branch today.

- [ ] **Step 3: Implement.** In `delete_dependencies`, generalize the video S3 branch to `media_of(self) is not None or self.type == POST_TYPE_VIDEO`:
  - With S3: delete the `s3_key_from_url(self.url)` object exactly as now.
  - Otherwise, when `media_local_path(self)` is set and exists: `os.unlink` it.
  - The thumbnail is `self.image`, so the existing image removal handles it.
  - Importing `app.shared.media` from `app/models.py` may cycle (`media.py` → `app.utils` → `app.models`). If it does, move `media_of` and `media_local_path` into a dependency-free module, e.g. `app/media_record.py` importing nothing from `app`, which `media.py` re-exports. Do not inline-import.

- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit: `fix: purging a media post removes its file locally and in S3`.

### Task 7: Interop end to end, Range 206, deploy docs

**Files:**
- Create: `interop/test_media_pipeline.py` (marker `interop`)
- Modify: `interop/seed_piefed.py`: a `video_channel` community `vc_media` and a `podcast` community `pod_media`, both moderated by alice. Set `allow_video_file_uploads`/`allow_audio_file_uploads` to `users`.
- Modify: `interop/clients.py` if a PieFed web-session helper is needed. Log in through the web form, the way the existing clients do any web call; reuse it if present.
- Modify: `deploy/DEPLOY_GUIDE.md`, `INSTALL.md` (nginx section ~:514), and the repo `Caddyfile` with `request_body { max_size 510MB }` on the PieFed site block.
- Modify: `docs/superpowers/specs/2026-10-14-native-federation-design.md`: add a line under A13 stating the gates' results once done.

- [ ] **Step 1: Write the interop test.** Use the harness PieFed image, which now has ffmpeg.
  - alice uploads `tests/fixtures/media/video_h264.mp4` to `vc_media` through the web form.
  - Poll the post, bounded to 60 s, until `extensions.media.status == 'ready'`. Read it through `GET /api/alpha/post?id=` or the DB via the seed's exec helper, whichever the harness already uses.
  - `GET` the media URL through Caddy with `Range: bytes=0-99`: `206`, `Content-Range` starts `bytes 0-99/`, and `Access-Control-Allow-Origin: *`.
  - The same for `audio.mp3` in `pod_media`.
  - Upload `not_media.mp4`: no post is created.

- [ ] **Step 2:** Run it: `interop/run.sh test interop/test_media_pipeline.py`. Expected: PASS. If the Range answer is not 206, fix the serving path, not the test.
- [ ] **Step 3: Docs.**
  - `DEPLOY_GUIDE.md` gets a "Media uploads" subsection. It covers the body limit (Caddy `request_body max_size`, nginx `client_max_body_size`), set to the larger upload limit plus 10 MB.
  - It states that `ffmpeg` is in the image.
  - It states that step 3 must not ship before step 4 (A13.6).
- [ ] **Step 4:** Run all gates:
  - The Global Constraints chain.
  - `interop/run.sh test`: the whole interop suite, then `interop/run.sh down`.
  - Expected: everything green.
- [ ] **Step 5:** Commit: `test: media uploads end to end in the harness; deploy docs for media body limits`.

---

## Self-review notes

- **Spec coverage:**

  | Spec section | Task |
  |---|---|
  | §3.4 | Tasks 3–5 |
  | §3.5 | Task 1 |
  | §5.1 | Tasks 3–4 |
  | §5.2 | Tasks 2 and 5 |
  | §5.3 | Task 4 |
  | §5.4 | Task 7 |
  | §5.5 | Task 6 |
  | §5.6 | Task 2 |
  | S3 path | Task 5 |
  | Deploy guide | Task 7 |

  Episode fields in `extensions['podcast']` (§3.4, second paragraph) belong to step 6, not here.
- **Out of scope:** serializers and delivery of `Video`/`Audio` objects (step 4 and step 6), forms that set `Community.kind` (step 4), and transcoding (never; R6).
- **Type consistency:**
  - `media_of`, `with_media`, `media_local_path`, `store_uploaded_media` and `queue_probe` live in `app/shared/media.py`, unless Task 6 moves `media_of`/`media_local_path` to break a cycle; it re-exports them.
  - `validate_probe` and `probe_media` live in `app/shared/tasks/media.py`.
  - The kinds are `'video'`/`'audio'` everywhere.
