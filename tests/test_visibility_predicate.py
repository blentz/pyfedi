from app import db
from app.models import Post, PostReply
from app.visibility import (can_view, listable_sql, mark_restricted, visible_to_clause,
                            RestrictedReply)
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


def test_null_counts_as_public(db_session):
    w = make_visibility_world()
    w.post.visibility = None
    assert can_view(w.post, None)


def test_clause_matches_python_predicate(db_session):
    w = make_visibility_world()
    for viewer in (None, w.follower.id, w.pending.id, w.stranger.id, w.author.id):
        ids = {p.id for p in Post.query.filter(visible_to_clause(Post, viewer))}
        assert (w.post.id in ids) == can_view(w.post, viewer), viewer


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
