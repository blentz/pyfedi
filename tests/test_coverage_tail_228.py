"""Round 228: eleven lines `tests/test_api_routes_gate_sweep.py` could not reach.

That sweep sends every alpha API route a minimal VALID payload with the API off, so the
`if not enable_api()` gate is what answers rather than schema validation. Its own docstring
says what it cannot speak for: a route it cannot get past validation on is a route whose
gate line stays uncovered. Five such lines remained, for two distinct reasons, and six more
lines are a branch no request in the suite had ever asked for.

    1597, 1613, 1629         the three upload routes: `location="files"`, so a JSON body
                             never reaches them and the sweep sends JSON
    1082, 1267               schemas with NO required fields but a `@validates_schema`
                             demanding one OF two, so the sweep's "required fields only"
                             payload is empty and refused before the gate
    540-541, 557-558, 844-845  the `?debug=` branch of the three list endpoints, which
                             re-loads the response through its own schema
    176, 1602, 1618, 1634, 1648   success-path returns: nothing had ever got all the way
                             through resolve_object, the three uploads or image/delete

Two of the eighteen are left red on purpose. `:1574` and `:1585` are the success returns of
`/user/register` and `/user/get_captcha`, and both helpers still `raise Exception('not
implemented')` (D1181), so no request can reach either line. `:1574` is where D1417 was
found while reading them.

THE DEBUG BRANCH IS WORTH MORE THAN ITS SIX LINES. `list_posts_response.load(resp)` runs
the endpoint's own declared schema over the response it is about to send, so a row that
asks for it is asserting that the thing the API returns actually satisfies the contract the
API publishes. Two of the three use a module-level schema instance and one constructs a
fresh `ListPostsResponse()`; the rows below cover both spellings.
"""
import io
from types import SimpleNamespace

import pytest
from flask import current_app, g

from app import db
from app.models import Site
from tests.factories import make_community, make_community_member, make_post, make_user

GATE = 'alpha api is not enabled'


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    return SimpleNamespace(client=app.test_client(), app=app, baseline=api_baseline)


def _png():
    return io.BytesIO(
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06'
        b'\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00'
        b'\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')


# --------------------------------------------------------------------------
# The four upload routes: multipart, so the sweep's JSON never reached them
# --------------------------------------------------------------------------


UPLOAD_PATHS = ['/api/alpha/upload/image', '/api/alpha/upload/community_image',
                '/api/alpha/upload/user_image']


class TestTheUploadRoutesGate:
    """`ImageUploadRequest` declares `file` with `location="files"`, so flask-smorest
    parses these from a multipart body. The gate sweep sends JSON, which fails argument
    parsing first -- the gate line is never reached and the sweep says so rather than
    claiming the route.
    """

    @pytest.mark.parametrize('path', UPLOAD_PATHS)
    def test_an_upload_is_refused_while_the_api_is_off(self, env, path):
        response = env.client.post(
            path, data={'file': (_png(), 'photo.png')},
            content_type='multipart/form-data')

        assert response.status_code == 400
        assert response.get_json()['message'] == GATE

    def test_deleting_an_image_is_refused_too(self, env):
        """`/image/delete` is the fourth route on this blueprint and the one the sweep
        DOES reach: its `ImageDeleteRequest.file` is an ordinary required String, so a
        JSON body carries it. Kept beside its three siblings so the blueprint's gate is
        asserted as a whole rather than three quarters of it."""
        response = env.client.post('/api/alpha/image/delete',
                                   json={'file': 'x.png'})

        assert response.status_code == 400
        assert response.get_json()['message'] == GATE

    @pytest.mark.parametrize('path', UPLOAD_PATHS)
    def test_the_gate_is_what_refuses_and_not_the_parser(self, env, path):
        """The distinction the sweep's docstring insists on. A malformed request is
        refused by argument parsing with a DIFFERENT message, so a row asserting only the
        status code would pass for the wrong reason."""
        parsed = env.client.post(path, data={'file': (_png(), 'photo.png')},
                                 content_type='multipart/form-data')
        unparsed = env.client.post(path, json={})

        assert parsed.get_json()['message'] == GATE
        assert unparsed.get_json().get('message') != GATE


# --------------------------------------------------------------------------
# The two schemas whose requirement is a validates_schema, not a required field
# --------------------------------------------------------------------------


class TestTheOneOfTwoSchemas:
    """`GetUserRequest` and `GetPrivateMessageConversationRequest` declare no required
    fields; each carries a `@validates_schema` demanding one of two. The sweep builds only
    required fields, so it sends nothing and the schema refuses before the gate.
    """

    @pytest.mark.parametrize('path,query', [
        ('/api/alpha/user', {'person_id': 1}),
        ('/api/alpha/private_message/conversation', {'person_id': 1}),
    ])
    def test_a_valid_request_reaches_the_gate(self, env, path, query):
        response = env.client.get(path, query_string=query)

        assert response.status_code == 400
        assert response.get_json()['message'] == GATE

    @pytest.mark.parametrize('path', ['/api/alpha/user',
                                      '/api/alpha/private_message/conversation'])
    def test_an_empty_request_is_refused_by_the_schema_instead(self, env, path):
        """Why those lines stayed uncovered, asserted rather than described: with no
        arguments the answer is a validation error, not the gate."""
        response = env.client.get(path)

        assert response.status_code == 400
        assert response.get_json().get('message') != GATE


# --------------------------------------------------------------------------
# The ?debug= branch of the three list endpoints
# --------------------------------------------------------------------------


class TestTheDebugBranch:
    """`/post/list`, `/post/list2` and `/comment/list` re-load their own response through
    the schema they publish when the request asks for `debug`. That is the endpoint
    checking itself against its contract, and no request in the suite had ever asked.

    D1416, found writing these rows: `debug` was declared on NO request schema. The two
    post endpoints received it only because their `@arguments` pass `unknown=INCLUDE`, so
    it arrived as a raw STRING -- and `?debug=false` is a non-empty string, hence truthy,
    hence the debug branch. `/comment/list` has no `unknown=INCLUDE` and its `DefaultSchema`
    excludes unknown keys, so `data.get('debug')` there was always None and lines 844-845
    could not run at all. Declaring `debug = fields.Boolean()` on both request schemas
    fixes each half: the value is now coerced, and it now survives to the comment endpoint.
    """

    @pytest.fixture
    def seeded(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
        community = make_community('debugland')
        author = make_user(None, 'debugauthor', local=True)
        make_community_member(author, community)
        make_post(community, author, ap_id='https://test.piefed.local/d/1')
        db.session.commit()
        return env

    @pytest.mark.parametrize('path', ['/api/alpha/post/list', '/api/alpha/post/list2',
                                      '/api/alpha/comment/list'])
    def test_the_response_validates_against_its_own_schema(self, seeded, path):
        """If the endpoint's output ever stopped matching its declared schema, THIS is the
        request that would say so -- `.load()` raises a ValidationError rather than
        returning, so a 200 is the assertion."""
        response = seeded.client.get(path, query_string={'debug': 'true'})

        assert response.status_code == 200

    @pytest.mark.parametrize('path', ['/api/alpha/post/list', '/api/alpha/post/list2',
                                      '/api/alpha/comment/list'])
    def test_the_ordinary_request_takes_the_other_arm(self, seeded, path):
        """The `else` of each `if data.get('debug')`, which serves the response without
        re-loading it. Both arms must answer the same way for a caller who cannot tell
        them apart."""
        response = seeded.client.get(path)

        assert response.status_code == 200

    # The two arms are indistinguishable on a well-formed response, so telling them apart
    # needs a response the schema REFUSES. Each entry is the endpoint, the name the route
    # module binds its list helper to, and a payload that breaks the declared type.
    MALFORMED = [
        ('/api/alpha/post/list', 'get_post_list', {'posts': [], 'next_page': 17}),
        ('/api/alpha/post/list2', 'get_post_list2', {'posts': [], 'next_page': 17}),
        ('/api/alpha/comment/list', 'get_reply_list', {'comments': [], 'next_page': 17}),
    ]

    @pytest.mark.parametrize('path,helper,payload', MALFORMED)
    def test_a_bad_response_is_refused_by_the_debug_branch(self, seeded, monkeypatch,
                                                           path, helper, payload):
        """What the branch is FOR, asserted rather than assumed: `next_page` is declared a
        String, so an integer there is a contract violation the ordinary path would happily
        serve. Without this row every assertion above is satisfied by a `load()` that
        returned its input unchecked."""
        monkeypatch.setattr(f'app.api.alpha.routes.{helper}',
                            lambda *args, **kwargs: dict(payload))

        response = seeded.client.get(path, query_string={'debug': 'true'})

        # The `ValidationError` escapes the view and the app's own handler turns it into a
        # 400, so the malformed body never goes out. A 400 is also what the closed gate
        # returns, hence the message check: without it this row would pass against an API
        # that had simply been switched off.
        assert response.status_code == 400
        assert response.get_json()['message'] != GATE
        assert 'next_page' in str(response.get_json())

    @pytest.mark.parametrize('path,helper,payload', MALFORMED)
    def test_the_same_bad_response_is_served_without_debug(self, seeded, monkeypatch,
                                                           path, helper, payload):
        """The other arm on the same input. An endpoint asked plainly does NOT check
        itself -- which is why `debug` exists and why it is documented as for testing."""
        monkeypatch.setattr(f'app.api.alpha.routes.{helper}',
                            lambda *args, **kwargs: dict(payload))

        response = seeded.client.get(path)

        assert response.status_code == 200
        assert response.get_json()['next_page'] == 17

    @pytest.mark.parametrize('path,helper,payload', MALFORMED)
    def test_debug_false_takes_the_ordinary_arm(self, seeded, monkeypatch, path, helper,
                                                payload):
        """The D1416 regression. Before `debug` was declared, `?debug=false` reached the
        two post endpoints as the STRING 'false', which is truthy, so a client that
        explicitly switched the feature off got it anyway. With the field declared the
        value is coerced and the bad response is served rather than refused."""
        monkeypatch.setattr(f'app.api.alpha.routes.{helper}',
                            lambda *args, **kwargs: dict(payload))

        response = seeded.client.get(path, query_string={'debug': 'false'})

        assert response.status_code == 200
        assert response.get_json()['next_page'] == 17

    def test_a_non_boolean_debug_is_now_a_validation_error(self, seeded):
        """The cost of declaring the field, stated rather than discovered later: a value
        marshmallow cannot read as a boolean is refused instead of being treated as true.
        `debug` is documented as for testing only, so this is the right trade."""
        response = seeded.client.get('/api/alpha/post/list',
                                     query_string={'debug': 'perhaps'})

        assert response.status_code == 400
        assert response.get_json()['message'] != GATE


# --------------------------------------------------------------------------
# The success-path `Schema().load(resp)` returns
# --------------------------------------------------------------------------


class TestTheSuccessReturns:
    """Every route on this module ends `return <Schema>().load(resp)`, and that line is
    reached only by a request that gets all the way through. Five such lines were still
    red: `resolve_object`, the three uploads, and `image/delete`.

    Each of these asserts the SHAPE the schema produces, not merely a 200: `.load()` on a
    `DefaultSchema` drops unknown keys, so a route returning the wrong dict still answers
    200 while serving something the caller cannot use.
    """

    @pytest.fixture
    def signed_in(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
        user = env.baseline.user1
        env.headers = {'Authorization': f'Bearer {user.encode_jwt_token()}'}
        return env

    def test_resolving_a_local_post_by_its_ap_id_returns_a_post_view(self, signed_in):
        """`:176`. `get_resolve_object` walks five tables in order looking for the `q`
        string; a `Post.ap_id` matches on the second, so this is also the shortest request
        that proves the walk reaches past its first table."""
        post = signed_in.baseline.post1

        response = signed_in.client.get('/api/alpha/resolve_object',
                                        query_string={'q': post.ap_id},
                                        headers=signed_in.headers)

        assert response.status_code == 200
        assert response.get_json()['post']['post']['id'] == post.id

    def test_resolving_without_a_token_is_refused_400(self, signed_in):
        """PERM-1, fixed (owner ruling). The same request with no Authorization header
        gets the API's standard `incorrect_login` 400; it used to be served anonymously."""
        response = signed_in.client.get('/api/alpha/resolve_object',
                                        query_string={'q': signed_in.baseline.post1.ap_id})

        assert response.status_code == 400
        assert response.get_json()['message'] == 'incorrect_login'

    @pytest.mark.parametrize('path', UPLOAD_PATHS)
    def test_an_upload_returns_the_url_of_the_stored_file(self, signed_in, monkeypatch,
                                                          path):
        """`:1602`, `:1618`, `:1634`. `process_upload` is the leaf that writes to disk and
        re-encodes the image; it has its own rows elsewhere, so it is replaced here and
        everything above it -- auth, the multipart parse, the response schema -- is real.
        """
        monkeypatch.setattr('app.api.alpha.utils.upload.process_upload',
                            lambda *args, **kwargs: 'https://test.piefed.local/stored.png')

        response = signed_in.client.post(
            path, data={'file': (_png(), 'photo.png')},
            content_type='multipart/form-data', headers=signed_in.headers)

        assert response.status_code == 200
        assert response.get_json()['url'] == 'https://test.piefed.local/stored.png'

    def test_deleting_an_image_answers_ok(self, signed_in, monkeypatch):
        """`:1648`. `ImageDeleteResponse` declares `result` required, so the literal
        `{'result': 'ok'}` the helper returns is the whole contract."""
        deleted = []
        monkeypatch.setattr('app.api.alpha.utils.upload.process_file_delete',
                            lambda url, user_id=None: deleted.append((url, user_id)))

        response = signed_in.client.post('/api/alpha/image/delete',
                                         json={'file': 'https://test.piefed.local/x.png'},
                                         headers=signed_in.headers)

        assert response.status_code == 200
        assert response.get_json()['result'] == 'ok'
        # The file named in the request, deleted on behalf of the user who signed it --
        # not some other user's file, which is the only interesting thing this route can
        # get wrong.
        assert deleted == [('https://test.piefed.local/x.png', signed_in.baseline.user1.id)]

    def test_an_unauthenticated_delete_is_refused(self, signed_in, monkeypatch):
        """The guard beside it. `post_image_delete` raises `incorrect_login` when neither
        a token nor a session identifies the caller, so no `process_file_delete` runs."""
        deleted = []
        monkeypatch.setattr('app.api.alpha.utils.upload.process_file_delete',
                            lambda url, user_id=None: deleted.append((url, user_id)))

        response = signed_in.client.post('/api/alpha/image/delete',
                                         json={'file': 'https://test.piefed.local/x.png'})

        assert response.status_code != 200
        assert deleted == []
