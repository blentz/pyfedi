"""Property checks for the files a user uploads to import (tier T6, app/user/routes.py and app/instance/util.py).

Two uploads drive outbound work on a user's behalf, and a third writes an instance's bans:

* the settings import (user settings, Import & Export): a JSON file stored in Redis and read back by
  import_settings_task, whose every entry becomes an actor lookup, a block or a bookmark;
* the Mastodon follows CSV (Add people): a CSV export whose first column is a handle to follow;
* the admin's ban list (Federation, ban lists): a JSON file whose entries become instance, domain, tag and user bans.

Properties:

* settings_import -- what the task reads out of the stored file never raises, whatever the bytes: anything that is
  not a JSON object is no entries at all, and the entries under each key are a list of at most
  IMPORT_MAX_ENTRIES, each of the shape the task uses it as -- a string, or for user_notes an object with a
  string target and a string text, read from 'note' (what the export writes) when present, else from 'body' -- in
  file order, taken from the first IMPORT_MAX_ENTRIES the file lists.
* mastodon_csv -- mastodon_csv_handles either raises csv.Error (which the view answers with a message) or returns
  at most one string per row, each passing is_fedi_handle and made of characters of the file's text as decoded
  (UTF-8, else Latin-1).
* ban_list -- ban_list_entries never raises: it is None exactly when the bytes are not UTF-8 JSON with an object at
  the top, and otherwise holds every section, each the file's own entries of the shape the import task writes into
  a column -- a non-empty name of at most 255 characters, or for banned_tags an object with such a name and a
  display_as that is one or null -- in file order. An instance or domain entry that is an IPv6 literal, bracketed or
  not, is in its compressed unbracketed spelling (the one every ban lookup compares); every other name is as listed.

No database and no network: these are the parse steps only, before any lookup.
"""

import csv
import ipaddress
import json

from app.admin.util import ban_list_entries
from app.constants import IMPORT_MAX_ENTRIES
from app.instance.util import is_fedi_handle, mastodon_csv_handles
from app.user.routes import import_entries, import_note_text, settings_import_contents
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
        if not isinstance(entry, dict) or not isinstance(entry.get('target'), str):
            return False
        text = entry['note'] if 'note' in entry else entry.get('body')
        return isinstance(text, str) and import_note_text(entry) is text
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
        listed = raw[:IMPORT_MAX_ENTRIES] if isinstance(raw, list) else []
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
    characters = set(text)
    rows = text.count('\n') + text.count('\r') + 1
    assert len(handles) <= rows, f'{len(handles)} handles out of at most {rows} rows'
    for handle in handles:
        assert isinstance(handle, str) and is_fedi_handle(handle), f'{handle!r} is not a fediverse handle'
        # A field's quoting is gone from the handle, so it need not appear in the text as one run; its characters must.
        assert set(handle) <= characters, f'{handle!r} holds characters the file does not'


BAN_LIST_NAME_SECTIONS = ('allowed_instances', 'banned_instances', 'banned_domains', 'banned_users')
BAN_LIST_HOST_SECTIONS = ('allowed_instances', 'banned_instances', 'banned_domains')


def _one_spelling(host: str) -> str:
    """An IPv6 literal's compressed spelling, without brackets; anything else unchanged."""
    literal = host[1:-1] if host[:1] == '[' and host[-1:] == ']' else host
    try:
        return ipaddress.IPv6Address(literal).compressed
    except ValueError:
        return host


def _ban_list_name(entry) -> bool:
    return isinstance(entry, str) and 1 <= len(entry) <= 255


def check_ban_list(data: bytes) -> None:
    data = data[:MAX_INPUT]
    entries = ban_list_entries(data)
    try:
        value = json.loads(data.decode('utf-8'))
    except (ValueError, RecursionError):   # UnicodeDecodeError is a ValueError
        value = None
    if not isinstance(value, dict):
        assert entries is None, f'{entries!r:.300} out of a file that is not a JSON object'
        return
    assert isinstance(entries, dict) and set(entries) == {*BAN_LIST_NAME_SECTIONS, 'banned_tags'}, f'{entries!r:.300}'
    for key in BAN_LIST_NAME_SECTIONS:
        raw = value.get(key)
        listed = raw if isinstance(raw, list) else []
        expected = [entry for entry in listed if _ban_list_name(entry)]
        if key in BAN_LIST_HOST_SECTIONS:
            expected = [_one_spelling(entry) for entry in expected]
        assert entries[key] == expected, f'{key}: {entries[key]!r:.300}'
    raw = value.get('banned_tags')
    listed = raw if isinstance(raw, list) else []
    expected = [{'name': tag['name'], 'display_as': tag.get('display_as')} for tag in listed
                if isinstance(tag, dict) and _ban_list_name(tag.get('name'))
                and (tag.get('display_as') is None or _ban_list_name(tag.get('display_as')))]
    assert entries['banned_tags'] == expected, f'banned_tags: {entries["banned_tags"]!r:.300}'


TARGETS = [
    Target(name='settings_import', check=check_settings_import, surface='imports', budget_ms=None, needs_app=False),
    Target(name='mastodon_csv', check=check_mastodon_csv, surface='imports', budget_ms=None, needs_app=False),
    Target(name='ban_list', check=check_ban_list, surface='imports', budget_ms=None, needs_app=False),
]
