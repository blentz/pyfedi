"""D1399: two unauthenticated ActivityPub endpoints, one of which authorised anything.

`/quote_boost_auth` is the `result` URL of a FEP-044f `Accept`. `process_quote_boost`
(app/activitypub/util.py:4095) decides whether to accept a `QuoteRequest` -- the quoted
object must exist here and its author must be local -- and then sends an `Accept` whose
`result` points at this endpoint. A peer dereferences that URL to CHECK the
authorisation, which is the whole reason it exists.

It checked nothing. Measured before the repair:

    GET /quote_boost_auth?stamp=abc                ValueError: substring not found
    GET /quote_boost_auth?stamp=                   ValueError: substring not found
    GET /quote_boost_auth?stamp=no-semicolon-here  ValueError: substring not found
    GET /quote_boost_auth                          404
    GET /quote_boost_auth?stamp=https://evil.test/p/1;https://evil.test/p/2
        200  {"type": "QuoteAuthorization",
              "interactionTarget": "https://evil.test/p/1",
              "interactingObject": "https://evil.test/p/2",
              "attributedTo": "https://test.piefed.local"}
    GET /quote_boost_auth?stamp=anything;anything
        200  interactionTarget: "anything"

Two defects in six lines. `stamp.index(';')` raised for any stamp without one, and the
`is None` guard above it ruled out only an absent parameter -- so `?stamp=` was a 500
as well (D1389's shape, a guard that rules out absent and nothing else). And the
authorisation itself was unconditional: this instance told a peer it had authorised a
quote of a post it does not host, for a caller who invented both halves of the stamp.
The condition `process_quote_boost` had checked was neither carried in the stamp nor
re-asked.

THE RESIDUAL, recorded because it needs a table rather than a guard: nothing persists
WHICH `QuoteRequest`s were accepted. So the endpoint can now confirm that the target is
a local post whose author is local, and it still cannot tell "this author approved this
quote" from "this post exists". A caller may name any local post beside any remote one.
Closing that means storing the accepted requests in `process_quote_boost` and looking
them up here -- a schema change, out of a coverage round's scope, and the guard added
here is strictly the condition that was already decided and then thrown away.

`/activitypub/externalInteraction` is the second endpoint, and the same family as the
`else: abort(404)` that `feed_moderators_route` and `feed_followers` were given: both of
its arms fell off the end of the view and returned None, which Flask answers with
`TypeError: The view function ... did not return a valid response`.
"""
import pytest
from flask import g
from unittest.mock import patch

from app import db
from app.activitypub.util import process_quote_boost
from app.models import Community, Post, PostReply, QuoteAuthorization, Site
from tests.factories import (make_community, make_instance, make_post,
                             make_post_reply, make_user)

pytestmark = pytest.mark.usefixtures('site')
HOST = 'test.piefed.local'
PEER = 'peer.test'

# Stamps that never reach a lookup. '' and the no-separator values were the 500s; the
# last two are a separator with nothing on one side of it.
UNUSABLE_STAMPS = ['abc', '', 'no-semicolon-here', 'https://test.piefed.local/post/1',
                   ';', ';https://peer.test/p/2', 'https://test.piefed.local/post/1;']


@pytest.fixture
def env(app, db_session):
    """A local post and reply whose authors are local, plus a remote post."""
    from types import SimpleNamespace
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    g.admin_ids = []
    db.session.commit()
    local = make_instance(HOST, software='piefed')
    make_user(local, 'founder', local=True)
    author = make_user(local, 'author', local=True)
    community = make_community('general')
    db.session.commit()
    # A LOCAL post still carries an `ap_id`, and it is this instance's own url:
    # `Post.generate_ap_id` (app/models.py:3356) sets
    # `<SERVER_URL>/c/<community>@<domain>/p/<id>/<slug>` for every post this
    # instance creates, and `get_by_ap_id` matches that column and nothing else. A
    # fixture leaving it None is a row the product cannot produce (fact 781) -- and
    # one that makes `get_by_ap_id` answer None for every local post, so the control
    # rows below would have failed while looking like the guard refusing them.
    post = make_post(community, author,
                     f'https://{HOST}/c/general@{HOST}/p/1/a-local-post',
                     title='A LOCAL POST')
    reply = make_post_reply(post, author, body='a local reply')
    reply.ap_id = f'https://{HOST}/comment/1'
    peer = make_instance(PEER)
    stranger = make_user(peer, 'stranger')
    remote_post = make_post(community, stranger, f'https://{PEER}/p/9',
                            title='A REMOTE POST')
    db.session.commit()
    return SimpleNamespace(app=app, client=app.test_client(), post=post,
                           reply=reply, remote_post=remote_post, author=author,
                           stranger=stranger, community=community)


def _auth(env, stamp):
    return env.client.get('/quote_boost_auth', query_string={'stamp': stamp})


def _approve(quoted, quoting_uri):
    """The row process_quote_boost writes when it Accepts a QuoteRequest (R205)."""
    if isinstance(quoted, Post):
        db.session.add(QuoteAuthorization(post_id=quoted.id, quoting_uri=quoting_uri))
    else:
        db.session.add(QuoteAuthorization(post_reply_id=quoted.id, quoting_uri=quoting_uri))
    db.session.commit()


def _accept_a_quote_request(env, quoted_ap_id, quoting_uri):
    """process_quote_boost, as the inbox calls it, with the delivery doubled."""
    activity = {'type': 'QuoteRequest', 'id': f'https://{PEER}/activities/1',
                'actor': env.stranger.ap_profile_id, 'object': quoted_ap_id,
                'instrument': {'id': quoting_uri}}
    env.stranger.instance.inbox = f'https://{PEER}/inbox'
    db.session.commit()
    with patch('app.activitypub.util.find_actor_or_create_cached', return_value=env.stranger), \
            patch('app.activitypub.util.send_post_request') as send:
        process_quote_boost(activity, quoted_ap_id, quoting_uri)
    return send


# --------------------------------------------------------------------------
# The stamp's shape
# --------------------------------------------------------------------------


class TestTheStamp:
    def test_an_absent_stamp_is_a_404(self, env):
        """The one case the original guard did cover."""
        assert env.client.get('/quote_boost_auth').status_code == 404

    @pytest.mark.parametrize('stamp', UNUSABLE_STAMPS)
    def test_a_stamp_that_does_not_split_is_a_404(self, env, stamp):
        """`stamp.index(';')` was `ValueError: substring not found` for every one of
        these, and `?stamp=` reached it because `is None` is not `not stamp`."""
        assert _auth(env, stamp).status_code == 404

    def test_the_absent_and_empty_stamps_are_refused_the_same_way(self, env):
        """THE ROUND'S RESIDUAL, and an EQUIVALENT MUTANT rather than a gap.

        `if not stamp:` guards `stamp.partition(';')` against an absent parameter,
        where `None.partition` is an AttributeError. Mutating it to `if stamp is None:`
        survives every test, and no test can kill it: the only input the two spellings
        disagree about is `''`, and `''` passes the mutated guard, partitions to
        `('', '', '')` and is refused by the both-halves check on the next line -- the
        same 404, for the same caller, with no observable difference.

        So the two programs are the same program. `not stamp` is kept because it says
        what it means at the point a reader looks for it, and this row records why its
        mutant is expected to live rather than leaving a survivor unexplained.
        """
        assert env.client.get('/quote_boost_auth').status_code == 404
        assert _auth(env, '').status_code == 404

    def test_a_real_target_with_no_quoting_object_is_refused(self, env):
        """The row that separates the two-halves guard from the lookup below it.

        Every other unusable stamp is refused by the lookup anyway -- the left half
        names no local post -- so a mutant deleting `if not local_post_id or not
        remote_post_id` survived all of them. This one names a REAL local post and
        leaves the quoting object empty, which the lookup would happily authorise: a
        QuoteAuthorization whose `interactingObject` is ''.
        """
        response = _auth(env, f'{env.post.profile_id()};')

        assert response.status_code == 404

    def test_a_quoting_object_with_no_target_is_refused(self, env):
        """The mirror, for the other operand of the same guard."""
        response = _auth(env, f';https://{PEER}/p/2')

        assert response.status_code == 404

    def test_the_separator_is_the_first_one(self, env):
        """`partition`, so a remote url containing its own `;` keeps it rather than
        being cut at the last separator. The target is what this instance vouches
        for, so it is the half that must not shift."""
        remote = f'https://{PEER}/p/9;extra'
        _approve(env.post, remote)

        response = _auth(env, f'{env.post.profile_id()};{remote}')

        assert response.status_code == 200
        body = response.get_json()
        assert body['interactionTarget'] == env.post.profile_id()
        assert body['interactingObject'] == remote


# --------------------------------------------------------------------------
# What the endpoint will and will not vouch for
# --------------------------------------------------------------------------


class TestWhatItAuthorises:
    def test_a_post_this_instance_does_not_host_is_refused(self, env):
        """The defect. This answered 200 with a `QuoteAuthorization` attributed to
        this server, naming a post on another one."""
        response = _auth(env, f'https://evil.test/p/1;https://{PEER}/p/2')

        assert response.status_code == 404

    def test_a_target_that_names_nothing_at_all_is_refused(self, env):
        response = _auth(env, f'anything;https://{PEER}/p/2')

        assert response.status_code == 404

    def test_a_local_post_by_a_remote_author_is_refused(self, env):
        """`process_quote_boost` requires `post.author.is_local()` before it ever
        issues one of these URLs, so the endpoint requires it too. A post in a local
        community written by a remote account is the case that separates "we host the
        row" from "our user wrote it"."""
        response = _auth(env, f'{env.remote_post.profile_id()};https://{PEER}/p/2')

        assert response.status_code == 404

    def test_a_local_post_by_a_local_author_is_authorised(self, env):
        """The control. A repair that refused everything would pass every row above
        and break the feature."""
        _approve(env.post, f'https://{PEER}/p/2')
        response = _auth(env, f'{env.post.profile_id()};https://{PEER}/p/2')

        assert response.status_code == 200
        body = response.get_json()
        assert body['type'] == 'QuoteAuthorization'
        assert body['interactionTarget'] == env.post.profile_id()
        assert body['interactingObject'] == f'https://{PEER}/p/2'

    def test_a_local_reply_is_authorised_too(self, env):
        """`process_quote_boost` falls back to `PostReply.get_by_ap_id`, so this does
        as well -- a quote of a comment is authorised the same way as a quote of a
        post."""
        _approve(env.reply, f'https://{PEER}/p/2')
        response = _auth(env, f'{env.reply.profile_id()};https://{PEER}/p/2')

        assert response.status_code == 200
        assert response.get_json()['interactionTarget'] == env.reply.profile_id()

    def test_the_document_is_served_as_activitypub(self, env):
        _approve(env.post, f'https://{PEER}/p/2')
        response = _auth(env, f'{env.post.profile_id()};https://{PEER}/p/2')

        assert response.headers['Content-Type'] == 'application/activity+json'

    def test_the_id_echoes_the_stamp_it_was_asked_about(self, env):
        """The `id` is this same URL, percent-encoded, so a consumer can tell two
        authorisations apart. It is built from the two halves rather than from the raw
        query string."""
        _approve(env.post, f'https://{PEER}/p/2')
        response = _auth(env, f'{env.post.profile_id()};https://{PEER}/p/2')

        body = response.get_json()
        assert body['id'].startswith('https://test.piefed.local/quote_boost_auth?stamp=')
        assert '%3A%2F%2F' in body['id']
        assert body['attributedTo'] == 'https://test.piefed.local'


class TestOnlyARecordedApprovalIsAuthorised:
    """R205, owner ruling: the residual above is closed. process_quote_boost records each
    QuoteRequest it Accepts -- the quoted post or reply, the quoting object's URI and when
    -- and the endpoint answers only for a recorded one, 404 otherwise."""

    def test_accepting_a_quote_request_records_the_approval(self, env):
        _accept_a_quote_request(env, env.post.ap_id, f'https://{PEER}/p/2')

        row = QuoteAuthorization.query.one()
        assert (row.post_id, row.post_reply_id, row.quoting_uri) == (env.post.id, None, f'https://{PEER}/p/2')
        assert row.approved_at is not None

    def test_accepting_a_quote_of_a_reply_records_the_reply(self, env):
        _accept_a_quote_request(env, env.reply.ap_id, f'https://{PEER}/p/2')

        row = QuoteAuthorization.query.one()
        assert (row.post_id, row.post_reply_id) == (None, env.reply.id)

    def test_accepting_the_same_request_twice_records_it_once(self, env):
        _accept_a_quote_request(env, env.post.ap_id, f'https://{PEER}/p/2')
        _accept_a_quote_request(env, env.post.ap_id, f'https://{PEER}/p/2')

        assert QuoteAuthorization.query.count() == 1

    def test_a_refused_request_records_nothing(self, env):
        _accept_a_quote_request(env, env.remote_post.ap_id, f'https://{PEER}/p/2')

        assert QuoteAuthorization.query.count() == 0

    def test_an_accepted_request_is_then_authorised(self, env):
        _accept_a_quote_request(env, env.post.ap_id, f'https://{PEER}/p/2')

        assert _auth(env, f'{env.post.profile_id()};https://{PEER}/p/2').status_code == 200

    def test_a_quote_nobody_approved_is_a_404(self, env):
        """The residual: a real local post by a local author, named beside a quoting
        object this instance never accepted a request from."""
        assert _auth(env, f'{env.post.profile_id()};https://{PEER}/p/2').status_code == 404

    def test_an_approval_for_another_quoting_object_does_not_carry_over(self, env):
        _approve(env.post, f'https://{PEER}/p/3')

        assert _auth(env, f'{env.post.profile_id()};https://{PEER}/p/2').status_code == 404

    def test_an_approval_of_the_post_does_not_cover_its_reply(self, env):
        _approve(env.post, f'https://{PEER}/p/2')

        assert _auth(env, f'{env.reply.profile_id()};https://{PEER}/p/2').status_code == 404


def test_the_endpoint_asks_the_same_question_process_quote_boost_asks():
    """The pair, over the source. `process_quote_boost` decides whether to issue one of
    these URLs and the endpoint decides whether to honour one, and the two must ask the
    same thing -- a Post or PostReply by ap_id, then `author.is_local()`. This is the
    producer/consumer rule of D1398 and round 203 applied to a decision rather than a
    field.
    """
    import ast
    import inspect

    import app.activitypub.routes as routes
    import app.activitypub.util as util

    def reads(function_source):
        tree = ast.parse(function_source.lstrip())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
        return names

    endpoint = reads(inspect.getsource(routes.quote_boost_auth))
    producer = reads(inspect.getsource(util.process_quote_boost))

    for required in ('get_by_ap_id', 'is_local'):
        assert required in endpoint, required
        assert required in producer, required


# --------------------------------------------------------------------------
# /activitypub/externalInteraction
# --------------------------------------------------------------------------


class TestTheExternalInteractionHandOff:
    def test_a_missing_uri_is_a_404_not_a_crash(self, env):
        """Fell off the end of the view and returned None:
        `TypeError: The view function ... did not return a valid response`."""
        assert env.client.get('/activitypub/externalInteraction').status_code == 404

    @pytest.mark.parametrize('uri', ['', '   '])
    def test_an_empty_uri_is_a_404(self, env, uri):
        response = env.client.get('/activitypub/externalInteraction',
                                  query_string={'uri': uri})

        assert response.status_code == 404

    def test_a_uri_naming_no_community_is_a_404(self, env):
        """The second arm, and the second None. `find_actor_or_create_cached` is
        patched because the question here is what the route does with the answer, not
        how it fetches one -- an unpatched call would try the network."""
        with patch('app.activitypub.routes.find_actor_or_create_cached',
                   return_value=None):
            response = env.client.get('/activitypub/externalInteraction',
                                      query_string={'uri': 'https://peer.test/c/nope'})

        assert response.status_code == 404

    def test_a_uri_naming_something_that_is_not_a_community_is_a_404(self, env):
        """`community_only=True` is passed, and the return is still checked: the
        redirect below builds a `/community/<link>/subscribe` url, so anything that is
        not a Community would send the caller to a page about the wrong thing."""
        with patch('app.activitypub.routes.find_actor_or_create_cached',
                   return_value=env.author):
            response = env.client.get('/activitypub/externalInteraction',
                                      query_string={'uri': f'https://{HOST}/u/author'})

        assert response.status_code == 404

    def test_a_community_is_redirected_to_its_subscribe_page(self, env):
        """The control."""
        with patch('app.activitypub.routes.find_actor_or_create_cached',
                   return_value=env.community) as lookup:
            response = env.client.get(
                '/activitypub/externalInteraction',
                query_string={'uri': f'https://{HOST}/c/general'})

        assert response.status_code == 302
        # The community page, where Join is a CSRF-checked form: subscribe is
        # POST-only since D994's sibling fix, so a hand-off cannot join by GET.
        assert response.headers['Location'] == f'/c/{env.community.link()}'
        assert lookup.call_args.kwargs == {'community_only': True}
