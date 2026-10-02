from app import db
from app.constants import VISIBILITIES, VISIBILITY_PUBLIC
from tests.factories import make_community, make_instance, make_post, make_post_reply, make_user


def test_constants_are_the_four_activitypub_audiences():
    assert VISIBILITIES == ('public', 'unlisted', 'followers', 'direct')


def test_new_rows_default_to_public(db_session):
    author = make_user(make_instance('m.example'), 'alice')
    post = make_post(make_community(), author, 'https://m.example/p/1')
    reply = make_post_reply(post, author)
    assert post.visibility == VISIBILITY_PUBLIC
    assert reply.visibility == VISIBILITY_PUBLIC


def test_server_default_fills_rows_inserted_without_the_column(db_session):
    author = make_user(make_instance('m.example'), 'alice')
    post = make_post(make_community(), author, 'https://m.example/p/1')
    db.session.execute(db.text("UPDATE post SET visibility = DEFAULT WHERE id = :id"), {'id': post.id})
    db.session.expire_all()
    assert db.session.get(type(post), post.id).visibility == 'public'
