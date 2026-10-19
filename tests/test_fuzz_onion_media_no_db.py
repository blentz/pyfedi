"""The onion media campaign checks never read the database.

onion_media_rewrite and onion_media_style reach the domain registry (app.storefront.domain_rows, a SiteDomain read)
through rewrite_links and _is_remote; the checks stub it, so a campaign on a fresh, unmigrated database still runs."""
import pytest

from app import db, storefront
from tests.fuzz.targets.onion_media import (check_onion_media_fetchable, check_onion_media_rewrite,
                                            check_onion_media_signature, check_onion_media_style)


@pytest.fixture
def no_database(app, monkeypatch):
    storefront.clear_domain_cache()

    def refuse(*args, **kwargs):
        raise AssertionError('the check read the database')

    monkeypatch.setattr(db.session, 'query', refuse)
    yield
    storefront.clear_domain_cache()


def test_rewrite_never_reads_the_database(app, no_database):
    check_onion_media_rewrite(b'<img src="https://lemmy.world/a.webp"><a href="https://x.test/b.png">x</a>'
                              b'<div style="background:url(https://y.test/c.png)"></div>')


def test_style_never_reads_the_database(app, no_database):
    check_onion_media_style(b'background:url(https://x.test/a.png)')


def test_signature_and_fetchable_never_read_the_database(app, no_database):
    check_onion_media_signature(b'\x00\x01a\x00\x01b')
    check_onion_media_fetchable(b'https://x.test/a.png')


def test_the_registry_and_s3_setting_are_restored_afterwards(app):
    before, s3 = storefront.domain_rows, dict(app.config).get('S3_PUBLIC_URL')
    check_onion_media_rewrite(b'<img src="https://x.test/a.png">')
    assert storefront.domain_rows is before
    assert dict(app.config).get('S3_PUBLIC_URL') == s3
