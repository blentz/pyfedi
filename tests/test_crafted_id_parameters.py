"""D1395: four more query-string ids that reached the database unparsed.

The campaign has recorded this shape three times -- D1311 (`/communities?topic_id=`
was `int()` bare, and a `<select>` sends the empty string when nothing is chosen),
D1313 (`int(request.args.get('new_feed_id'))` on two feed routes) and D1389
(`int(request.form.get('community_id'))` behind a `!= ''` test, which only rules
out absent and empty). Four sites still had it, three of them on pages that take
no login:

    /tags/posts/<id>?topic_id=abc   db.session.get(Topic, 'abc')
    /tags/posts/<id>?feed_id=abc    db.session.get(Feed, 'abc')
    /feed/new?topic_id=abc          db.session.get(Topic, 'abc')
    /modlog?communities=abc         int('abc')

Measured before the repair:

    PROBE  TOPIC_ID 'abc'     ProgrammingError  LINE 3: WHERE topic.id = 'abc'
                              InvalidTextRepresentation: invalid input syntax
                              for type integer: "abc"
           TOPIC_ID '1.5'     the same
           TOPIC_ID 'null'    the same
           FEED_ID  'abc'     the same, WHERE feed.id = 'abc'
           MODLOG  'abc'      ValueError: invalid literal for int() with base 10

A 500 out of the database driver, not a refusal: the caller learns the column
type and the failing statement, and each request leaves a rolled-back session
behind.

WHAT IS NEW HERE, and why the sweep was worth repeating. `tests/test_tag_lists.py`
already records these two as repaired. Its P4 says "community_id reached `int()`,
and topic_id and feed_id reached `.get()` followed by an attribute access -- three
crafted parameters, three 500s". That round gave `community_id` its `type=int` and
gave the other two an `or abort(404)` -- which answers the id that names **no**
topic, a different failure with the same symptom. The id that is not an id never
reached the `or`. A repair that fixes one of two failure modes and a docstring
that claims both is worse than no docstring: the next reader stops looking.

A 24-DIGIT ID IS NOT THE SAME PROBLEM, and was checked rather than assumed.
`Topic.id` is a four-byte `integer`, so `?topic_id=999999999999999999999999`
looked like it would overflow past any parse guard. It does not:

    PROBE  db.session.get(Topic, 10**24)           None
           GET /post/<24 digits>                   404
           GET /community/<24 digits>/block        404
           GET /api/alpha/post?id=<24 digits>      400

SQLAlchemy binds a Python int as a parameter and Postgres compares it without
coercing it into the column's type, so a number too large for the column simply
matches no row. The earlier `InvalidTextRepresentation` came from the **string**,
not from its size. So `type=int` is the whole fix, and the `<int:...>` path
converters elsewhere in the codebase need nothing.

FACT 322 applies to the modlog rows: one request per test.
"""
import pytest

from app import db
from app.models import Site, Tag, Topic
from tests.factories import (make_community, make_instance, make_local_feed,
                             make_post, make_user)

pytestmark = pytest.mark.usefixtures('site')
HOST = 'test.piefed.local'

# Every value a select, a stale link or a crafted request can put in one of these
# parameters and that is not an id. '' is the one a `<select>` with nothing chosen
# sends, and the reason D1311 exists.
NOT_IDS = ['abc', '1.5', 'null', 'None', '-', '0x1', '1e3', '',
           ' 1 ', '1;drop', '%00']

# A number, so `type=int` accepts it and the walrus is truthy: this is a
# well-formed id that names nothing, and the routes with an `or abort(404)`
# answer 404 for it rather than falling through. Kept out of NOT_IDS because
# putting it there is what made three rows of this file wrong first time round.
TOO_LARGE_FOR_THE_COLUMN = '999999999999999999999999'


@pytest.fixture
def env(app, db_session):
    """A topic, a feed and a tag, each with a post, so the branches under repair
    have something to find when the parameter IS an id."""
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    instance = make_instance(HOST, software='piefed')
    make_user(instance, 'founder', local=True)
    alice = make_user(instance, 'alice', local=True)
    alice.verified = True
    alice.private_key = 'a-key'
    topic = Topic(name='Fediverse', machine_name='fediverse', num_communities=1,
                  show_posts_in_children=False)
    db.session.add(topic)
    tag = Tag(name='solarstorm', display_as='solarstorm', banned=False,
              post_count=0)
    db.session.add(tag)
    db.session.commit()
    community = make_community('general')
    community.topic_id = topic.id
    feed = make_local_feed('newsfeed', public=True)
    db.session.commit()
    from app.models import FeedItem, post_tag
    db.session.add(FeedItem(feed_id=feed.id, community_id=community.id))
    post = make_post(community, alice, f'https://{HOST}/p/1', title='TAGGEDPOST')
    db.session.execute(post_tag.insert().values(post_id=post.id, tag_id=tag.id))
    db.session.commit()
    from types import SimpleNamespace
    return SimpleNamespace(alice=alice, topic=topic, tag=tag, feed=feed,
                           community=community)


def _logged_in(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    return client


# --------------------------------------------------------------------------
# /tags/posts/<tag_id> -- the two the earlier round named and half repaired
# --------------------------------------------------------------------------


class TestTheTagPostList:
    @pytest.mark.parametrize('value', NOT_IDS)
    def test_a_topic_id_that_is_not_one_is_not_a_500(self, app, env, value):
        from unittest.mock import patch
        with patch('app.tag.routes.flask') as fl:
            fl.render_template.return_value = 'rendered'
            response = app.test_client().get(
                f'/tags/posts/{env.tag.id}?topic_id={value}')
        assert response.status_code == 200

    @pytest.mark.parametrize('value', NOT_IDS)
    def test_a_feed_id_that_is_not_one_is_not_a_500(self, app, env, value):
        from unittest.mock import patch
        with patch('app.tag.routes.flask') as fl:
            fl.render_template.return_value = 'rendered'
            response = app.test_client().get(
                f'/tags/posts/{env.tag.id}?feed_id={value}')
        assert response.status_code == 200

    @pytest.mark.parametrize('parameter', ['topic_id', 'feed_id'])
    def test_an_unparsable_id_filters_nothing_rather_than_everything(
            self, app, env, parameter):
        """Which way it falls matters. `type=int` makes the walrus falsy, so the
        branch is skipped and the caller gets the unfiltered list -- the answer
        an absent parameter gets. A repair that instead left the filter in place
        with an empty id list would also stop the 500 and would silently answer
        'no posts' to a stale link.
        """
        from unittest.mock import patch
        with patch('app.tag.routes.flask') as fl:
            fl.render_template.return_value = 'rendered'
            app.test_client().get(f'/tags/posts/{env.tag.id}?{parameter}=abc')
        titles = [post.title
                  for post in fl.render_template.call_args.kwargs['posts']]
        assert titles == ['TAGGEDPOST']

    @pytest.mark.parametrize('parameter,attribute', [('topic_id', 'topic'),
                                                     ('feed_id', 'feed')])
    def test_an_id_that_is_one_still_filters(self, app, env, parameter,
                                             attribute):
        """The control for every row above. A fix that dropped both branches
        would pass all of them."""
        from unittest.mock import patch
        target = getattr(env, attribute)
        with patch('app.tag.routes.flask') as fl:
            fl.render_template.return_value = 'rendered'
            response = app.test_client().get(
                f'/tags/posts/{env.tag.id}?{parameter}={target.id}')
        assert response.status_code == 200
        titles = [post.title
                  for post in fl.render_template.call_args.kwargs['posts']]
        assert titles == ['TAGGEDPOST']

    @pytest.mark.parametrize('parameter', ['topic_id', 'feed_id'])
    @pytest.mark.parametrize('value', ['999999', TOO_LARGE_FOR_THE_COLUMN])
    def test_an_id_naming_nothing_is_still_a_404(self, app, env, parameter,
                                                value):
        """The `or abort(404)` the earlier round added is the OTHER failure and
        must survive this repair: a well-formed id for a row nobody holds is a
        404, not an unfiltered page.

        The 24-digit value belongs here and not among the unparsable ones. It IS
        a number, so `type=int` accepts it; what makes it name nothing is that no
        row has it.
        """
        from unittest.mock import patch
        with patch('app.tag.routes.flask') as fl:
            fl.render_template.return_value = 'rendered'
            response = app.test_client().get(
                f'/tags/posts/{env.tag.id}?{parameter}={value}')
        assert response.status_code == 404

    @pytest.mark.parametrize('parameter', ['topic_id', 'feed_id'])
    def test_zero_is_treated_as_absent(self, app, env, parameter):
        """`0` is falsy after conversion, so it skips the branch rather than
        asking for row 0. `community_id` on the same route has behaved this way
        since it gained `type=int`; this records that the two now agree."""
        from unittest.mock import patch
        with patch('app.tag.routes.flask') as fl:
            fl.render_template.return_value = 'rendered'
            response = app.test_client().get(
                f'/tags/posts/{env.tag.id}?{parameter}=0')
        assert response.status_code == 200


# --------------------------------------------------------------------------
# /feed/new?topic_id= -- 'create a feed from this topic'
# --------------------------------------------------------------------------


class TestCreatingAFeedFromATopic:
    @pytest.mark.parametrize('value', NOT_IDS)
    def test_a_topic_id_that_is_not_one_is_not_a_500(self, app, env, value):
        from unittest.mock import patch
        client = _logged_in(app, env.alice)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            response = client.get(f'/feed/new?topic_id={value}')
        assert response.status_code == 200

    def test_a_topic_that_exists_still_prefills_the_form(self, app, env):
        """The control, and the feature: the page exists to turn a topic into a
        feed, so the title, the url and the community list must come from it."""
        from unittest.mock import patch
        client = _logged_in(app, env.alice)
        captured = {}

        def fake_render(template, **kwargs):
            captured.update(kwargs)
            return 'rendered'

        with patch('app.feed.routes.render_template', side_effect=fake_render):
            response = client.get(f'/feed/new?topic_id={env.topic.id}')

        assert response.status_code == 200
        form = captured['form']
        assert form.title.data == 'Fediverse'
        assert form.url.data == 'fediverse'
        assert 'general' in form.communities.data

    @pytest.mark.parametrize('value', ['999999', TOO_LARGE_FOR_THE_COLUMN])
    def test_a_topic_id_naming_nothing_is_a_404(self, app, env, value):
        from unittest.mock import patch
        client = _logged_in(app, env.alice)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            response = client.get(f'/feed/new?topic_id={value}')
        assert response.status_code == 404

    def test_the_parameter_is_read_once(self, app, env):
        """It used to be read twice -- `if request.args.get('topic_id'):` and
        then again inside the `get()`. Two reads of one parameter is how a guard
        and the value it guards come to disagree, and there is no reason for it
        here."""
        import ast
        import inspect
        from app.feed.routes import feed_new
        tree = ast.parse(inspect.getsource(feed_new).lstrip())
        reads = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and node.func.attr == 'get'
                 and any(isinstance(a, ast.Constant) and a.value == 'topic_id'
                         for a in node.args)]
        assert len(reads) == 1


# --------------------------------------------------------------------------
# /modlog?communities= -- D1389's shape verbatim, on a public page
# --------------------------------------------------------------------------


class TestTheModlogCommunityFilter:
    @pytest.mark.parametrize('value', NOT_IDS + [TOO_LARGE_FOR_THE_COLUMN])
    def test_a_community_id_that_is_not_one_is_not_a_500(self, app, env, value):
        """`int(community_id) if community_id != '' else 0` -- the `!= ''` only
        ruled out the one value a select sends, and the modlog takes no login."""
        response = app.test_client().get(f'/modlog?communities={value}')
        assert response.status_code == 200

    def test_a_community_that_exists_still_filters(self, app, env):
        """The control. `community_id` is passed to the template as well as to
        the query, so the filter has to survive the conversion."""
        from unittest.mock import patch
        captured = {}

        def fake_render(template, **kwargs):
            captured.update(kwargs)
            return 'rendered'

        with patch('app.main.routes.render_template', side_effect=fake_render):
            response = app.test_client().get(
                f'/modlog?communities={env.community.id}')

        assert response.status_code == 200
        assert captured['community_id'] == env.community.id

    def test_the_empty_string_still_means_no_filter(self, app, env):
        """The value the `!= ''` test existed for, kept working: `type=int`
        answers the default for it rather than raising."""
        from unittest.mock import patch
        captured = {}

        def fake_render(template, **kwargs):
            captured.update(kwargs)
            return 'rendered'

        with patch('app.main.routes.render_template', side_effect=fake_render):
            app.test_client().get('/modlog?communities=')

        assert captured['community_id'] == 0

    def test_an_unparsable_id_means_no_filter_rather_than_no_entries(
            self, app, env):
        from unittest.mock import patch
        captured = {}

        def fake_render(template, **kwargs):
            captured.update(kwargs)
            return 'rendered'

        with patch('app.main.routes.render_template', side_effect=fake_render):
            app.test_client().get('/modlog?communities=abc')

        assert captured['community_id'] == 0


# --------------------------------------------------------------------------
# The round's residual
# --------------------------------------------------------------------------


def test_the_feed_branchs_own_404_can_no_longer_be_observed(app, env):
    """THE ROUND'S RESIDUAL, recorded rather than worked around.

    `tag_posts`' feed branch is

        feed = db.session.get(Feed, feed_id) or abort(404)
        if not feed_readable_by(feed, ...):
            abort(404)

    and `feed_readable_by(None, ...)` is False, so D1395's mutant
    `M6 tag_posts feed_id loses its 404` SURVIVED: deleting the `or abort(404)`
    leaves the same 404, produced one line later by D1394's visibility guard.
    Fact 75 CAUSE 9 -- a guard that cannot discriminate -- arrived here by a
    second guard being added in front of the same answer, and no behavioural test
    can tell the two apart.

    Both stay. The `or abort(404)` says what it means where a reader looks for
    it, and a visibility helper is the wrong place to learn that a row exists.
    `show_tag` and `tag_cloud` carry the same pair for the same reason; `feed_new`
    has only the one, and its mutant dies.

    Asserted here is the fact that makes the mutant unkillable, so the next
    reader does not spend the round rediscovering it.
    """
    from app.utils import feed_readable_by
    assert feed_readable_by(None, env.alice.id) is False


# --------------------------------------------------------------------------
# The measurement the repair rests on
# --------------------------------------------------------------------------


def test_an_id_too_large_for_the_column_matches_no_row(app, env):
    """Checked rather than assumed, because it decides whether `type=int` is
    enough. `Topic.id` is a four-byte `integer` and this is a 24-digit number,
    so it looked like it would overflow past any parse guard on its way to
    Postgres. SQLAlchemy binds it as a parameter and the comparison simply
    matches nothing -- no `NumericValueOutOfRange`, no rollback. Three rows of
    this file asserted 200 for it before this was measured; they assert 404 now,
    beside the ordinary id that names nothing.

    The earlier failures came from the STRING reaching the column, not from its
    magnitude, which is why `<int:...>` path converters elsewhere need no
    bound either.
    """
    assert db.session.get(Topic, 10 ** 24) is None
    assert app.test_client().get(f'/post/{10 ** 24}').status_code == 404
