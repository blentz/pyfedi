"""Post and PostReply keep the ids of the Create and Announce that delivered them. PeerTube's announce ids are
longer than 100 characters (https://peertube.linuxrocks.online/videos/watch/<uuid>/announces/116674 is 101), and a
String(100) made every such video fail to insert, which on hell.cloud stopped a channel's backfill (interop D24)."""
import pytest
import sqlalchemy as sa

from app import db
from app.models import Post, PostReply


@pytest.mark.parametrize('table', ['post', 'post_reply'])
@pytest.mark.parametrize('column', ['ap_create_id', 'ap_announce_id'])
def test_the_activity_id_columns_hold_255_characters(app, db_session, table, column):
    columns = {c['name']: c for c in sa.inspect(db.engine).get_columns(table)}

    assert columns[column]['type'].length == 255


@pytest.mark.parametrize('model', [Post, PostReply])
def test_the_models_agree(model):
    assert model.__table__.c.ap_create_id.type.length == 255
    assert model.__table__.c.ap_announce_id.type.length == 255
