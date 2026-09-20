# Sub-project 79 slice B: `admin_site` — an upload path that destroys before it validates

**Status:** slice B complete. FOUR production defects fixed (D912-D915); the
design anticipated two. See the findings ledger, D912-D919.
**Branch:** `blentz`, at `219861f1c`
**Predecessor:** slice A, which fixed D898-D900 and left `app/admin/routes.py`
unfloored by design.

## Goal

`admin_site` is 151 of the module's gaps and is almost entirely one thing: the
**site icon upload**. It writes to a served directory, deletes files, shells
out to Pillow and to the SVG sanitizer, and is reachable by anyone holding
`change instance settings`. It is the highest-risk single function in the
module and it is taken second for that reason.

## THE PRODUCTION CHANGES

### P1 — a corrupt upload destroys the site's existing logo

The route unlinks the five `site.logo*` files and the two `logo_512`/`logo_192`
settings files **before** it has established that the upload is an image at
all. Pillow only sees the bytes further down, and when it raises, the old files
are already gone, `db.session.commit()` is never reached, and the row still
points at them.

**Probe**, uploading 24 bytes of ASCII named `evil.png` over an existing logo:

```
PROBE s5 old logo on disk before: True
PROBE s5 RAISED: UnidentifiedImageError cannot identify image file 'app/static/media/logo_JTi2x.png'
PROBE s5 row still points at: /static/media/existing_100.png
PROBE s5 old logo on disk after: False
PROBE s5 logo_180 after: False
```

So the site logo becomes a broken image on **every page**, the database says it
is fine, and the only way back is another upload. The 500 is the visible half;
the destroyed logo is the half nobody would connect to it.

**Fix:** capture the superseded paths up front, and unlink them only once the
new logo has been processed and assigned. Any failure in between then leaves
the existing logo exactly as it was.

### P2 — `.SVG` is sanitized as an SVG and then handed to Pillow

```python
if file_ext.lower() == '.svg' and not sanitize_svg(...):   # case-insensitive
    abort(400)

if file_ext == '.svg':                                     # case-SENSITIVE
```

`allowed_extensions` is checked with `.lower()`, and the form's `FileAllowed`
lowercases too, so `.SVG` is accepted. The sanitize guard directly above is
case-insensitive — its comment explains at length why. The branch predicate
five lines later is not, so a `.SVG` upload falls through to `Image.open`.

**Probe:**

```
PROBE s1 RAISED: UnidentifiedImageError cannot identify image file 'app/static/media/logo_Vyucu.SVG'
PROBE s1 orphans left: ['logo_Vyucu.SVG']
PROBE s2 status: 200 logo: /static/media/logo_grNN1.svg        <- lowercase control
```

A 500, and the uploaded file is left in `app/static/media`, which is served.
It has been sanitized by that point, so the content is not the problem; the
accumulation is — every attempt leaves another file and no code path ever
removes it.

`app/admin/routes.py:206` is **the only `file_ext == '.svg'` in the repository
without `.lower()`**; `app/community/util.py:558,569,723` all have it. An
isolated slip, not a convention.

**Fix:** `.lower()`, and a full decode check before the derivatives are built,
so an undecodable upload is a 400 with its bytes removed rather than a 500 with
them left behind. `.load()` and not `.verify()`: `verify()` reads headers only,
and a truncated image passes it and then raises during `thumbnail()` — which is
exactly the window P1 is about.

## The durable artefact

A row that uploads a file the route cannot decode and asserts **the existing
logo is still on disk and still served**. It is the invariant that P1 broke and
that any future reordering of this block would break again.

## What the slice found beyond the design

- **D914** -- the `img.width > 100` false arm saves to `<base>.png` and then
  deleted `<base>{file_ext}`, which for a PNG upload is the same path. A site
  icon of 100px or less was committed as `site.logo` and unlinked in the same
  request. Found by the row that opened the path the route had just committed:
  `FileNotFoundError: [Errno 2] No such file or directory:
  'app/static/media/logo_qHkJy.png'`.
- **D915** -- fixing P2 made the SANITIZE guard's case load-bearing, because
  the branch now stores the file verbatim and serves it from this origin. The
  mutant reverting that guard survived all 25 rows; the only unsanitizable-SVG
  row used a lowercase name. Closed by parameterising over both cases.

R1 became **D918** and R2 became **D919**. R1's resolution changed: the route's
extension check is KEPT and given a row that relaxes the form's validator, so
deleting it as dead now fails a test.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `app/admin/routes.py:161` vs `app/admin/forms.py:icon` | `allowed_extensions` includes `.gif`, `FileAllowed` does not — so the route's `.gif` entry is **dead**: `PROBE s4 gif status: 200 errors: {'icon': ['Images only!']}`. Harmless today, misleading to read. | A decision about whether GIF site icons are wanted, which is a product question. |
| R2 | `app/admin/routes.py:157` | `request.files['icon']` raises `BadRequestKeyError` when the part is absent, so a non-browser POST gets a bare **400 with no form error** — measured, `PROBE s3 no icon field, status: 400`. Correct status, unhelpful body. | The same shape as D896; belongs with a decision about non-browser clients of the admin forms. |

## Success criteria

- `admin_site` at `[]` on the **full-suite** run.
- No floor yet — slices C through F remain.
- P1 and P2 land with their pins inverted.
- Suite green; floors checked with `&&`; a mutation pass with anchors checked
  first (D833).
- Findings from **D912**; `tests/README.md` facts from **357**.
