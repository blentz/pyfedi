"""The three typeahead endpoints that build HTML by hand.

Each answers an htmx `hx-post`/`hx-get` whose response is swapped into the DOM as
HTML, and each built that HTML with an f-string rather than through Jinja:

    app/main/routes.py        modlog_search_suggestions
                              f"<option value='{m.ap_id or m.user_name}'>"
    app/post/routes.py        post_search_community_suggestions
                              f"<option value='{c}'>"
    app/community/routes.py   _make_community_results_datalist_html
                              f'<option value="{community_name}"></option>'

D1375, two faults.

INJECTION. A user name is not a safe HTML attribute value. Nothing restricts the
characters in a REMOTE actor's name -- `actor_name_from_ap` strips the peer's
`preferredUsername` and cuts it to the column and does not filter it -- so a peer
publishing

    "preferredUsername": "x'><img src=x onerror=alert(1)>"

had that stored verbatim in `User.user_name`, and the modlog endpoint echoed it.
Measured, the whole response body:

    <option value='x'><img src=x onerror=alert(1)>'>

The `<img>` has left the attribute. Jinja autoescapes every template in this
codebase; these three strings never went through Jinja. The community endpoints
are the same shape one actor type over, through `Community.name` and
`lemmy_link()`.

THE MISSING GATE. `modlog()` is `@login_required_if_private_instance`, and the
comment above it records why -- the modlog "was the exception: it answered 200
with its public entries where /communities and / redirect to the login". The
endpoint that page's search box calls carried no decorator at all. Measured on a
private instance:

    /modlog                     302
    /modlog/search_suggestions  200, and it named the user

Five accounts per substring, to anybody, at the endpoint of the page that was
gated for exactly this reason.

THIS IS THE THIRD TIME. D998 and D1111 are the same finding at
`post_search_community_suggestions`: "the fallback search matched on name and
`ap_id` with only `banned == False`, so it named PRIVATE communities -- to
anyone, since the route carried no decorator either". That round fixed the query
and added `@login_required`, and left the unescaped f-string one line below
untouched. A page and the endpoint it feeds are two places one control has to be
applied, and a hand-built HTML string is a fourth thing to check when you are
already looking at one.
"""
import pytest
from flask import g

from app import db
from app.models import Community, Site, User
from tests.factories import make_community, make_instance, make_user

PAYLOADS = [
    "x'><img src=x onerror=alert(1)>",
    'x" onfocus=alert(1) autofocus="',
    "x'/><script>alert(1)</script>",
    "x'>",
    'x<b>bold</b>',
]


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    db.session.commit()
    return site


def named(user_name, local=True):
    """A user whose name carries whatever a peer published.

    Set after creation rather than passed to the factory: the factory builds
    `ap_profile_id` and friends from the name, and this is about the name column
    alone.
    """
    instance = make_instance('peer.typeahead.test')
    user = make_user(instance, 'placeholder')
    user.user_name = user_name
    user.ap_id = None if local else f'{user_name}@peer.typeahead.test'
    db.session.commit()
    return user


# --------------------------------------------------------------------------
# The modlog endpoint: the gate
# --------------------------------------------------------------------------


class TestTheModlogSuggestionsGate:
    def _both(self, app, private):
        g.site.private_instance = private
        db.session.commit()
        make_user(make_instance('peer.typeahead.test'), 'secretperson')
        db.session.commit()
        client = app.test_client()
        suggestions = client.post('/modlog/search_suggestions',
                                  data={'user_name': 'secret'})
        page = client.get('/modlog')
        return suggestions, page

    def test_a_private_instance_refuses_an_anonymous_visitor(self, app, env):
        """D1375. Measured: 200, and `secretperson` in the body, while the page
        the box belongs to answered 302."""
        suggestions, page = self._both(app, private=True)

        assert page.status_code == 302
        assert suggestions.status_code == 302

    def test_the_names_do_not_reach_an_anonymous_visitor(self, app, env):
        """The status is not the assertion. What matters is that the endpoint
        does not enumerate accounts."""
        suggestions, _ = self._both(app, private=True)

        assert b'secretperson' not in suggestions.data

    def test_the_endpoint_answers_on_a_public_instance(self, app, env):
        """The other direction: `login_required_if_private_instance`, not
        `login_required`. The modlog is public on a public instance and its
        search box has to keep working there, which is what makes this the same
        decorator the page carries rather than a stricter one."""
        suggestions, page = self._both(app, private=False)

        assert page.status_code == 200
        assert suggestions.status_code == 200
        assert b'secretperson' in suggestions.data

    def test_a_signed_in_user_is_served_on_a_private_instance(self, app, env):
        """The gate is about anonymity, not about the modlog being secret."""
        g.site.private_instance = True
        alice = make_user(make_instance('peer.typeahead.test'), 'alice')
        make_user(db.session.get(User, alice.id).instance, 'secretperson')
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(alice.id)
            sess['_fresh'] = True

        response = client.post('/modlog/search_suggestions',
                               data={'user_name': 'secret'})

        assert response.status_code == 200
        assert b'secretperson' in response.data


# --------------------------------------------------------------------------
# All three endpoints: the escaping
# --------------------------------------------------------------------------


class TestTheModlogEndpointEscapesTheName:
    @pytest.mark.parametrize('payload', PAYLOADS)
    def test_the_name_is_not_echoed_raw(self, app, env, payload):
        named(payload)
        client = app.test_client()

        response = client.post('/modlog/search_suggestions',
                               data={'user_name': 'x'})

        assert payload.encode() not in response.data

    @pytest.mark.parametrize('payload', PAYLOADS)
    def test_no_tag_escapes_the_attribute(self, app, env, payload):
        """The property, rather than a list of payloads: the body is exactly the
        `<option ...>` elements this endpoint builds, so no `<` may appear
        between the opening quote and the closing one."""
        named(payload)
        client = app.test_client()

        body = client.post('/modlog/search_suggestions',
                           data={'user_name': 'x'}).data.decode()

        for element in [e for e in body.split('<option ') if e]:
            value = element[element.index("'") + 1:element.rindex("'")]
            assert '<' not in value and '>' not in value, value

    def test_an_ordinary_name_is_still_offered(self, app, env):
        """Escaping must not break the feature: the value is what the box fills
        in, so a plain name has to survive it unchanged."""
        named('ordinaryperson')
        client = app.test_client()

        body = client.post('/modlog/search_suggestions',
                           data={'user_name': 'ordinary'}).data

        assert b"<option value='ordinaryperson'>" in body

    def test_a_remote_name_is_offered_by_its_ap_id(self, app, env):
        """`m.ap_id or m.user_name` -- a remote user is offered as
        `name@domain`, and that is the half an attacker controls entirely."""
        named("evil'><img src=x>", local=False)
        client = app.test_client()

        body = client.post('/modlog/search_suggestions',
                           data={'user_name': 'evil'}).data

        assert b'<img' not in body
        assert b'&lt;img' in body

    def test_a_remote_user_is_offered_as_name_at_domain(self, app, env):
        """`m.ap_id or m.user_name`, with the two differing. The box fills a
        moderator's form field, and `alice` and `alice@peer.example` are two
        different accounts -- offering the bare name for a remote user would put
        the wrong account in the field. Asserted with a payload-free name so it
        is about WHICH value is offered, not about escaping."""
        instance = make_instance('peer.typeahead.test')
        remote = make_user(instance, 'placeholder')
        remote.user_name = 'aliceremote'
        remote.ap_id = 'aliceremote@peer.typeahead.test'
        db.session.commit()
        client = app.test_client()

        body = client.post('/modlog/search_suggestions',
                           data={'user_name': 'aliceremote'}).data

        assert b"<option value='aliceremote@peer.typeahead.test'>" in body
        assert b"<option value='aliceremote'>" not in body

    def test_a_local_user_is_offered_by_bare_name(self, app, env):
        """The other arm of the same `or`: a local user has no `ap_id`."""
        named('alicelocal')
        client = app.test_client()

        body = client.post('/modlog/search_suggestions',
                           data={'user_name': 'alicelocal'}).data

        assert b"<option value='alicelocal'>" in body

    def test_the_suspect_field_is_escaped_too(self, app, env):
        """Two form fields reach the same query: `suspect_user_name` first, then
        `user_name`. Both build the same response, and a fix applied to one code
        path would be a fix applied to both -- asserted so that stays true if
        they ever diverge."""
        named("x'><img src=x onerror=alert(1)>")
        client = app.test_client()

        response = client.post('/modlog/search_suggestions',
                               data={'suspect_user_name': 'x'})

        assert b'<img' not in response.data


class TestTheCommunitySuggestionEndpoints:
    def _post_suggestions(self, app, user, term):
        """A CSRF token is required: without one the route answers 400, and every
        assertion below about what is NOT in the body would pass on an empty
        one (fact 749)."""
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
        response = client.post('/post/search_community_suggestions',
                               data={'which_community': term,
                                     'csrf_token': token})
        assert response.status_code == 200, response.data
        return response

    @pytest.mark.parametrize('payload', ["evil'><img src=x onerror=alert(1)>",
                                         'evil" onfocus=alert(1) autofocus="',
                                         "evil'/><script>alert(1)</script>"])
    def test_the_post_form_endpoint_escapes_a_community_name(self, app, env,
                                                            payload):
        """`c` is `community.lemmy_link()`, built from `name` and `ap_domain`.
        A remote community's name is the peer's `preferredUsername`, unfiltered.
        This endpoint is `@login_required` (D1111), so the victim is a signed-in
        user rather than any visitor -- still a name a peer chose, rendered into
        their DOM."""
        alice = make_user(make_instance('peer.typeahead.test'), 'alice')
        community = make_community(name=payload)
        community.ap_id = f'{payload}@peer.typeahead.test'
        db.session.commit()

        response = self._post_suggestions(app, alice, 'evil')

        # The community MUST be in the answer, escaped. Without this the three
        # negative assertions below would all pass on an empty body -- which is
        # exactly how the first version of this test passed while the endpoint
        # was answering 400 (fact 749).
        assert b'evil' in response.data, response.data
        assert b'<img' not in response.data
        assert b'<script' not in response.data
        assert payload.encode() not in response.data

    def test_an_ordinary_community_is_still_offered(self, app, env):
        alice = make_user(make_instance('peer.typeahead.test'), 'alice')
        make_community(name='ordinarycommunity')
        db.session.commit()

        response = self._post_suggestions(app, alice, 'ordinarycommunity')

        assert b'ordinarycommunity' in response.data

    @pytest.mark.parametrize('payload', ["evil'><img src=x onerror=alert(1)>",
                                         'evil" onfocus=alert(1) autofocus="',
                                         'evil<b>x</b>'])
    def test_the_add_remote_datalist_escapes_a_name(self, app, env, payload):
        """`_make_community_results_datalist_html` takes its names from
        `app/static/tmp/all_communities.json`, which is fetched rather than
        written here. Called directly: the route around it reads that file, and
        the question is what the function does with a name, not whether the file
        exists."""
        from app.community.routes import _make_community_results_datalist_html

        html = _make_community_results_datalist_html(payload)

        assert '<img' not in html
        assert '<b>' not in html
        assert '<script' not in html
        assert html.startswith('<option value="') and html.endswith('"></option>')

    def test_the_add_remote_datalist_keeps_an_ordinary_name(self, app, env):
        from app.community.routes import _make_community_results_datalist_html

        assert _make_community_results_datalist_html('memes@peer.example') == \
            '<option value="memes@peer.example"></option>'
