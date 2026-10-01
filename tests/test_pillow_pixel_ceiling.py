"""D467, fixed: no request path rewrites Pillow's process-wide pixel ceiling.

Eight upload and fetch paths assigned `Image.MAX_IMAGE_PIXELS = 89478485`, a
class attribute shared by the whole process. That value is Pillow's own
default, so the assignments widened nothing; what they did was silently undo
any narrower ceiling a deployment (or a test) had set, on the first upload.
They are gone, so the ceiling is whatever the process configured.
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

    assert hits == []
