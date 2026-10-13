"""Every translation catalog passes Babel's own checks -- the ones `pybabel compile` runs at image build.

A translation whose placeholders do not match its msgid is not only noise in the build log: a non-fuzzy one is
compiled into the .mo, and `_('... %(author_name)s ...', author_name=x)` then formats a string that asks for
`%(user_name)s` -- a KeyError on the page that shows it. Fuzzy entries are left out of the .mo, but Weblate's stale
suggestions with another string's placeholders still fail the check, so they are held to it too.
"""
from pathlib import Path

import pytest
from babel.messages.pofile import read_po

CATALOGS = sorted((Path(__file__).resolve().parent.parent / 'app' / 'translations').glob('*/LC_MESSAGES/messages.po'))


@pytest.mark.parametrize('po', CATALOGS, ids=lambda p: p.parent.parent.name)
def test_catalog_passes_babels_checks(po):
    with po.open('rb') as f:
        catalog = read_po(f, locale=po.parent.parent.name)
    errors = [f'{message.lineno}: {message.id!r}: {err}' for message, errs in catalog.check() for err in errs]
    assert not errors, '\n'.join(errors)
