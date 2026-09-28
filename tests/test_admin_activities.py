"""`admin_activities` and `activity_json`: the federation log an admin reads.

30 statements between them with no test at all. Both are
`permission_required('change instance settings')`, and everything they display
came from a peer.

D1383. `activity_json` pretty-prints the stored body and renders the result
`{{ json_html | safe }}`. Two branches:

    valid JSON     "```json\\n" + pretty_json + "\\n```"   -> markdown_to_html
    anything else  "`" + pretty_json + "`"                -> markdown_to_html

The first is safe by construction and this file pins why: `json.dumps` escapes
every control character, so no line of its output can be a ``` fence -- a backtick
run inside a string value stays on that string's line, behind the `"` and the
`indent=2` prefix. That is what lets the branch keep its syntax highlighting.

The second was reached precisely when the body did NOT parse, so `pretty_json` was
the peer's bytes verbatim, wrapped in a SINGLE backtick -- the weakest delimiter
markdown has. Measured:

    input   `{"a": "` <img src=x> **bold** `"}`
    output  <code>{"a": "</code> <img loading="lazy" src="x"/>
            <strong>bold</strong> <code>"}</code>

`markdown_to_html` ends in `allowlist_html`, so this is markup injection rather
than script: an attacker-chosen image, fetched from their server the moment an
admin opens the page, and attacker-chosen links -- on the page an admin uses to
inspect suspicious federation, with the body they most want to read garbled. It is
escaped into a `<pre><code>` directly now, which is the arm
`app/post/routes.py`'s source view already uses for a body it cannot safely fence.
"""
import json
from datetime import timedelta

import pytest
from flask import g

from app import db
from app.models import ActivityPubLog, Site, utcnow
from tests.factories import make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def admin_client(app, db_session):
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = make_instance('test.piefed.local')
    admin = make_user(local, 'theadmin', local=True)
    assert admin.id == 1  # fact 347: id 1 is an admin
    db.session.commit()
    g.admin_ids = [admin.id]
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(admin.id)
        sess['_fresh'] = True
    return client


def json_block(response) -> bytes:
    """Just the rendered JSON, not the whole page.

    base.html contributes an `<h1>` and several `<img>` tags of its own, so
    asserting that a tag is absent from `response.data` is meaningless -- it fails
    against a perfectly safe page. The fallback branch emits `<pre><code>` and the
    valid-JSON branch a `<div class="codehilite">`, so the block is found by
    whichever marker is present and everything outside it is ignored.
    """
    data = response.data
    if b'<pre><code>' in data:
        start = data.index(b'<pre><code>')
        return data[start:data.index(b'</code></pre>', start)]
    marker = b'<div class="codehilite">'
    assert marker in data, data[:400]
    start = data.index(marker)
    return data[start:data.index(b'</pre>', start)]


def logged(body, result='success', direction='in', age_days=0):
    row = ActivityPubLog(direction=direction, activity_type='Create',
                         result=result, activity_id='https://peer.example/a/1',
                         exception_message='', activity_json=body)
    row.created_at = utcnow() - timedelta(days=age_days)
    db.session.add(row)
    db.session.commit()
    return row


# --------------------------------------------------------------------------
# D1383: the fallback branch
# --------------------------------------------------------------------------


class TestABodyThatIsNotJson:
    # Every entry must FAIL to parse, or it takes the valid-JSON branch and
    # proves nothing about the fallback. `{"a": "` <img src=x> `"}` was in this
    # list first and is perfectly good JSON -- its value is the backtick-wrapped
    # text -- so the premise is asserted below rather than assumed.
    BREAKOUTS = [
        '{"a": ` <img src=x> `}',
        '` ![x](https://evil.test/track.png) `',
        'not json at all ` [click](https://evil.test) `',
        '`\n\n# PWNED\n\n`',
        '``` <img src=x> ```',
        "`<script>alert(1)</script>`",
    ]

    @pytest.mark.parametrize('body', BREAKOUTS)
    def test_the_payload_really_does_not_parse(self, body):
        with pytest.raises(Exception):
            json.loads(body)

    @pytest.mark.parametrize('body', BREAKOUTS)
    def test_no_markup_of_the_peers_reaches_the_page(self, app, admin_client,
                                                     body):
        row = logged(body)

        response = admin_client.get(f'/admin/activity_json/{row.id}')

        assert response.status_code == 200
        block = json_block(response).lower()
        assert b'<img' not in block
        assert b'<strong>' not in block
        assert b'<h1' not in block
        assert b'<a ' not in block
        assert b'<script' not in block

    @pytest.mark.parametrize('body', BREAKOUTS)
    def test_the_body_is_still_shown(self, app, admin_client, body):
        """The page's whole purpose. A fix that dropped the body would satisfy
        every assertion above -- and an admin inspecting a malformed activity
        needs to see exactly what arrived."""
        row = logged(body)

        response = admin_client.get(f'/admin/activity_json/{row.id}')

        assert b'<pre><code>' in response.data
        # Something of the payload survives in the block, escaped -- otherwise
        # every assertion in the sibling test would pass on an empty block.
        assert json_block(response).strip() != b'<pre><code>'

    def test_the_angle_brackets_are_escaped_not_removed(self, app, admin_client):
        row = logged('{"a": "` <img src=x> `"}')

        response = admin_client.get(f'/admin/activity_json/{row.id}')

        assert b'&lt;img src=x&gt;' in response.data

    def test_a_plain_unparseable_body_is_shown_verbatim(self, app, admin_client):
        row = logged('this is not json')

        response = admin_client.get(f'/admin/activity_json/{row.id}')

        assert b'this is not json' in response.data

    def test_an_empty_body_does_not_crash(self, app, admin_client):
        row = logged('')

        assert admin_client.get(
            f'/admin/activity_json/{row.id}').status_code == 200

    def test_a_null_body_does_not_crash(self, app, admin_client):
        """`activity_json` is nullable -- activities.html has an
        `is none` arm for it -- and `json.loads(None)` raises, which is what the
        `except Exception` is for."""
        row = logged(None)

        assert admin_client.get(
            f'/admin/activity_json/{row.id}').status_code == 200


# --------------------------------------------------------------------------
# The valid-JSON branch, and why it is safe
# --------------------------------------------------------------------------


class TestABodyThatIsJson:
    def test_it_is_pretty_printed(self, app, admin_client):
        row = logged(json.dumps({'type': 'Create', 'id': 'https://peer/1'}))

        response = admin_client.get(f'/admin/activity_json/{row.id}')

        assert b'&quot;type&quot;' in response.data or b'"type"' in response.data

    def test_a_backtick_run_inside_a_string_cannot_open_a_fence(self, app,
                                                               admin_client):
        """Why this branch may keep markdown. `json.dumps` escapes the newline,
        so the injected markdown stays inside one JSON string on one line, behind
        that line's `"` and indent -- and a ``` fence has to start its line.
        Asserted rather than assumed, because the fallback branch shows what
        happens when the delimiter really can be closed.
        """
        row = logged(json.dumps({'content': '```\n\n# PWNED\n\n![x](https://evil.test/t.png)'}))

        response = admin_client.get(f'/admin/activity_json/{row.id}')

        block = json_block(response).lower()
        assert b'<h1' not in block
        assert b'<img' not in block

    def test_a_backtick_run_as_a_key_cannot_open_a_fence(self, app, admin_client):
        row = logged(json.dumps({'```': 'x', 'type': 'Create'}))

        response = admin_client.get(f'/admin/activity_json/{row.id}')

        assert response.status_code == 200
        assert b'<h1' not in json_block(response).lower()

    def test_an_unknown_activity_is_a_404(self, app, admin_client):
        """`db.session.get(...) or abort(404)`."""
        assert admin_client.get('/admin/activity_json/999999').status_code == 404


# --------------------------------------------------------------------------
# admin_activities: the listing
# --------------------------------------------------------------------------


class TestTheActivitiesListing:
    def test_it_lists_a_logged_activity(self, app, admin_client):
        logged(json.dumps({'type': 'Create'}))

        response = admin_client.get('/admin/activities')

        assert response.status_code == 200
        assert b'https://peer.example/a/1' in response.data

    def test_rows_older_than_three_days_are_deleted(self, app, admin_client):
        """The listing prunes as a side effect of being viewed -- the only thing
        that deletes these rows, so it is asserted on the table rather than the
        page."""
        old = logged(json.dumps({'type': 'Old'}), age_days=4)
        recent = logged(json.dumps({'type': 'New'}), age_days=1)
        old_id, recent_id = old.id, recent.id

        admin_client.get('/admin/activities')

        db.session.expire_all()
        assert db.session.get(ActivityPubLog, old_id) is None
        assert db.session.get(ActivityPubLog, recent_id) is not None

    @pytest.mark.parametrize('result', ['success', 'failure', 'ignored'])
    def test_the_result_filter(self, app, admin_client, result):
        logged(json.dumps({'type': 'A'}), result='success')
        logged(json.dumps({'type': 'B'}), result='failure')

        response = admin_client.get('/admin/activities',
                                    query_string={'result': result})

        assert response.status_code == 200

    def test_the_result_filter_excludes_other_results(self, app, admin_client):
        logged(json.dumps({'type': 'A'}), result='failure')

        shown = admin_client.get('/admin/activities',
                                 query_string={'result': 'failure'}).data
        hidden = admin_client.get('/admin/activities',
                                  query_string={'result': 'success'}).data

        assert b'https://peer.example/a/1' in shown
        assert b'https://peer.example/a/1' not in hidden

    def test_the_direction_filter(self, app, admin_client):
        logged(json.dumps({'type': 'A'}), direction='in')

        shown = admin_client.get('/admin/activities',
                                 query_string={'direction': 'in'}).data
        hidden = admin_client.get('/admin/activities',
                                  query_string={'direction': 'out'}).data

        assert b'https://peer.example/a/1' in shown
        assert b'https://peer.example/a/1' not in hidden

    def test_no_filter_shows_everything(self, app, admin_client):
        """Both filters are `if result_filter:` / `if direction_filter:`, so an
        absent parameter must not narrow anything."""
        logged(json.dumps({'type': 'A'}), result='failure', direction='out')

        response = admin_client.get('/admin/activities')

        assert b'https://peer.example/a/1' in response.data

    def test_the_off_switch_is_announced(self, app, admin_client, monkeypatch):
        """`LOG_ACTIVITYPUB_TO_DB` off means the page can only ever be empty, so
        it says so rather than looking like federation has stopped."""
        monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', False)

        response = admin_client.get('/admin/activities')

        assert b'LOG_ACTIVITYPUB_TO_DB' in response.data


class TestWhoMaySeeThem:
    @pytest.mark.parametrize('path', ['/admin/activities',
                                      '/admin/activity_json/1'])
    def test_an_anonymous_visitor_may_not(self, app, admin_client, path):
        assert app.test_client().get(path).status_code in (302, 401, 403)

    @pytest.mark.parametrize('path', ['/admin/activities',
                                      '/admin/activity_json/1'])
    def test_an_ordinary_user_may_not(self, app, admin_client, path):
        """`permission_required('change instance settings')`."""
        local = make_instance('other.piefed.local')
        ordinary = make_user(local, 'ordinaryperson', local=True)
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(ordinary.id)
            sess['_fresh'] = True

        assert client.get(path).status_code in (302, 401, 403)
