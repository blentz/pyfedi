"""What every service makes of a post: its text, cut where a service needs it, and its pictures as JPEGs within a
service's size limit (spec §6). The services shape these into their own posts."""
import io
import os
import re
import unicodedata

from PIL import Image, ImageOps

from app.connections import ServiceError
from app.connections.http import call

ZERO_WIDTH_JOINERS = (chr(0x200D), chr(0xFE0F))   # a cut never ends on a joiner or an emoji presentation selector
MARKDOWN_LINK = re.compile(r'!?\[([^\]]*)\]\((https?://[^)\s]+)\)')
PICTURE_BYTES = 25_000_000   # a picture of the post's, fetched from its own address
JPEG_QUALITIES = (85, 75, 65, 50, 35)


def plain_text(post) -> str:
    """The post as text: its title (when the body does not already start with it) and its Markdown body, with
    Markdown links written out so their addresses show (and become links where a service makes them)."""
    body = MARKDOWN_LINK.sub(lambda m: f'{m.group(1)} {m.group(2)}'.strip(), post.body or '').strip()
    title = (post.title or '').strip()
    if title and not body.startswith(title[:40]):
        return f'{title}\n\n{body}'.strip()
    return body


def cut(text: str, limit: int) -> str:
    """At most `limit` code points of `text`, not ending inside a combining sequence, then an ellipsis."""
    if len(text) <= limit:
        return text
    end = limit - 1
    # back off while the first character dropped belongs to the one before it (an accent, a joiner, a presentation
    # selector), or the last kept is a joiner: the whole joined sequence goes, never half of it
    while end > 0 and (unicodedata.combining(text[end]) or text[end] in ZERO_WIDTH_JOINERS
                       or text[end - 1] in ZERO_WIDTH_JOINERS):
        end -= 1
    return text[:end].rstrip() + '…'


def image_bytes(file) -> bytes | None:
    """A picture's bytes: its stored file here, or its address."""
    path = file.file_path
    if path and os.path.isfile(path):
        with open(path, 'rb') as handle:
            return handle.read()
    if file.source_url and file.source_url.startswith('https://'):
        try:
            return call('GET', file.source_url, max_bytes=PICTURE_BYTES).content
        except ServiceError:
            return None
    return None


def as_jpeg(data: bytes, max_bytes: int, max_side: int) -> tuple[bytes, int, int] | None:
    """The picture as a JPEG of at most `max_bytes` and `max_side` pixels a side, with its width and height; None
    when it is no picture, or will not fit."""
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    except Exception:
        return None
    image = image.convert('RGB')
    image.thumbnail((max_side, max_side))
    for quality in JPEG_QUALITIES:
        out = io.BytesIO()
        image.save(out, 'JPEG', quality=quality, optimize=True)
        if out.tell() <= max_bytes:
            return out.getvalue(), image.width, image.height
        image.thumbnail((image.width * 3 // 4, image.height * 3 // 4))
    return None
