"""Property checks for the files a user uploads to import (tier T6, app/user/routes.py and app/instance/util.py).

Two uploads drive outbound work on a user's behalf:

* the settings import (user settings, Import & Export): a JSON file stored in Redis and read back by
  import_settings_task, whose every entry becomes an actor lookup, a block or a bookmark;
* the Mastodon follows CSV (Add people): a CSV export whose first column is a handle to follow.

Properties:

* settings_import -- what the task reads out of the stored file never raises, whatever the bytes: anything that is
  not a JSON object is no entries at all, and the entries under each key are a list of at most
  SETTINGS_IMPORT_MAX_ENTRIES, each of the shape the task uses it as -- a string, or for user_notes an object with a
  string target and body -- in file order, taken from the first SETTINGS_IMPORT_MAX_ENTRIES the file lists.
* mastodon_csv -- mastodon_csv_handles either raises csv.Error (which the view answers with a message) or returns
  strings that each pass is_fedi_handle and appear, in order, in the file's text as decoded (UTF-8, else Latin-1).

No database and no network: these are the parse steps only, before any lookup.
"""

import csv
import json

from app.instance.util import is_fedi_handle, mastodon_csv_handles
from app.user.routes import SETTINGS_IMPORT_MAX_ENTRIES, import_entries, settings_import_contents
from tests.fuzz.targets._target import Target

# A settings export is a few kilobytes and the upload cap is 5 MiB; a longer fuzz input says nothing a shorter one
# does not. The same goes for a follows CSV.
MAX_INPUT = 64 * 1024

# Every key import_settings_task reads, and whether its entries are actor/domain strings or note objects.
SETTINGS_KEYS = ('followed_communities', 'blocked_communities', 'blocked_users', 'user_notes', 'blocked_instances',
                 'saved_posts', 'saved_comments')


def well_formed_entry(key: str, entry) -> bool:
    """The shape the task uses an entry of `key` as."""
    if key == 'user_notes':
        return isinstance(entry, dict) and isinstance(entry.get('target'), str) and isinstance(entry.get('body'), str)
    return isinstance(entry, str)


def _reference_contents(data: bytes) -> dict:
    try:
        value = json.loads(data)
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def check_settings_import(data: bytes) -> None:
    data = data[:MAX_INPUT]
    contents = settings_import_contents(data)
    assert isinstance(contents, dict), f'settings_import_contents gave a {type(contents).__name__}'
    # Keys, not equality: a NaN in the file is a float that is unequal to itself.
    assert list(contents) == list(_reference_contents(data)), 'a JSON object was not read as itself'
    for key in SETTINGS_KEYS:
        entries = import_entries(contents, key)
        assert isinstance(entries, list), f'{key}: {type(entries).__name__}'
        raw = contents.get(key)
        listed = raw[:SETTINGS_IMPORT_MAX_ENTRIES] if isinstance(raw, list) else []
        assert entries == [entry for entry in listed if well_formed_entry(key, entry)], (
            f'{key}: {entries!r:.300} from {raw!r:.300}')


def check_mastodon_csv(data: bytes) -> None:
    data = data[:MAX_INPUT]
    try:
        handles = mastodon_csv_handles(data)
    except csv.Error:
        return
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        text = data.decode('latin-1')
    position = 0
    for handle in handles:
        assert isinstance(handle, str) and is_fedi_handle(handle), f'{handle!r} is not a fediverse handle'
        found = text.find(handle, position)
        assert found >= 0, f'{handle!r} is not in the file after offset {position}'
        position = found + len(handle)


TARGETS = [
    Target(name='settings_import', check=check_settings_import, surface='imports', budget_ms=None, needs_app=False),
    Target(name='mastodon_csv', check=check_mastodon_csv, surface='imports', budget_ms=None, needs_app=False),
]
