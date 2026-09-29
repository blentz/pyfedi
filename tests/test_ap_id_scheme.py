"""D1406: an announced Create's object id, stored as ap_id and rendered as an href.

`Post.ap_id` and `PostReply.ap_id` come from `request_json['object']['id']`, a string the
sending peer chooses. Three things read it back:

* templates. `post/post_options.html:234` and `post/post_reply_options.html:194` render it
  as the "view on remote instance" link -- `<a href="{{ post.ap_id }}">`, no guard.
* lookups. `Post.get_by_ap_id` and `filter_by(ap_id=...)` use it as the key that makes a
  Create idempotent and lets a later Update or Delete find the row.
* fetches. `resolve_remote_post` requests it, and this instance signs that request.

THE DIRECT PATH WAS GUARDED AND THE ANNOUNCED PATH WAS NOT. `ensure_domains_match`, which
requires the object id's host to equal the actor's host, is called from exactly one place
(`app/activitypub/routes.py:1251`), inside `if not announced and not community:`. An
Announce skips that block entirely -- and an Announce is the ordinary way a Lemmy
community relays a post, so the guarded path is the rarer one. Measured through the
dispatcher, with `can_create_post` doubled (this fixture's author has no `ap_domain`, so
`instance_banned(None)` refuses it for an unrelated reason -- fact 781's shape):

    PROBE announced posts:   [(1, 'javascript:alert(document.domain)', 'A post')]
    PROBE announced log:     [('success', None)]
    PROBE announced replies: [(1, 'javascript:alert(document.domain)')]
    PROBE announced log:     [('success', None)]
    PROBE direct log:        [('failure', 'Domains do not match')]

Both stored, both logged SUCCESS. The refusal now lives in `create_post` and
`create_post_reply`, one per object kind, beside the `local_only` and visibility refusals
they already had -- and a third time in `app/community/util.py`, where a fetched comment
tree builds replies from `reply_data['id']` and never passed through an inbox at all.

AN ALLOWLIST OF http(s), which makes three fields with three answers in four rounds:
an Event's links (allowlist, because the local form already required it), `Post.url`
(blocklist, because remote software picks a link a person clicks and `magnet:` is real), an
image url (allowlist, because this instance fetches it) and now an ap id -- allowlist, for
the fetch reason plus the specification: an ActivityPub id is an https URI.

REFUSED RATHER THAN DROPPED, unlike every other field in these four rounds, because
`ap_id` is not optional. A post with no id cannot be deduplicated, updated or deleted by
its author later, so storing the rest of it would leave a row this instance can never
reconcile with its origin.
"""
import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.activitypub.util import create_post, create_post_reply
from app.models import ActivityPubLog, Post, PostReply, utcnow
from tests.factories import make_community, make_post, make_site, make_user, seed_community_owner
from tests.test_inbox_dispatch_new_content import announced_activity, content_object, direct_activity
from tests.test_inbox_dispatch_preamble import dispatch

HOST = 'peer.example'
HOSTILE = 'javascript:alert(document.domain)'

NOT_AP_IDS = [
    HOSTILE,
    'JavaScript:alert(1)',
    ' javascript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'vbscript:msgbox(1)',
    # An id has to be a url this instance can fetch and sign a request to, so these are
    # refused for the same reason rather than as dangerous strings.
    'magnet:?xt=urn:btih:0123456789abcdef',
    'tag:peer.example,2026:objects/1',
    'urn:uuid:1f0c3a6e-0000-0000-0000-000000000000',
    '//peer.example/p/1',
    '/p/1',
    'peer.example/p/1',
    '',
    None,
]


@pytest.fixture
def env(app, db_session, monkeypatch):
    """A remote author and a community, with the two refusals that have nothing to do
    with this rule taken out of the way.

    `can_create_post`/`can_create_post_reply`: the factory author has no `ap_domain`, and
    `instance_banned(None)` is True by design (app/utils.py:2486 -- an absent domain is
    refused rather than waved through), so the real gate refuses this author for a reason
    that would hide the behaviour under test. `ap_fetched_at` is stamped so the preamble
    does not schedule a real actor fetch.
    """
    from types import SimpleNamespace
    make_site()
    instance = seed_community_owner(HOST)
    community = make_community(host=HOST)
    author = make_user(instance, 'author')
    author.ap_fetched_at = utcnow()
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'can_create_post', lambda user, content: True)
    monkeypatch.setattr(activitypub_routes, 'can_create_post_reply',
                        lambda user, content: True)
    monkeypatch.setattr('app.activitypub.util.make_image_sizes', lambda *a, **k: None)
    monkeypatch.setattr('app.utils.mime_type_using_head', lambda url: '')
    return SimpleNamespace(instance=instance, community=community, author=author)


def _object(ap_id, **extra):
    """A public Page. `to` matters: without it `activitypub_visibility` answers 'direct'
    and `create_post` refuses before reaching the id check, which is how the first
    version of this probe measured nothing."""
    return content_object(ap_id, name='A post', content='<p>hi</p>',
                          to=['https://www.w3.org/ns/activitystreams#Public'], **extra)


def _reply_object(ap_id, in_reply_to, **extra):
    """A public Note under `in_reply_to`, and deliberately WITHOUT `name`: a Note that
    has both `inReplyTo` and `name` is a POLL VOTE to the dispatcher, whose `name` is the
    choice voted for. With one, these rows were refused as 'Poll vote for a post with no
    poll' and never reached `create_post_reply`."""
    return content_object(ap_id, object_type='Note', in_reply_to=in_reply_to,
                          content='<p>hi</p>',
                          to=['https://www.w3.org/ns/activitystreams#Public'], **extra)


def _log_messages():
    return [row.exception_message for row in ActivityPubLog.query.all()]


# --------------------------------------------------------------------------
# Through the dispatcher, on the path that had no check
# --------------------------------------------------------------------------


class TestAnAnnouncedCreate:
    @pytest.fixture(autouse=True)
    def logging_on(self, app, monkeypatch):
        monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)

    @pytest.mark.parametrize('ap_id', [HOSTILE, 'data:text/html,<script>alert(1)</script>',
                                       'magnet:?xt=urn:btih:abc', '/p/1'])
    def test_a_post_whose_id_is_not_an_http_url_is_not_stored(self, env, ap_id):
        obj = _object(ap_id, attributedTo=env.author.ap_profile_id,
                      audience=env.community.ap_profile_id)

        dispatch(announced_activity(env.community, env.author, obj))

        assert Post.query.count() == 0
        assert 'Object id is not an http(s) url' in _log_messages()

    def test_a_post_with_a_real_id_still_arrives(self, env):
        """The control, and the witness that this path reaches `create_post` at all --
        every row above asserts an absence, which an unreachable path also produces."""
        obj = _object(f'https://{HOST}/p/1', attributedTo=env.author.ap_profile_id,
                      audience=env.community.ap_profile_id)

        dispatch(announced_activity(env.community, env.author, obj))

        post = Post.query.one()
        assert post.ap_id == f'https://{HOST}/p/1'
        assert post.title == 'A post'

    def test_a_reply_whose_id_is_not_an_http_url_is_not_stored(self, env):
        parent = make_post(env.community, env.author, f'https://{HOST}/p/parent')
        db.session.commit()
        obj = _reply_object(HOSTILE, parent.ap_id,
                            attributedTo=env.author.ap_profile_id,
                            audience=env.community.ap_profile_id)

        dispatch(announced_activity(env.community, env.author, obj))

        assert PostReply.query.count() == 0
        assert 'Object id is not an http(s) url' in _log_messages()

    def test_a_reply_with_a_real_id_still_arrives(self, env):
        parent = make_post(env.community, env.author, f'https://{HOST}/p/parent2')
        db.session.commit()
        obj = _reply_object(f'https://{HOST}/comment/1', parent.ap_id,
                            attributedTo=env.author.ap_profile_id,
                            audience=env.community.ap_profile_id)

        dispatch(announced_activity(env.community, env.author, obj))

        assert PostReply.query.one().ap_id == f'https://{HOST}/comment/1'


def test_the_direct_path_still_refuses_for_its_own_reason(app, db_session, env,
                                                          monkeypatch):
    """`ensure_domains_match` is what stops this on the unannounced path, and it stops it
    EARLIER -- before `create_post` is reached. Both refusals are wanted: this row exists
    so that a later change moving or removing the domain check shows up as the loss of a
    second, independent guard rather than as nothing at all."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    obj = _object(HOSTILE, attributedTo=env.author.ap_profile_id,
                  audience=env.community.ap_profile_id)

    dispatch(direct_activity(env.author, obj))

    assert Post.query.count() == 0
    assert 'Domains do not match' in _log_messages()


# --------------------------------------------------------------------------
# The two boundaries, called directly, over the whole table
# --------------------------------------------------------------------------


class TestTheBoundaries:
    @pytest.mark.parametrize('ap_id', NOT_AP_IDS)
    def test_create_post_refuses_every_id_that_is_not_an_http_url(self, env, ap_id):
        activity = {'id': f'https://{HOST}/activities/1', 'type': 'Create',
                    'actor': env.author.ap_profile_id,
                    'object': _object(ap_id, attributedTo=env.author.ap_profile_id,
                                      audience=env.community.ap_profile_id)}

        assert create_post(False, env.community, activity, env.author) is None
        assert Post.query.count() == 0

    @pytest.mark.parametrize('ap_id', NOT_AP_IDS)
    def test_create_post_reply_refuses_every_id_that_is_not_an_http_url(self, env, ap_id):
        parent = make_post(env.community, env.author, f'https://{HOST}/p/x{abs(hash(str(ap_id)))}')
        db.session.commit()
        activity = {'id': f'https://{HOST}/activities/2', 'type': 'Create',
                    'actor': env.author.ap_profile_id,
                    'object': _reply_object(ap_id, parent.ap_id,
                                            attributedTo=env.author.ap_profile_id,
                                            audience=env.community.ap_profile_id)}

        assert create_post_reply(False, env.community, parent.ap_id, activity,
                                 env.author) is None
        assert PostReply.query.count() == 0

    @pytest.mark.parametrize('shape', [{}, {'object': None}, {'object': 'a string'},
                                       {'object': ['a', 'list']}, {'object': {}},
                                       {'object': {'id': 5}}])
    def test_an_object_that_is_not_a_dict_with_an_id_is_refused_not_a_crash(self, env,
                                                                           shape):
        """`request_json['object']` is a peer's value and has been every JSON type in this
        campaign's findings. The guard reads it with `.get` behind an `isinstance`, so
        none of these is a TypeError or a KeyError out of the boundary."""
        activity = {'id': f'https://{HOST}/activities/3', 'type': 'Create',
                    'actor': env.author.ap_profile_id}
        activity.update(shape)

        assert create_post(False, env.community, activity, env.author) is None

    def test_create_post_accepts_a_real_id(self, env):
        """The control for the table above."""
        activity = {'id': f'https://{HOST}/activities/4', 'type': 'Create',
                    'actor': env.author.ap_profile_id,
                    'object': _object(f'https://{HOST}/p/ok',
                                      attributedTo=env.author.ap_profile_id,
                                      audience=env.community.ap_profile_id)}

        post = create_post(False, env.community, activity, env.author)

        assert post is not None
        assert post.ap_id == f'https://{HOST}/p/ok'


# --------------------------------------------------------------------------
# The third site: a comment tree this instance fetched
# --------------------------------------------------------------------------


def test_a_fetched_comment_tree_skips_a_reply_whose_id_is_not_an_http_url():
    """`app/community/util.py` builds `PostReply` rows from a Lemmy API response's
    `reply_data['id']`, which never passes through an inbox and so inherits none of the
    checks above. Asserted over the source rather than by driving the fetch: the loop sits
    inside three levels of pagination over a live API shape, and what matters here is that
    the same predicate guards the same column at all three producers."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / 'app' / 'community'
              / 'util.py').read_text()

    assert "if _as_url(reply_data.get('id')) is None:" in source
    assert source.count("reply_data['object'] = {'id': reply_data['id']}") == 1


# --------------------------------------------------------------------------
# The sinks
# --------------------------------------------------------------------------


def test_the_templates_still_render_ap_id_as_an_href():
    """Why the ingest is the boundary. If one of these gains a guard, this row fails and
    whoever reads it should be told the boundary moved."""
    from pathlib import Path

    templates = Path(__file__).resolve().parent.parent / 'app' / 'templates' / 'post'

    assert 'href="{{ post.ap_id }}"' in (templates / 'post_options.html').read_text()
    assert 'href="{{ post_reply.ap_id }}"' in \
        (templates / 'post_reply_options.html').read_text()
