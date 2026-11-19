"""Fixtures from tests/test_onion_unlinkable_ip.py that other test modules request (registered in tests/conftest.py)."""
import pytest
from flask import g

from app import db
from app.models import Language
from tests.factories import make_post as make_post_row
from tests.factories import make_site
from tests.test_onion_unlinkable_ip import WORLD


@pytest.fixture
def onion_unlinkable_ip_world(two_domains):
    """A community and a post on the first domain, a site row, and `g.site`."""
    first, onion = two_domains.domains
    WORLD['community'] = two_domains.local_community('talk', first)
    quiet = two_domains.local_community('quiet', first)
    quiet.private = True   # stops a reply's federation tasks at their first guard
    g.site = make_site()
    author = two_domains.local_user('author', first)
    WORLD['post'] = make_post_row(quiet, author, 'https://elsewhere.example/p/1')
    db.session.add(Language(code='en', name='English'))
    db.session.commit()
    return two_domains
