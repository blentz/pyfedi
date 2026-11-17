"""A link (`<a href>`) sends a GET, so one to a route that answers only POST is a 405 for whoever clicks it.

D1018 made `community.community_add_moderator` POST-only (a GET that changed state was a CSRF hole) but left the
find-moderator page's "Add" button a link, so adding a moderator answered 405. This scans every template's `<a>`
tags for an `href` built with `url_for('<endpoint>' ...)` naming an endpoint whose rules all refuse GET. A link that
posts through htmx (`hx-post`) is not a GET when clicked, so it passes.
"""
import os
import re

A_TAG = re.compile(r'<a\b[^>]*>', re.DOTALL)
HREF_URL_FOR = re.compile(r"""href\s*=\s*["']\{\{\s*url_for\(\s*['"]([\w.]+)['"]""")
TEMPLATES = os.path.join(os.path.dirname(__file__), '..', 'app', 'templates')


def post_only_endpoints(app):
    methods = {}
    for rule in app.url_map.iter_rules():
        methods.setdefault(rule.endpoint, set()).update(rule.methods or ())
    return {endpoint for endpoint, allowed in methods.items() if 'GET' not in allowed}


def links_to(post_only):
    for folder, _dirs, files in os.walk(TEMPLATES):
        for name in sorted(files):
            if not name.endswith('.html'):
                continue
            path = os.path.join(folder, name)
            with open(path, encoding='utf-8') as template:
                text = template.read()
            for tag in A_TAG.finditer(text):
                if 'hx-post' in tag.group(0):
                    continue
                for endpoint in HREF_URL_FOR.findall(tag.group(0)):
                    if endpoint in post_only:
                        line = text.count('\n', 0, tag.start()) + 1
                        yield f'{os.path.relpath(path, TEMPLATES)}:{line} {endpoint}'


def test_no_template_links_to_a_post_only_route(app):
    found = list(links_to(post_only_endpoints(app)))

    assert found == [], 'a link sends GET; make these POST forms with a csrf_token: ' + ', '.join(found)


def test_the_scan_finds_the_add_moderator_link_it_was_written_for(app, tmp_path, monkeypatch):
    """The scan itself: the find-moderator page's old "Add" link is caught; an htmx-posting one is not."""
    import tests.test_no_links_to_post_only_routes as scan   # lazy: the module patches its own TEMPLATES
    (tmp_path / 'page.html').write_text(
        """<a class="btn" href="{{ url_for('community.community_add_moderator', community_id=1, user_id=2) }}">Add</a>
<a href="{{ url_for('community.remove_icon', community_id=1) }}"
   hx-post="{{ url_for('community.remove_icon', community_id=1) }}">Delete</a>""")
    monkeypatch.setattr(scan, 'TEMPLATES', str(tmp_path))

    assert list(scan.links_to(post_only_endpoints(app))) == ['page.html:1 community.community_add_moderator']
