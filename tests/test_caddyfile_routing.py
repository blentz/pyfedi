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

# The Caddyfile names its host and ACME email as {$PIEFED_DOMAIN} and {$ACME_EMAIL}; Caddy fills them from the
# environment when it loads the file, and these tests do the same with test values.
ENV = {'PIEFED_DOMAIN': 'piefed.example', 'ACME_EMAIL': 'admin@piefed.example'}


def caddyfile_lines():
    """The Caddyfile's lines with its {$NAME} placeholders replaced by the test values."""
    text = CADDYFILE.read_text()
    for name, value in ENV.items():
        text = text.replace('{$' + name + '}', value)
    return text.splitlines()


def handled_paths():
    """The path matchers of every `handle <path> {` block, comments ignored."""
    paths = []
    for line in caddyfile_lines():
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


def web_proxy_block():
    """The directives directly inside the `reverse_proxy web:5000 { ... }` block, comments ignored. Nested
    blocks (the `handle_response` that serves cached onion media) are skipped."""
    lines = [line.split('#', 1)[0].strip() for line in caddyfile_lines()]
    start = lines.index('reverse_proxy web:5000 {')
    top_level, depth = [], 1
    for line in lines[start + 1:]:
        depth += line.endswith('{') - (line == '}')
        if depth == 0:
            return top_level
        if depth == 1 and line != '}':
            top_level.append(line)


def caddy_seconds(value):
    match = re.fullmatch(r'(\d+)(ms|s)', value)
    assert match, f'unsupported Caddy duration {value}'
    return int(match.group(1)) / (1000 if match.group(2) == 'ms' else 1)


def test_requests_wait_for_the_app_while_it_restarts():
    """While gunicorn restarts (migrations, preload), dials to web:5000 are refused and Caddy answered
    every inbox delivery with 502; a sender that does not retry lost the activity. Caddy retries a
    refused dial for any method, POST included, because nothing was sent upstream yet."""
    options = dict(line.split(None, 1) for line in web_proxy_block() if line)

    # A restart took about 10 s (502s from 14:33:04, gunicorn listening at 14:33:12).
    assert caddy_seconds(options['lb_try_duration']) >= 30
    assert caddy_seconds(options['lb_try_interval']) <= 1


def accel_block():
    """The directives of the `handle_response @accel { ... }` block that serves cached onion media, comments ignored."""
    lines = [line.split('#', 1)[0].strip() for line in caddyfile_lines()]
    start = lines.index('handle_response @accel {')
    return lines[start + 1:lines.index('}', start)]


def accel_headers():
    """{name: value} for each `header` directive in the accel block, any matcher token dropped."""
    headers = {}
    for line in accel_block():
        if line.startswith('header '):
            words = line.split(None, 1)[1]
            if words.startswith('@'):
                words = words.split(None, 1)[1]
            name, value = words.split(None, 1)
            headers[name] = value
    return headers


def test_cached_onion_media_stays_in_the_browser_cache():
    """A signed URL names fixed content, so a visitor's browser keeps the file for a week without asking again.
    `handle_response` copies no upstream header, so Caddy must set it."""
    assert accel_headers()['Cache-Control'] == '"public, max-age=604800, immutable"'


def test_cached_onion_media_is_served_as_inert_media():
    """The cached file is remote content on the onion origin: the browser must not sniff it into HTML or run it as a
    document. `handle_response` copies none of the route's headers, so the accel block must set each one itself."""
    assert accel_headers() == {'X-Content-Type-Options': 'nosniff',
                               'Content-Security-Policy': '"default-src \'none\'; sandbox"',
                               'Cache-Control': '"public, max-age=604800, immutable"'}
    assert accel_block()[-1] == 'file_server'           # every header is set before the file goes out


def test_a_cached_file_evicted_before_caddy_serves_it_is_not_kept_by_the_browser():
    """Eviction can delete the file between the route's hit and file_server. That 404 must not carry the week-long
    immutable Cache-Control, or the browser keeps a broken image for a week. `header` runs before `rewrite`, so the
    `file` matcher names the accel path itself rather than {path}."""
    block = accel_block()
    assert '@present file {rp.header.X-Accel-Redirect}' in block
    assert 'header @present Cache-Control "public, max-age=604800, immutable"' in block


def site_block_directives(address, lines):
    """The directives directly inside `<address> {`, comments ignored."""
    start = lines.index(f'{address} {{')
    body = []
    for line in lines[start + 1:]:
        if line == '}':
            return body
        body.append(line)


def test_the_federating_domain_template_routes_like_the_main_block_once_uncommented():
    raw = caddyfile_lines()
    live = [line.split('#', 1)[0].strip() for line in raw]
    main = site_block_directives(ENV['PIEFED_DOMAIN'], live)

    template = []
    in_template = False
    for line in raw:
        if line.startswith('# shop.example {'):
            in_template = True
        if in_template:
            template.append(line[2:].split('#', 1)[0].strip())
            if line == '# }':
                break
    assert template, 'expected a commented shop.example block'
    uncommented = site_block_directives('shop.example', template)

    assert 'import piefed_routes' in uncommented and 'import piefed_routes' in main
    assert not any(d.startswith('header Onion-Location') for d in uncommented)
    assert any(d.startswith('header Onion-Location') for d in main)
    assert 'request_header -X-PieFed-Via' in uncommented
    assert [d for d in main if not d.startswith('header Onion-Location') and d] == [d for d in uncommented if d]
