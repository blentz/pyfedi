"""Uploads that carry metadata the stored copy must not, for the tests of every path that re-encodes an image.

A phone's JPEG carries EXIF -- GPS position, camera make and model -- and often XMP. Pillow reads both into
`img.info`, and they survive exif_transpose, convert and thumbnail. The pillow_avif writer copies them from
`im.info` into the file it writes, so with MEDIA_IMAGE_FORMAT=AVIF the uploader's location was published with the
image. A malformed EXIF block made the same writer raise `ValueError: Failed to encode image: Invalid Exif
payload`, or write an AVIF Pillow cannot reopen. The two malformed samples are the fuzz findings, committed under
tests/fuzz/corpus/upload_decode/ (the first byte there is the target's selector).
"""

import io
import pathlib

from PIL import Image

CORPUS = pathlib.Path(__file__).parent / 'fuzz' / 'corpus' / 'upload_decode'

GPS_IFD = 0x8825
MAKE = 0x010F
XMP = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF/></x:xmpmeta>'

# Every key Pillow reads EXIF or XMP into, across the formats these paths save.
METADATA_KEYS = ('exif', 'xmp', 'XML:com.adobe.xmp')


def jpeg_with_gps_exif(size=(16, 12)) -> bytes:
    """A JPEG whose EXIF names a camera make and a GPS position, with an XMP packet beside it."""
    image = Image.new('RGB', size, (120, 130, 140))
    exif = image.getexif()
    exif[MAKE] = 'Probe Camera Co'
    exif.get_ifd(GPS_IFD).update({1: 'N', 2: (51.0, 30.0, 0.0), 3: 'W', 4: (0.0, 7.0, 0.0)})
    buffer = io.BytesIO()
    image.save(buffer, format='JPEG', exif=exif, xmp=XMP)
    return buffer.getvalue()


def jpeg_with_malformed_exif(name: str) -> bytes:
    """One of the fuzz findings' JPEGs: 'avif_invalid_exif_payload' or 'avif_unreadable_exif'."""
    return (CORPUS / name).read_bytes()[1:]


MALFORMED_EXIF = ('avif_invalid_exif_payload', 'avif_unreadable_exif')


def assert_no_metadata(path_or_file, fmt=None) -> None:
    """The stored image reopens (as `fmt`, when given) and carries no EXIF and no XMP."""
    with Image.open(path_or_file) as img:
        img.load()
        if fmt is not None:
            assert img.format == fmt
        carried = [key for key in METADATA_KEYS if img.info.get(key)]  # pillow_heif reads 'exif': None
        assert carried == [], f'stored {img.format} carries {carried}'
        assert GPS_IFD not in img.getexif() and MAKE not in img.getexif()
