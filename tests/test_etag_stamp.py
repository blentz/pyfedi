"""An ETag must be the same in every worker and after every restart: hash() of a datetime is salted per process
(PYTHONHASHSEED), so a validator built on it never matched a client's If-None-Match from another worker."""
import subprocess
import sys
from datetime import datetime

from app.utils import etag_stamp

WHEN = datetime(2026, 10, 14, 3, 3, 54, 135703)


def test_the_stamp_is_the_same_whatever_the_hash_seed():
    code = ('from datetime import datetime; from app.utils import etag_stamp; '
            'print(etag_stamp(datetime(2026, 10, 14, 3, 3, 54, 135703)))')
    seen = {subprocess.run([sys.executable, '-c', code], env={'PYTHONHASHSEED': seed, 'PYTHONPATH': '.',
                            'SERVER_NAME': 'test.piefed.local', 'DATABASE_URL': 'sqlite://'},
                           capture_output=True, text=True, check=True).stdout.strip() for seed in ('1', '2')}
    assert len(seen) == 1 and seen == {etag_stamp(WHEN)}


def test_the_stamp_changes_with_the_value_and_accepts_none():
    assert etag_stamp(WHEN) != etag_stamp(datetime(2026, 10, 14, 3, 3, 54, 135704))
    assert etag_stamp(None) == etag_stamp(None)
