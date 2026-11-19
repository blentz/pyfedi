"""Fixtures from tests/test_onion_hidden_api_ids.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import db
from app.models import ChatMessage, Report
from tests.factories import (
    COMMUNITY_ADMINISTRATION,
    grant_permission,
    grant_permissions,
)


@pytest.fixture
def onion_hidden_api_ids_alpha(app, onion_hidden_on_clearnet_onion, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    grant_permission(onion_hidden_on_clearnet_onion.admin, 'ban users')
    grant_permissions(onion_hidden_on_clearnet_onion.admin, *COMMUNITY_ADMINISTRATION)
    grant_permission(onion_hidden_on_clearnet_onion.admin, 'administer all users')
    return onion_hidden_on_clearnet_onion


@pytest.fixture
def reports(onion_in_reply, talking):
    onion = onion_in_reply
    message = ChatMessage.query.filter_by(conversation_id=onion.with_onion.id).first()
    rows = [Report(reasons='spam', description='REPORTED', type=1, reporter_id=onion.first_bob.id, status=0,
                   suspect_post_id=onion.in_first.id, in_community_id=onion.first_art.id, source_instance_id=1,
                   suspect_user_id=onion.onion_bob.id, targets={'suspect_post_id': onion.in_first.id}),
            Report(reasons='spam', description='REPORTED', type=2, reporter_id=onion.first_bob.id, status=0,
                   suspect_post_id=onion.first_post.id, suspect_post_reply_id=onion.onion_in_reply.id,
                   in_community_id=onion.first_art.id, source_instance_id=1, suspect_user_id=onion.onion_bob.id,
                   targets={'suspect_comment_id': onion.onion_in_reply.id}),
            Report(reasons='spam', description='REPORTED', type=4, reporter_id=onion.first_bob.id, status=0,
                   suspect_conversation_id=onion.with_onion.id, source_instance_id=1, suspect_user_id=onion.onion_bob.id,
                   targets={'suspect_message_id': message.id}),
            Report(reasons='spam', description='CONTROL', type=1, reporter_id=onion.first_bob.id, status=0,
                   suspect_post_id=onion.first_post.id, in_community_id=onion.first_art.id, source_instance_id=1,
                   suspect_user_id=onion.first_bob.id, targets={'suspect_post_id': onion.first_post.id})]
    db.session.add_all(rows)
    db.session.commit()
    onion.reports = rows
    return onion
