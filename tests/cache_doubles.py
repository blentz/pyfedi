"""Narrow a patched `cache.get` / `cache.set` to the keys a test is about.

flask-caching 2.5 routes `@cache.memoize` through `Cache.get` and `Cache.set`, so a test that
patches either one also sees every memoized lookup the request makes (the site dict,
`instance_banned`, settings). These helpers keep such a test about its own key.
"""
from app import cache


def get_for(prefix, value):
    """A side_effect for a patched `cache.get`: `value` for keys starting with `prefix`, the real
    cache for every other key. Build it before the patch starts, so `real` is the unpatched get."""
    real = cache.get

    def get(key, *args, **kwargs):
        return value if str(key).startswith(prefix) else real(key, *args, **kwargs)
    return get


def calls_for(mock, prefix):
    """The calls on a patched `cache.set` whose key starts with `prefix`."""
    return [call for call in mock.call_args_list
            if str(call.args[0] if call.args else call.kwargs.get('key')).startswith(prefix)]
