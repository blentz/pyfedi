"""D467, fixed: no request path rewrites Pillow's process-wide pixel ceiling.

Eight upload and fetch paths assigned `Image.MAX_IMAGE_PIXELS = 89478485`, a
class attribute shared by the whole process. That value is Pillow's own
default, so the assignments widened nothing; what they did was silently undo
any narrower ceiling a deployment (or a test) had set, on the first upload.
They are gone, so the ceiling is whatever the process configured.

And the process configures it once: app/__init__.py sets it at import, to half
IMAGE_PIXEL_CAP, because Pillow raises DecompressionBombError only above TWICE
the ceiling (and merely warns above it). That is the one assignment allowed.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_no_app_code_assigns_the_pillow_pixel_ceiling():
    pattern = re.compile(r'MAX_IMAGE_PIXELS\s*=(?!=)')
    hits = [f'{path.relative_to(ROOT)}:{number}'
            for path in sorted((ROOT / 'app').rglob('*.py'))
            for number, line in enumerate(path.read_text().splitlines(), start=1)
            if pattern.search(line)]

    assert hits == ['app/__init__.py:' + str(_the_one_assignment())]


def _the_one_assignment():
    lines = (ROOT / 'app' / '__init__.py').read_text().splitlines()
    return next(number for number, line in enumerate(lines, start=1)
                if 'Image.MAX_IMAGE_PIXELS = IMAGE_PIXEL_CAP // 2' in line)


def test_pillow_refuses_exactly_above_the_cap():
    """IMAGE_PIXEL_CAP is the largest image any decode path accepts: Pillow raises above twice MAX_IMAGE_PIXELS."""
    import app  # NOQA  # the import is what sets the ceiling
    from PIL import Image

    from app.constants import IMAGE_PIXEL_CAP

    assert IMAGE_PIXEL_CAP == 50_000_000
    assert Image.MAX_IMAGE_PIXELS * 2 == IMAGE_PIXEL_CAP
    Image._decompression_bomb_check((IMAGE_PIXEL_CAP, 1))
    try:
        Image._decompression_bomb_check((IMAGE_PIXEL_CAP + 1, 1))
    except Image.DecompressionBombError:
        pass
    else:
        raise AssertionError('one pixel over the cap was not refused')
