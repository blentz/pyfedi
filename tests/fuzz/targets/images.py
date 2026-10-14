"""Property checks for image decode (tier T7).

Every image PieFed stores is decoded by Pillow first, and the bytes are somebody else's: a user's upload
(app/shared/upload.py process_upload, app/shared/post.py edit_post, app/community/util.py save_icon_file and
save_banner_file) or a peer's thumbnail (app/utils.py url_to_thumbnail_file). Pillow's C decoders are the main
crash surface, and how many of them a hostile file can reach depends on which formats each path lets through.

The decode/transform steps are inline in those functions, between writing the upload to disk and writing the
results back, so each check below runs the same steps in the same order, on BytesIO instead of files, calling
the production helpers they call (to_srgb, scale_gif, get_new_frames, save_new_gif) and reading the production
allowlists (app.community.util.allowed_extensions, app.utils.allowed_thumbnail_extensions). Each step names the
lines it mirrors. A selector byte at the front of the input picks the configuration the step depends on: the
instance's MEDIA_IMAGE_*FORMAT settings and the uploaded file's extension.

Properties:

* upload_decode -- the process_upload and edit_post sequence: open, the format check, exif_transpose, convert (or
  to_srgb on edit_post's JPEG arm), thumbnail to MEDIA_IMAGE_MAX_DIMENSION, save. Only Pillow's refusals escape
  (see below); an image the format check passes is one of the formats the extension list names; the result is RGB
  for a JPEG target and RGBA otherwise, fits MEDIA_IMAGE_MAX_DIMENSION on both sides, and saves and reopens as
  the format it was saved as, at that size.
* icon_banner_decode -- save_icon_file (its still-image and its animated-GIF arm) and save_banner_file: the same,
  with their own caps (250 and 40 for an icon and its thumbnail, 1600x600 and 878x500 for a banner), and every
  image either path decodes is one of the formats the extension list names.
* remote_thumbnail_decode -- url_to_thumbnail_file's resize: any format Pillow opens (a peer chooses), exif_transpose,
  convert, the 170 and 512 thumbnails, saved in MEDIA_IMAGE_MEDIUM_FORMAT. The function catches Exception around
  all of it, so the property is the output: both thumbnails fit their caps, are RGB or RGBA as configured, and
  reopen as saved.
* to_srgb -- any image Pillow opens and loads, with its own ICC profile or with the input's bytes as one, comes
  out of to_srgb as RGB at the same size, and to_srgb itself raises nothing.
* scale_gif -- scale_gif on any image Pillow opens gives an animation with one frame per input frame, each within
  the scale. Not "no larger than the input": production calls it to bring the canvas down to 250x250 and 40x40,
  and a re-encode of full RGBA frames is routinely more bytes than a delta-coded GIF of the same frames.
* c2pa_inspect -- inspect_image_c2pa returns its dict, of exactly its shape and types, for any bytes and any
  mimetype string (the mimetype is the browser's to send), and raises nothing -- not even a Rust panic, which
  surfaces as pyo3's PanicException, a BaseException its `except Exception` does not catch.

A refusal is an exception Pillow (or its HEIF/AVIF plugin) raises from its own code for a file it will not
decode. The types are not a closed set: besides UnidentifiedImageError, OSError, ValueError and
DecompressionBombError, the first campaigns found "SyntaxError: broken PNG file" out of load() for a PNG whose chunk
after IDAT is garbage (Image.open turns SyntaxError into UnidentifiedImageError, load() does not), and pillow_avif's
"RuntimeError: Failed to decode frame 0" for a damaged AVIF (upload_decode's png_broken_chunk_after_idat and
avif_color_planes_fail_to_decode). So the production paths have to catch Exception around their Pillow steps, as
url_to_thumbnail_file does (D1328), and a refusal here is judged by where it was raised, not by its type (_refused).
An exception raised in PieFed's own code -- app/, or a check -- is the finding.

Decompression bombs. Pillow's guard is in force on every path -- nothing in app/ changes Image.MAX_IMAGE_PIXELS --
but it only RAISES above twice that (178,956,970 pixels); between MAX_IMAGE_PIXELS and twice it, it warns, and a
warning is not a refusal. A 140 KB PNG of 12000x12000 decodes through upload_decode's steps in about 4 s and 1.2
GB. Whether to refuse those is the owner's call (a 108-megapixel phone photo is in that band), so the checks escalate
DecompressionBombWarning to a refusal: a campaign then spends its time on the codecs instead of re-finding that.

Frame counts. Nothing bounds the frames get_new_frames walks, and each costs a full-canvas RGBA paste and a
thumbnail: a 14 KB GIF of 400 frames on a 2000x2000 screen (each frame a one-pixel delta) took 30 s of CPU and 3.3
GB in get_new_frames -- reachable through save_icon_file's .gif arm, so through any user's avatar. Refusing such
files is again the owner's call, so scale_gif skips inputs of more than MAX_FRAMES frames rather than judging them,
and the per-input budget catches a canvas-times-frames product that grows past what the seeds take.

Plugins: register_heif_opener() and `import pillow_avif` are called at import, because production calls them on
the first .heic/.heif/.avif upload and they stay registered for the life of the worker -- after that every later
Image.open in the process accepts HEIF and AVIF whatever the file is called.

No database, no network, no files: everything is BytesIO.
"""

import io
import os
import warnings

import PIL
import pillow_avif  # NOQA  # registers the plugin, as production's first AVIF upload does
from PIL import Image, ImageOps
import pillow_heif
from pillow_heif import register_heif_opener

from app.community.util import allowed_extensions
from app.utils import allowed_thumbnail_extensions, inspect_image_c2pa, scale_gif, to_srgb
from config import Config
from tests.fuzz.targets._target import Target

register_heif_opener()
# Every plugin, now rather than on the first unknown format: run_campaign imports this module inside
# atheris.instrument_imports, and only a module imported there is instrumented (see its comment on PIL).
# Production loads them all the same way, on the first Image.open of a format preinit() does not cover.
Image.init()

# Where a refusal is raised from: Pillow and the two plugins production registers.
PILLOW_PACKAGES = tuple(os.path.dirname(module.__file__) + os.sep for module in (PIL, pillow_avif, pillow_heif))

# The upload cap (R207) is 10 MB, but a fuzz input past 64 KiB says nothing about the codecs a shorter one does not,
# and the seeds stay under it.
MAX_INPUT = 64 * 1024

MAX_DIMENSION = Config.MEDIA_IMAGE_MAX_DIMENSION

# The values MEDIA_IMAGE_FORMAT and its medium/thumbnail siblings take: unset (keep the upload's own format), or a
# format to re-encode everything as.
FORMATS = ('', 'JPEG', 'WEBP', 'PNG', 'AVIF')

# The extension an upload is saved under decides two things in these sequences: RGB or RGBA, and the save format
# when no MEDIA_IMAGE_FORMAT is set. '.svg' and '.gif' never reach the still-image sequences. '.mpo' is left out
# deliberately: an RGBA image cannot be saved as MPO ("cannot write mode RGBA as JPEG"), so with no format
# configured every .mpo upload fails at the save in all four upload paths -- reported to the owner rather than
# changed here, because the fix makes an input that is refused today succeed.
UPLOAD_EXTENSIONS = tuple(ext for ext in allowed_extensions if ext not in ('.svg', '.gif', '.mpo'))
THUMBNAIL_EXTENSIONS = tuple(ext for ext in allowed_thumbnail_extensions if ext != '.svg') + ('.img',)


def accepted_format(img) -> bool:
    """process_upload's and edit_post's format check, which save_banner_file spells differently to the same effect:
    the format Pillow read is one the extension list names, HEIF standing for .heic (D579)."""
    return '.' + img.format.lower() in allowed_extensions or (img.format == 'HEIF' and '.heic' in allowed_extensions)


ACCEPTED_FORMATS = frozenset(ext[1:].upper() for ext in allowed_extensions) | {'HEIF'}


def _open(data: bytes):
    """Image.open as production calls it, except that a DecompressionBombWarning is a refusal (module docstring)."""
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        return Image.open(io.BytesIO(data))


def _open_loaded(data: bytes):
    img = _open(data)
    img.load()
    return img


def raised_by_pillow(exc: BaseException) -> bool:
    """Whether `exc` was raised from Pillow's or a plugin's own code: the innermost frame of its traceback."""
    tb = exc.__traceback__
    while tb.tb_next is not None:
        tb = tb.tb_next
    return tb.tb_frame.f_code.co_filename.startswith(PILLOW_PACKAGES)


def _refused(step, *args):
    """step(*args), or None when Pillow refuses (module docstring). Anything else propagates: an AssertionError is
    a check's property break, and any other exception raised outside Pillow is PieFed's."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            return step(*args)
    except AssertionError:
        raise
    except Exception as exc:
        if isinstance(exc, Image.DecompressionBombWarning) or raised_by_pillow(exc):
            return None
        raise


def _save(img, fmt: str, quality=None) -> bytes:
    """img.save(path, optimize=True, format=..., quality=...) as the production paths call it, into memory."""
    buffer = io.BytesIO()
    kwargs = {'quality': quality} if quality else {}
    img.save(buffer, format=fmt, optimize=True, **kwargs)
    return buffer.getvalue()


def _format_of(ext: str) -> str:
    """The format Pillow saves `path` as when no format is given: the one registered for its extension."""
    return Image.registered_extensions()[ext]


def _assert_saved(saved: bytes, fmt: str, size, label: str) -> None:
    reopened = Image.open(io.BytesIO(saved))
    assert reopened.format == fmt, f'{label}: saved as {fmt}, reopened as {reopened.format}'
    assert reopened.size == size, f'{label}: saved at {size}, reopened at {reopened.size}'


def _fits(img, box, label: str) -> None:
    assert 1 <= img.width <= box[0] and 1 <= img.height <= box[1], f'{label}: {img.size} does not fit {box}'


# ---------------------------------------------------------------------------
# upload_decode: app/shared/upload.py:64-79, app/shared/post.py:608-627
# ---------------------------------------------------------------------------

def _upload_steps(img, ext: str, image_format: str, srgb: bool):
    # D579: Pillow reports a .heic file as 'HEIF'. A format the list does not name: 'filetype not allowed'.
    if not accepted_format(img):
        return None
    assert img.format in ACCEPTED_FORMATS, f'the format check let {img.format} through'
    img = ImageOps.exif_transpose(img)
    rgb = image_format == 'JPEG' or ext in ['.jpg', '.jpeg']
    if srgb and rgb:    # edit_post converts its JPEG arm with to_srgb; process_upload converts it plainly
        img = to_srgb(img)
    else:
        img = img.convert('RGB' if rgb else 'RGBA')
    img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), resample=Image.LANCZOS)
    return img, rgb


def check_upload_decode(data: bytes) -> None:
    """Selector byte: bit 0 edit_post (else process_upload); bits 1-3 MEDIA_IMAGE_FORMAT; bits 4-7 extension."""
    data = data[:MAX_INPUT]
    if not data:
        return
    selector, image = data[0], data[1:]
    srgb = bool(selector & 1)
    image_format = FORMATS[(selector >> 1 & 7) % len(FORMATS)]
    ext = UPLOAD_EXTENSIONS[(selector >> 4) % len(UPLOAD_EXTENSIONS)]

    img = _refused(_open, image)
    if img is None:
        return
    result = _refused(_upload_steps, img, ext, image_format, srgb)
    if result is None:
        return
    img, rgb = result
    assert img.mode == ('RGB' if rgb else 'RGBA'), f'{ext} with format {image_format!r} came out {img.mode}'
    _fits(img, (MAX_DIMENSION, MAX_DIMENSION), 'upload')
    fmt = image_format.upper() if image_format else _format_of(ext)
    _assert_saved(_save(img, fmt, Config.MEDIA_IMAGE_QUALITY), fmt, img.size, 'upload')


# ---------------------------------------------------------------------------
# icon_banner_decode: app/community/util.py save_icon_file:798-858, save_banner_file:952-1003
# ---------------------------------------------------------------------------

class _NamedBuffer(io.BytesIO):
    """A BytesIO with a file name, so scale_gif's save, which passes no format, picks GIF from the name as it does
    for the path production gives it."""
    name = 'fuzz.gif'


def _icon_gif(image: bytes):
    """save_icon_file's animated-GIF arm, util.py:798-817: whatever the bytes are, under a .gif name."""
    img = _open(image)
    assert img.format in ACCEPTED_FORMATS, f'save_icon_file decoded a {img.format} as a .gif icon'
    if img.width > 250 or img.height > 250:
        scaled = _NamedBuffer()
        scale_gif(io.BytesIO(image), (250, 250), scaled)
        img = Image.open(io.BytesIO(scaled.getvalue()))
        image = scaled.getvalue()
    _fits(img, (250, 250), 'gif icon')
    thumbnail = _NamedBuffer()
    scale_gif(io.BytesIO(image), (40, 40), thumbnail)
    _fits(Image.open(io.BytesIO(thumbnail.getvalue())), (40, 40), 'gif icon thumbnail')


def _two_sizes(img, ext, image_format, thumbnail_format, box, thumbnail_box, label):
    """The resize-and-thumbnail tail both functions share (util.py:832-858 and 974-1000)."""
    if img.width > box[0] or img.height > box[1] or image_format or thumbnail_format:
        img = img.convert('RGB' if (image_format == 'JPEG' or ext in ['.jpg', '.jpeg']) else 'RGBA')
        img.thumbnail(box, resample=Image.LANCZOS)
        fmt = image_format.upper() if image_format else _format_of(ext)
        _assert_saved(_save(img, fmt, Config.MEDIA_IMAGE_QUALITY), fmt, img.size, label)
        _fits(img, box, label)
    img = img.convert('RGB' if thumbnail_format == 'JPEG' else 'RGBA')
    img.thumbnail(thumbnail_box, resample=Image.LANCZOS)
    fmt = thumbnail_format.upper() if thumbnail_format else 'WEBP'    # the path is '<name>_thumbnail.webp'
    _assert_saved(_save(img, fmt, Config.MEDIA_IMAGE_THUMBNAIL_QUALITY), fmt, img.size, label + ' thumbnail')
    _fits(img, thumbnail_box, label + ' thumbnail')


def _icon_still(image: bytes, ext, image_format, thumbnail_format):
    """save_icon_file's still-image arm, util.py:818-858."""
    img = _open(image)
    assert img.format in ACCEPTED_FORMATS, f'save_icon_file decoded a {img.format} as a {ext} icon'
    img = ImageOps.exif_transpose(img)
    _two_sizes(img, ext, image_format, thumbnail_format, (250, 250), (40, 40), 'icon')


def _banner(image: bytes, ext, image_format, thumbnail_format):
    """save_banner_file, util.py:952-1003."""
    img = _open(image)
    img_ext = '.heic' if img.format == 'HEIF' else '.' + img.format.lower()
    if img_ext not in allowed_extensions:
        return  # abort(400)
    assert img.format in ACCEPTED_FORMATS, f'save_banner_file let {img.format} through'
    img = ImageOps.exif_transpose(img)
    _two_sizes(img, ext, image_format, thumbnail_format, (1600, 600), (878, 500), 'banner')


def check_icon_banner_decode(data: bytes) -> None:
    """Selector byte: bits 0-1 the path (icon still, icon GIF, banner, banner); bits 2-4 MEDIA_IMAGE_FORMAT;
    bit 5 MEDIA_IMAGE_THUMBNAIL_FORMAT WEBP (its default), else the same as MEDIA_IMAGE_FORMAT; bits 6-7 the
    extension."""
    data = data[:MAX_INPUT]
    if not data:
        return
    selector, image = data[0], data[1:]
    path = selector & 3
    image_format = FORMATS[(selector >> 2 & 7) % len(FORMATS)]
    thumbnail_format = 'WEBP' if selector & 32 else FORMATS[(selector >> 2 & 7) % len(FORMATS)]
    ext = UPLOAD_EXTENSIONS[(selector >> 6) % len(UPLOAD_EXTENSIONS)]
    if path == 1:
        _refused(_icon_gif, image)
    elif path == 0:
        _refused(_icon_still, image, ext, image_format, thumbnail_format)
    else:
        _refused(_banner, image, ext, image_format, thumbnail_format)


# ---------------------------------------------------------------------------
# remote_thumbnail_decode: app/utils.py url_to_thumbnail_file:4541-4568
# ---------------------------------------------------------------------------

def check_remote_thumbnail_decode(data: bytes) -> None:
    """Selector byte: bits 0-2 MEDIA_IMAGE_MEDIUM_FORMAT; bits 3-7 the extension the content type mapped to.

    Production wraps every step in `except Exception` and drops the thumbnail, so any exception is a refusal
    here; what is checked is what a thumbnail that survives looks like."""
    data = data[:MAX_INPUT]
    if not data:
        return
    selector, image = data[0], data[1:]
    medium_format = FORMATS[(selector & 7) % len(FORMATS)]
    final_ext = THUMBNAIL_EXTENSIONS[(selector >> 3) % len(THUMBNAIL_EXTENSIONS)]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image)) as img:
                img = ImageOps.exif_transpose(img)
                rgb = medium_format == 'JPEG' or final_ext in ['.jpg', '.jpeg']
                img = img.convert('RGB' if rgb else 'RGBA')
                fmt = medium_format.upper() if medium_format else _format_of(final_ext)
                sizes = []
                for box in ((170, 170), (512, 512)):
                    copy = img.copy()
                    copy.thumbnail(box, resample=Image.LANCZOS)
                    sizes.append((copy, box, _save(copy, fmt, Config.MEDIA_IMAGE_MEDIUM_QUALITY)))
    except Exception:
        return
    for copy, box, saved in sizes:
        assert copy.mode == ('RGB' if rgb else 'RGBA'), f'{final_ext}/{medium_format!r} thumbnail is {copy.mode}'
        _fits(copy, box, f'remote {box[0]} thumbnail')
        _assert_saved(saved, fmt, copy.size, f'remote {box[0]} thumbnail')


# ---------------------------------------------------------------------------
# to_srgb: app/utils.py
# ---------------------------------------------------------------------------

def check_to_srgb(data: bytes) -> None:
    """Selector byte: bit 0 replaces the image's ICC profile with the next field's bytes (a two-byte big-endian
    length, then that many bytes); the rest is the image."""
    data = data[:MAX_INPUT]
    if not data:
        return
    selector, rest = data[0], data[1:]
    icc = None
    if selector & 1:
        length = int.from_bytes(rest[:2], 'big')
        icc, rest = rest[2:2 + length], rest[2 + length:]
    img = _refused(_open_loaded, rest)
    if img is None:
        return
    if icc is not None:
        img.info['icc_profile'] = icc
    out = to_srgb(img)
    assert out.mode == 'RGB', f'to_srgb gave {out.mode} for {img.mode}'
    assert out.size == img.size, f'to_srgb changed {img.size} to {out.size}'


# ---------------------------------------------------------------------------
# scale_gif: app/utils.py scale_gif, get_new_frames, save_new_gif
# ---------------------------------------------------------------------------

MAX_FRAMES = 1000


def _scale(image: bytes, scale):
    source = _open(image)
    frames = getattr(source, 'n_frames', 1)
    # The frame count of the input is the bound on the work: get_new_frames does one paste and one thumbnail per
    # frame. Inputs with more are skipped here, not judged -- see the module docstring's note on frame counts.
    if frames > MAX_FRAMES:
        return None
    scaled = _NamedBuffer()
    scale_gif(io.BytesIO(image), scale, scaled)
    return frames, Image.open(io.BytesIO(scaled.getvalue()))


def check_scale_gif(data: bytes) -> None:
    """Selector byte: the scale, 1-256 on both sides; the rest is the image."""
    data = data[:MAX_INPUT]
    if not data:
        return
    scale = (data[0] + 1, data[0] + 1)
    result = _refused(_scale, data[1:], scale)
    if result is None:
        return
    frames, out = result
    out_frames = getattr(out, 'n_frames', 1)
    # save_new_gif may merge consecutive identical frames (Pillow does, adding their durations), never add one.
    assert 1 <= out_frames <= frames, f'{frames} frames in, {out_frames} out'
    _fits(out, scale, 'scaled gif')


# ---------------------------------------------------------------------------
# c2pa_inspect: app/utils.py inspect_image_c2pa
# ---------------------------------------------------------------------------

def check_c2pa_inspect(data: bytes) -> None:
    """The first field (two-byte big-endian length, then that many bytes, as UTF-8 with replacement) is the
    mimetype the browser sent; the rest is the file."""
    data = data[:MAX_INPUT]
    length = int.from_bytes(data[:2], 'big')
    mimetype = data[2:2 + length].decode('utf-8', errors='replace')
    body = data[2 + length:]
    result = inspect_image_c2pa(body, mimetype)
    assert isinstance(result, dict) and list(result) == ['c2pa'], f'{result!r:.300}'
    c2pa = result['c2pa']
    assert isinstance(c2pa, dict) and list(c2pa) == ['present', 'ai_generated', 'creator', 'software'], f'{c2pa!r:.300}'
    assert isinstance(c2pa['present'], bool) and isinstance(c2pa['ai_generated'], bool), f'{c2pa!r:.300}'
    assert c2pa['creator'] is None or isinstance(c2pa['creator'], str), f'creator {c2pa["creator"]!r:.300}'
    assert c2pa['software'] is None, f'software {c2pa["software"]!r:.300}'
    assert not c2pa['ai_generated'] or c2pa['present'], 'AI-generated without a manifest'


# Budgets are about ten times the slowest committed seed, measured 2026-10-14 in the test container (CPU time):
# upload_decode 36 ms (heif), icon_banner_decode 8 ms, remote_thumbnail_decode 8 ms, to_srgb 8 ms, scale_gif 6 ms,
# c2pa_inspect 0.4 ms. The campaign's -timeout is ten times the budget again, so a decode that runs for seconds --
# the shape of a decompression bomb Pillow lets through -- comes out of a campaign as a timeout artifact.
TARGETS = [
    Target(name='upload_decode', check=check_upload_decode, surface='images', budget_ms=500, needs_app=False),
    Target(name='icon_banner_decode', check=check_icon_banner_decode, surface='images', budget_ms=200,
           needs_app=False),
    Target(name='remote_thumbnail_decode', check=check_remote_thumbnail_decode, surface='images', budget_ms=200,
           needs_app=False),
    Target(name='to_srgb', check=check_to_srgb, surface='images', budget_ms=200, needs_app=False),
    Target(name='scale_gif', check=check_scale_gif, surface='images', budget_ms=200, needs_app=False),
    Target(name='c2pa_inspect', check=check_c2pa_inspect, surface='images', budget_ms=50, needs_app=False),
]
