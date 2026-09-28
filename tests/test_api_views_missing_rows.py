"""What the API's view helpers do with an id nobody holds.

Ten functions in `app/api/alpha/views.py` are annotated to take a model OR an
integer, and every caller that has only an id relies on that -- `build_removed_comments`
in `app/api/alpha/utils/misc.py` passes `entry.reply.user_id` and `entry.reply.post_id`,
for instance.

D1367. Four resolved the id in a way that reports a missing row -- `post_view` with
`db.session.get` plus an explicit `raise NoResultFound`, and `community_view`,
`flair_view` and `instance_view` with `.filter_by(id=...).one()`, which raises the
same thing. The other six used `db.session.get` and then read attributes off the
result, so an id nobody holds was

    AttributeError: 'NoneType' object has no attribute '__table__'

`shared_error_handler` in `app/api/alpha/__init__.py` turns `NoResultFound` into a
400 `{"status": "Not found"}`; an AttributeError is not handled there, so those six
answered a 500 (and a Sentry event) for a request that is simply about something
that does not exist.

Measured before the repair:

    post_view                      NoResultFound
    community_view                 NoResultFound
    flair_view                     NoResultFound
    instance_view                  NoResultFound
    user_view                      AttributeError: ... has no attribute '__table__'
    reply_view                     AttributeError: ... has no attribute '__table__'
    conversation_information_view  AttributeError: ... has no attribute 'id'
    conversation_report_view       AttributeError: ... 'suspect_conversation_id'
    feed_view, topic_view          the same `get` with no guard
"""
import pytest
from flask import g
from sqlalchemy.orm.exc import NoResultFound

from app import db
from app.api.alpha import views
from app.models import Site

MISSING = 999999


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    db.session.commit()
    return api_baseline


class TestAnIdNobodyHolds:
    """Every one of the ten, so the rule is "this file answers NoResultFound" rather
    than "these four do"."""

    @pytest.mark.parametrize('call', [
        pytest.param(lambda: views.post_view(MISSING, 1), id='post_view'),
        pytest.param(lambda: views.user_view(MISSING, 1), id='user_view'),
        pytest.param(lambda: views.community_view(MISSING, 1), id='community_view'),
        pytest.param(lambda: views.flair_view(MISSING), id='flair_view'),
        pytest.param(lambda: views.reply_view(MISSING, 1), id='reply_view'),
        pytest.param(lambda: views.instance_view(MISSING, 1), id='instance_view'),
        pytest.param(lambda: views.conversation_information_view(MISSING),
                     id='conversation_information_view'),
        pytest.param(lambda: views.conversation_report_view(MISSING),
                     id='conversation_report_view'),
    ])
    def test_it_raises_no_result_found(self, env, call):
        with pytest.raises(NoResultFound):
            call()

    def test_feed_view(self, env):
        """Its signature takes eight more positional arguments, all of which are
        read after the id is resolved, so the guard has to come first."""
        import inspect

        parameters = list(inspect.signature(views.feed_view).parameters)
        arguments = {name: None for name in parameters[2:]}

        with pytest.raises(NoResultFound):
            views.feed_view(MISSING, 1, **arguments)

    def test_topic_view(self, env):
        import inspect

        parameters = list(inspect.signature(views.topic_view).parameters)
        arguments = {name: None for name in parameters[2:]}

        with pytest.raises(NoResultFound):
            views.topic_view(MISSING, 1, **arguments)


class TestTheErrorThatReachesTheClient:
    """Why `NoResultFound` rather than any other exception: it is the one
    `shared_error_handler` knows."""

    def test_the_handler_maps_it_to_a_400(self, app):
        from app.api.alpha import shared_error_handler

        with app.test_request_context():
            response, status = shared_error_handler(NoResultFound())

        assert status == 400
        assert response.get_json()['status'] == 'Not found'

    def test_an_attribute_error_is_not_mapped(self, app):
        """The other half: nothing in the handler recognises it, which is how these
        six became 500s."""
        from app.api.alpha import shared_error_handler

        with app.test_request_context():
            result = shared_error_handler(
                AttributeError("'NoneType' object has no attribute '__table__'"))

        assert result is None or getattr(result, 'status_code', None) != 400


class TestAModelStillWorks:
    """The guards are on the int path only; passing a model must be untouched."""

    def test_post_view(self, env):
        from app.models import Post

        post = db.session.get(Post, env.post1.id)

        assert views.post_view(post, 1)['id'] == post.id

    def test_user_view(self, env):
        assert views.user_view(env.user1, 1)['id'] == env.user1.id

    def test_reply_view(self, env):
        from app.models import PostReply

        reply = db.session.get(PostReply, env.reply1.id)

        assert views.reply_view(reply, 1)['id'] == reply.id

    def test_an_id_that_does_exist(self, env):
        """The int path itself, so the guard is known not to have broken it."""
        assert views.user_view(env.user1.id, 1)['id'] == env.user1.id
        assert views.post_view(env.post1.id, 1)['id'] == env.post1.id
        assert views.reply_view(env.reply1.id, 1)['id'] == env.reply1.id


class TestThePropertyForTheWholeFile:
    """A seventh helper appearing without the guard is what this catches."""

    def test_every_get_on_a_resolved_id_is_followed_by_a_none_check(self):
        import ast
        from pathlib import Path

        source = Path('app/api/alpha/views.py').read_text(encoding='utf8')
        tree = ast.parse(source)

        offenders = []
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            arguments = node.args.args
            if not arguments or not arguments[0].annotation:
                continue
            if 'int' not in ast.unparse(arguments[0].annotation):
                continue
            body = ast.unparse(node)
            if 'db.session.get' not in body:
                continue   # `.one()` raises NoResultFound by itself
            after = body.split('db.session.get', 1)[1][:400]
            if 'is None' not in after or 'NoResultFound' not in after:
                offenders.append(f'{node.name} (line {node.lineno})')

        assert offenders == [], (
            'these resolve an id with db.session.get and do not raise NoResultFound '
            'for a missing row: ' + ', '.join(offenders))
