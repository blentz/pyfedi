"""The Caddyfile sends only the paths FastAPI serves to the notifications service.

`handle /notifications/*` sent every path under /notifications to
fastapi_server.py, which serves only /notifications/stream. Flask's
POST /notifications/all_read ("Mark all as read") therefore reached FastAPI and
came back as `{"detail":"Not Found"}`. Each `handle` path in the Caddyfile must
name a route FastAPI really serves, and must not cover any Flask route.
"""
import pathlib
import re

import fastapi_server

CADDYFILE = pathlib.Path(__file__).resolve().parent.parent / 'Caddyfile'


def handled_paths():
    """The path matchers of every `handle <path> {` block, comments ignored."""
    paths = []
    for line in CADDYFILE.read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        match = re.match(r'^handle\s+(/\S*)\s*\{$', line)
        if match:
            paths.append(match.group(1))
    return paths


def caddy_matches(matcher, path):
    """Caddy's path matcher: exact, unless it ends in `*`, which is a prefix match."""
    if matcher.endswith('*'):
        return path.startswith(matcher[:-1])
    return path == matcher


def test_the_caddyfile_hands_fastapi_only_paths_it_serves():
    fastapi_paths = {route.path for route in fastapi_server.app.routes}

    assert handled_paths(), 'expected at least the notifications stream block'
    for matcher in handled_paths():
        assert matcher in fastapi_paths, f'{matcher} is proxied to FastAPI, which does not serve it'


def test_no_flask_route_is_shadowed_by_a_caddy_handle_block(app):
    for rule in app.url_map.iter_rules():
        # A rule's static prefix is enough: a path under a Caddy prefix matcher is
        # shadowed whatever its variable parts are.
        static_path = rule.rule.split('<', 1)[0]
        for matcher in handled_paths():
            assert not caddy_matches(matcher, static_path), \
                f'Caddy sends {rule.rule} ({rule.endpoint}) to FastAPI via `handle {matcher}`'


def test_mark_all_as_read_is_a_flask_route_the_old_matcher_would_have_shadowed(app):
    """Pins the regression: the old wildcard covered this path, the new matcher does not."""
    assert any(rule.rule == '/notifications/all_read' for rule in app.url_map.iter_rules())
    assert caddy_matches('/notifications/*', '/notifications/all_read')
    assert not any(caddy_matches(m, '/notifications/all_read') for m in handled_paths())


def test_the_live_stream_is_proxied_to_fastapi():
    assert '/live/stream' in handled_paths()
