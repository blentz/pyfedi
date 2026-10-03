from sqlalchemy import text

from app import db
from app.models import Post, PostReply
from app.visibility import (
    RestrictedReply,
    can_view,
    is_open,
    listable_clause,
    listable_sql,
    mark_restricted,
    visible_to_clause,
    visible_to_sql,
)
from tests.factories import make_visibility_world


def test_open_audiences_are_visible_to_everyone(db_session):
    w = make_visibility_world()
    for value in ('public', 'unlisted'):
        w.post.visibility = value
        assert can_view(w.post, None)
        assert can_view(w.post, w.stranger.id)


def test_followers_only_rules(db_session):
    w = make_visibility_world()
    assert can_view(w.post, w.follower.id)
    assert can_view(w.post, w.author.id)
    assert not can_view(w.post, w.stranger.id)
    assert not can_view(w.post, None)


def test_pending_follow_does_not_unlock(db_session):
    """Review focus 1."""
    w = make_visibility_world()
    assert not can_view(w.post, w.pending.id)


def test_direct_and_unknown_values_are_never_visible(db_session):
    """Review focus 4."""
    w = make_visibility_world()
    for value in ('direct', 'bogus'):
        w.post.visibility = value
        assert not can_view(w.post, w.author.id + 1000)


def test_unknown_value_denies_the_author_and_an_accepted_follower(db_session):
    """Only `followers` unlocks for the author or a follower; a value nothing knows about is shut to everyone."""
    w = make_visibility_world()
    for obj in (w.post, w.reply):
        for value in ('direct', 'bogus', ''):
            obj.visibility = value
            for viewer in (None, w.author.id, w.follower.id, w.pending.id, w.stranger.id):
                assert not can_view(obj, viewer), (type(obj).__name__, value, viewer)


def test_null_counts_as_public(db_session):
    w = make_visibility_world()
    w.post.visibility = None
    assert can_view(w.post, None)


def test_clause_matches_python_predicate(db_session):
    w = make_visibility_world()
    for viewer in (None, w.follower.id, w.pending.id, w.stranger.id, w.author.id):
        ids = {p.id for p in Post.query.filter(visible_to_clause(Post, viewer))}
        assert (w.post.id in ids) == can_view(w.post, viewer), viewer


def test_clause_matches_python_predicate_for_posts_and_replies_and_every_value(db_session):
    w = make_visibility_world()
    for model, obj in ((Post, w.post), (PostReply, w.reply)):
        for value in ('followers', 'public', 'unlisted', 'direct', 'bogus'):
            obj.visibility = value
            db.session.flush()
            for viewer in (None, w.follower.id, w.pending.id, w.stranger.id, w.author.id):
                ids = {o.id for o in model.query.filter(visible_to_clause(model, viewer))}
                assert (obj.id in ids) == can_view(obj, viewer), (model.__name__, value, viewer)


def test_sql_matches_python_predicate(db_session):
    w = make_visibility_world()
    query = text(f"SELECT p.id FROM post p WHERE {visible_to_sql('p')}")
    for viewer in (None, w.follower.id, w.pending.id, w.stranger.id, w.author.id):
        ids = {row[0] for row in db.session.execute(query, {'visibility_viewer_id': viewer})}
        for post in (w.post, w.public_post):
            assert (post.id in ids) == can_view(post, viewer), viewer


def test_sql_treats_null_visibility_as_public(db_session):
    # post.visibility is NOT NULL, so feed the predicate a derived table that yields NULL.
    w = make_visibility_world()
    query = text("SELECT p.id FROM (SELECT id, user_id, CAST(NULL AS VARCHAR) AS visibility FROM post) p "
                 f"WHERE {visible_to_sql('p')}")
    ids = {row[0] for row in db.session.execute(query, {'visibility_viewer_id': None})}
    assert w.post.id in ids


def test_listable_clause_only_public(db_session):
    w = make_visibility_world()
    ids = {p.id for p in Post.query.filter(listable_clause(Post))}
    assert w.public_post.id in ids
    assert w.post.id not in ids


def test_listable_sql_shape():
    assert listable_sql('p') == "p.visibility = 'public'"


def test_mark_restricted_keeps_children(db_session):
    w = make_visibility_world()
    tree = [{'comment': w.reply, 'replies': [{'comment': w.public_child, 'replies': []}]}]
    marked = mark_restricted(tree, w.stranger.id)
    assert isinstance(marked[0]['comment'], RestrictedReply)
    assert marked[0]['restricted'] is True
    assert marked[0]['replies'][0]['comment'] is w.public_child
    assert not marked[0]['replies'][0].get('restricted')


def test_is_open_is_true_for_public_unlisted_null_and_things_with_no_visibility(db_session):
    w = make_visibility_world()
    for value, expected in (('public', True), ('unlisted', True), (None, True), ('followers', False),
                            ('direct', False), ('bogus', False)):
        w.post.visibility = value
        assert is_open(w.post) is expected, value
    assert is_open(w.author) and is_open(w.community)
