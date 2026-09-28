"""`posting_warning`, the one `|safe` field with no sanitiser behind it.

`app/templates/post/post.html` renders both of them unescaped:

    {{ post.community.posting_warning|safe }}
    {{ post.community.instance.posting_warning|safe }}

on every post page in that community. `|safe` is deliberate -- a moderator may
format the warning -- and in this codebase that is always paid for at the WRITE
end: `description_html`, `about_html` and `rules_html` all come out of
`allowlist_html`. `posting_warning` had five writers and no sanitiser at any of
them:

    app/community/routes.py    the create form and the edit form
    app/admin/routes.py        the admin community form, and the instance form
    app/activitypub/util.py    the community refresh, and community creation

D1377. The last two are the ones a stranger reaches: `postingWarning` is whatever
a peer published in its Group document, and this instance publishes the same key
(`app/activitypub/routes.py:535`), so PieFed instances exchange it. Measured, from
both ActivityPub paths:

    REFRESH stored='<img src=x onerror=alert(1)><script>alert(2)</script>'
    CREATE  stored='<img src=x onerror=alert(1)><script>alert(2)</script>'

Two further shapes from the same read, which is D1372's family at a key it did not
cover -- both abort the refresh task, and that task re-raises, so the community
stops being refreshed at all:

    postingWarning={}          ProgrammingError: can't adapt type 'dict'
    700 characters             DataError: value too long for character
                               varying(512)

WHY SANITISE RATHER THAN ESCAPE AT RENDER. Dropping `|safe` would take the
formatting away from local moderators, who are the reason it is there. Sanitising
at the write keeps the affordance and removes the hazard, and puts this field on
the same footing as every other `|safe` field.

THE INSTANCE WARNING IS INCLUDED THOUGH ONLY AN ADMIN WRITES IT. A site admin can
already put HTML in `g.site.description`, which is also `|safe`, so this is not the
boundary that matters -- but one helper for one column means the four community
writers cannot drift from each other, and the instance writer costs one line.
"""
import pytest
from flask import g

from app import db
from app.activitypub.util import actor_json_to_model, refresh_community_profile_task
from app.models import Community, Site
from app.utils import sanitise_posting_warning
from tests.factories import peer_actor_json, peer_instance
from tests.test_ap_refresh_profiles import (PEER, _group_document,
                                            _remote_community, _serve)

SCRIPTS = [
    '<img src=x onerror=alert(1)>',
    '<script>alert(1)</script>',
    '<a href="javascript:alert(1)">click</a>',
    '<div onclick="alert(1)">click</div>',
    '<iframe src="https://evil.test"></iframe>',
    '<svg onload=alert(1)>',
]


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    db.session.commit()
    return site


# --------------------------------------------------------------------------
# The helper
# --------------------------------------------------------------------------


class TestSanitisePostingWarning:
    @pytest.mark.parametrize('payload', SCRIPTS)
    def test_nothing_that_executes_survives(self, app, payload):
        cleaned = sanitise_posting_warning(payload)

        assert '<script' not in cleaned
        assert 'onerror' not in cleaned
        assert 'onclick' not in cleaned
        assert 'onload' not in cleaned
        assert '<iframe' not in cleaned
        assert 'javascript:' not in cleaned

    @pytest.mark.parametrize('payload', ['<strong>Read the rules</strong>',
                                         '<em>please</em>',
                                         '<p>Be nice</p>',
                                         'Plain text is fine'])
    def test_the_formatting_a_moderator_wants_survives(self, app, payload):
        """The reason `|safe` is there. A fix that escaped everything would take
        this away, and every assertion above would still pass."""
        cleaned = sanitise_posting_warning(payload)

        assert '&lt;' not in cleaned
        for tag in ('strong', 'em', 'p'):
            if f'<{tag}>' in payload:
                assert f'<{tag}>' in cleaned

    def test_a_link_survives_with_its_href(self, app):
        cleaned = sanitise_posting_warning('<a href="https://example.com">rules</a>')

        assert 'https://example.com' in cleaned and 'rules' in cleaned

    @pytest.mark.parametrize('value', ['', None])
    def test_nothing_in_nothing_out(self, app, value):
        assert sanitise_posting_warning(value) == ''

    def test_the_result_fits_the_column(self, app):
        """`String(512)`. Sanitising can lengthen a string -- `&` becomes `&amp;`
        -- so the cut has to come after it, which is the ordering this asserts."""
        cleaned = sanitise_posting_warning('&' * 400)

        assert len(cleaned) <= 512

    def test_a_long_warning_is_cut(self, app):
        assert len(sanitise_posting_warning('w' * 900)) <= 512


# --------------------------------------------------------------------------
# The two ActivityPub writers
# --------------------------------------------------------------------------


def _refresh_with(http_mock, value):
    community = _remote_community()
    cid = community.id
    document = _group_document()
    document['postingWarning'] = value
    _serve(http_mock, f'https://{PEER}/c/memes', document)
    refresh_community_profile_task(cid, None)
    db.session.expire_all()
    return db.session.get(Community, cid)


def _created_with(value, name='memes2'):
    peer_instance(PEER)
    document = peer_actor_json('Group', name=name, server=PEER,
                               fields={'postingWarning': value})
    return actor_json_to_model(document, f'!{name}', PEER)


class TestTheWarningAPeerPublishes:
    @pytest.mark.parametrize('payload', SCRIPTS)
    def test_the_refresh_stores_nothing_that_executes(self, app, env, http_mock,
                                                      payload):
        stored = _refresh_with(http_mock, payload).posting_warning or ''

        assert '<script' not in stored and 'onerror' not in stored
        assert 'onload' not in stored and '<iframe' not in stored
        assert 'javascript:' not in stored

    @pytest.mark.parametrize('payload', SCRIPTS)
    def test_creation_stores_nothing_that_executes(self, app, env, payload):
        stored = _created_with(payload).posting_warning or ''

        assert '<script' not in stored and 'onerror' not in stored
        assert 'onload' not in stored and '<iframe' not in stored
        assert 'javascript:' not in stored

    def test_a_peers_formatting_still_arrives(self, app, env, http_mock):
        """Not "the field is blanked": a remote community's warning is worth
        showing, and a fix that dropped it would pass every row above."""
        stored = _refresh_with(http_mock,
                               '<strong>Read the rules</strong>').posting_warning

        assert '<strong>' in stored and 'Read the rules' in stored

    @pytest.mark.parametrize('value', [5, [], {}, True, ['a'], 'w' * 900])
    def test_an_awkward_value_does_not_abort_the_refresh(self, app, env,
                                                        http_mock, value):
        """`{}` was `ProgrammingError: can't adapt type 'dict'` and 900
        characters was `DataError` on a String(512). The task re-raises after
        rolling back, so either left the community unrefreshable for ever --
        D1372's family at a key it did not cover."""
        community = _refresh_with(http_mock, value)

        assert community is not None
        assert (community.posting_warning or '') == '' or \
            len(community.posting_warning) <= 512

    @pytest.mark.parametrize('value', [5, [], {}, True, 'w' * 900])
    def test_an_awkward_value_does_not_refuse_the_community(self, app, env,
                                                           value):
        community = _created_with(value)

        assert community is not None

    def test_an_absent_warning_is_none(self, app, env, http_mock):
        """The `else None` arm, which was already there: a peer that publishes no
        warning is not a peer publishing an empty one."""
        community = _remote_community()
        cid = community.id
        document = _group_document()
        assert 'postingWarning' not in document
        _serve(http_mock, f'https://{PEER}/c/memes', document)

        refresh_community_profile_task(cid, None)
        db.session.expire_all()

        assert db.session.get(Community, cid).posting_warning is None


# --------------------------------------------------------------------------
# The three local writers
# --------------------------------------------------------------------------


class TestTheLocalWriters:
    """A community moderator is not a site admin, and this value is rendered to
    every member who opens a post there."""

    def _edit_community(self, app, user, community, warning):
        from flask import session as flask_session
        from flask_wtf.csrf import generate_csrf

        client = app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(user.id)
            sess['_fresh'] = True
        with app.test_request_context():
            token = generate_csrf()
            raw = flask_session['csrf_token']
        with client.session_transaction() as sess:
            sess['csrf_token'] = raw
        return client, token, raw

    def test_the_helper_is_what_every_writer_calls(self, app, env):
        """The writers are five lines in three modules, and the round before this
        one lost three mutants to call sites its tests did not reach (fact 801).
        This asserts the property that makes that impossible here: every
        assignment to a `posting_warning` column goes through the helper.
        """
        import re
        from pathlib import Path

        offenders = []
        for path in ['app/community/routes.py', 'app/admin/routes.py',
                     'app/activitypub/util.py']:
            for n, line in enumerate(Path(path).read_text().splitlines(), 1):
                if re.search(r'posting_warning\s*=(?!=)', line) and \
                        'db.Column' not in line:
                    if not ('sanitise_posting_warning' in line or
                            'posting_warning_from_ap' in line):
                        offenders.append(f'{path}:{n}: {line.strip()}')

        assert not offenders, offenders

    @pytest.mark.parametrize('payload', SCRIPTS)
    def test_the_admin_community_form_sanitises(self, app, env, payload):
        """Called through the helper rather than the route: the route is covered
        by tests/test_admin_community_listings.py, and what this round changed is
        which function the value passes through."""
        cleaned = sanitise_posting_warning(payload)

        assert '<script' not in cleaned and 'onerror' not in cleaned

    def test_the_stored_value_is_what_the_page_would_render(self, app, env,
                                                           http_mock):
        """The end of the chain. `|safe` renders the column verbatim, so the
        column's contents ARE the DOM -- this asserts the two are the same thing
        and that nothing executable is in either."""
        from flask import render_template_string

        stored = _refresh_with(http_mock,
                               '<img src=x onerror=alert(1)>').posting_warning or ''
        rendered = render_template_string('{{ w|safe }}', w=stored)

        assert rendered == stored
        assert 'onerror' not in rendered
