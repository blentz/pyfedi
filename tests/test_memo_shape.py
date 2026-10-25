"""Memos whose value pickles a class this branch changed are keyed apart from the old code's (final fix F1).

Prod's cache is redis and outlives a deploy. An entry the old code wrote holds the old shape: a 6-field DomainInfo
fails to unpickle (a TypeError flask-caching logs on every call until the entry expires), and an old Post or Community
has no `local_only` or `site_domain_id` in its state. The new code must never read such an entry, and a rollback must
never read one the new code wrote. The test config's cache is NullCache, so each test installs a real one.
"""
import io
import logging
import pickle
import time

import pytest
from cachelib import SimpleCache

from app import cache
from app.main.util import sidebar_active_communities, sidebar_new_communities
from app.storefront import DomainInfo, domain_rows
from app.models import Community
from app.utils import community_moderators, instance_sticky_posts, joined_communities, moderating_communities
from tests.factories import make_community, make_instance, make_user


@pytest.fixture
def real_cache(app, monkeypatch):
    backend = SimpleCache()
    monkeypatch.setitem(app.extensions['cache'], cache, backend)
    return backend


def _old_key(fn, *args):
    """The key the old code memoized `fn(*args)` under: the function's own name, no `make_name`."""
    return cache._memoize_make_cache_key()(fn.uncached, *args)


def _v2_key(fn, *args):
    """The key plan C1's code memoized `fn(*args)` under: `make_name` with memo shape v2."""
    return cache._memoize_make_cache_key(make_name=lambda name: f'{name}:v2')(fn.uncached, *args)


class _ShortDomainInfo(pickle.Pickler):
    """Pickles a DomainInfo the way an older class with fewer fields did: unpickling calls DomainInfo with that many
    arguments."""

    def __init__(self, out, fields: int):
        super().__init__(out, pickle.HIGHEST_PROTOCOL)
        self.fields = fields

    def reducer_override(self, obj):
        if isinstance(obj, DomainInfo):
            return DomainInfo, tuple(obj)[:self.fields]
        return NotImplemented


def _short_pickle(value, fields: int) -> bytes:
    out = io.BytesIO()
    _ShortDomainInfo(out, fields).dump(value)
    return out.getvalue()


@pytest.mark.parametrize('key, fields', [(_old_key, 6), (_v2_key, 7)])
def test_an_older_shorter_domain_rows_entry_is_never_read(db_session, real_cache, caplog, key, fields):
    """6 fields before plan C1 (no `id`), 7 before C2 (no `instance_id`)."""
    old = (DomainInfo('example.org', 'https', True, 'anyone', True, False, 1, 1),)
    real_cache._cache[key(domain_rows)] = (time.time() + 300, _short_pickle(old, fields))
    with caplog.at_level(logging.ERROR):
        rows = domain_rows()
    assert 'Exception possibly due to cache backend' not in caplog.text
    assert rows and all(len(row) == 8 for row in rows)


@pytest.mark.parametrize('fn, args', [
    (domain_rows, ()),
    (instance_sticky_posts, ('hot',)),
    (joined_communities, (1,)),
    (moderating_communities, (1,)),
    (sidebar_active_communities, (None,)),
    (sidebar_new_communities, (None,)),
    (community_moderators, (1,)),   # R3: CommunityMember rows, each with its User loaded (lazy='joined')
])
def test_an_entry_under_the_old_name_is_ignored(db_session, real_cache, fn, args):
    real_cache.set(_old_key(fn, *args), 'OLD SHAPE', timeout=300)
    assert fn(*args) != 'OLD SHAPE'


def test_a_new_entry_is_not_under_the_old_name(db_session, real_cache):
    """A rollback to the old code reads the old name: nothing the new code writes is there."""
    instance_sticky_posts('hot')
    assert real_cache.get(_old_key(instance_sticky_posts, 'hot')) is None


def test_a_communitys_moderators_entry_under_the_old_name_is_ignored(db_session, real_cache):
    """R3: Community.moderators pickles CommunityMember rows, and each carries a full User."""
    make_user(make_instance('test.piefed.local'), 'founder', local=True)   # make_community's user_id=1
    community = make_community('memoed')
    real_cache.set(_old_key(Community.moderators, community), 'OLD SHAPE', timeout=300)
    assert community.moderators() != 'OLD SHAPE'
