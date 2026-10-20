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
from app.utils import instance_sticky_posts, joined_communities, moderating_communities


@pytest.fixture
def real_cache(app, monkeypatch):
    backend = SimpleCache()
    monkeypatch.setitem(app.extensions['cache'], cache, backend)
    return backend


def _old_key(fn, *args):
    """The key the old code memoized `fn(*args)` under: the function's own name, no `make_name`."""
    return cache._memoize_make_cache_key()(fn.uncached, *args)


class _SixFieldDomainInfo(pickle.Pickler):
    """Pickles a DomainInfo the way the old 6-field class did: unpickling calls DomainInfo with six arguments."""

    def reducer_override(self, obj):
        if isinstance(obj, DomainInfo):
            return DomainInfo, tuple(obj)[:6]
        return NotImplemented


def _six_field_pickle(value) -> bytes:
    out = io.BytesIO()
    _SixFieldDomainInfo(out, pickle.HIGHEST_PROTOCOL).dump(value)
    return out.getvalue()


def test_an_old_six_field_domain_rows_entry_is_never_read(db_session, real_cache, caplog):
    old = (DomainInfo('hell.cloud', 'https', True, 'anyone', True, False, 1),)
    real_cache._cache[_old_key(domain_rows)] = (time.time() + 300, _six_field_pickle(old))
    with caplog.at_level(logging.ERROR):
        rows = domain_rows()
    assert 'Exception possibly due to cache backend' not in caplog.text
    assert rows and all(len(row) == 7 for row in rows)


@pytest.mark.parametrize('fn, args', [
    (domain_rows, ()),
    (instance_sticky_posts, ('hot',)),
    (joined_communities, (1,)),
    (moderating_communities, (1,)),
    (sidebar_active_communities, (None,)),
    (sidebar_new_communities, (None,)),
])
def test_an_entry_under_the_old_name_is_ignored(db_session, real_cache, fn, args):
    real_cache.set(_old_key(fn, *args), 'OLD SHAPE', timeout=300)
    assert fn(*args) != 'OLD SHAPE'


def test_a_new_entry_is_not_under_the_old_name(db_session, real_cache):
    """A rollback to the old code reads the old name: nothing the new code writes is there."""
    instance_sticky_posts('hot')
    assert real_cache.get(_old_key(instance_sticky_posts, 'hot')) is None
