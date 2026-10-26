"""The urls the AI-detection admin notifications store go by account id, and an old `/u/<name>` url is resolved when
followed (follow-up F1, fix round 1).

`notify_admin(.., f'/u/{user.link()}', ..)` stored a name that is bare on the storefront the post came over, so an admin
following it on the other domain reached a namesake."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import current_app

from app import db
from app.constants import NOTIF_REPORT
from app.local_names import notification_href
from app.models import Notification, PostReply
from app.utils import utcnow
from tests.conftest import forget_login
from tests.factories import make_post
from tests.test_models_post_new_branches import env as post_env  # noqa: F401  (the fixture)
from tests.test_models_post_new_branches import new
from tests.test_models_post_reply_new import a_reply
from tests.test_models_post_reply_new import env as reply_env  # noqa: F401  (the fixture)
from tests.two_domains import default_request, onion_request, two_domains  # noqa: F401  (the fixture)

pytestmark = pytest.mark.usefixtures('site')

THREE_AI = [json.dumps({'detection': 'ai'})] * 3


def verdict():
    return MagicMock(status_code=200, json=lambda: {'confidence': 0.9, 'detection_result': 'ai'})


def test_an_em_dash_report_stores_the_accounts_id(reply_env):
    reply_env.replier.created = utcnow()
    db.session.commit()
    with patch('app.utils.notify_admin') as notify:
        a_reply(reply_env, body='a reply — with an em dash')
    assert notify.call_args.args[1] == f'/user/{reply_env.replier.id}'


def test_the_ban_after_ai_replies_stores_the_accounts_id(reply_env, monkeypatch):
    monkeypatch.setitem(current_app.config, 'DETECT_AI_ENDPOINT', 'https://ai.example/check')
    reply_env.replier.created = utcnow()
    db.session.commit()
    with patch('app.models.get_request', return_value=verdict(), create=True), \
            patch('app.utils.get_request', return_value=verdict()), \
            patch('app.models.recent_ai_detections', return_value=THREE_AI), \
            patch('app.utils.notify_admin') as notify:
        a_reply(reply_env, body='x' * 300)
    assert notify.call_args.args[1] == f'/user/{reply_env.replier.id}'


def test_the_ban_after_ai_posts_stores_the_accounts_id(post_env, monkeypatch):
    monkeypatch.setitem(current_app.config, 'DETECT_AI_ENDPOINT', 'https://ai.example/check')
    post_env.author.created = utcnow()
    db.session.commit()
    with patch('app.utils.get_request', return_value=verdict()), \
            patch('app.models.recent_ai_detections', return_value=THREE_AI), \
            patch('app.utils.notify_admin') as notify:
        new(post_env, content='x' * 300)
    assert notify.call_args.args[1] == f'/user/{post_env.author.id}'


@pytest.fixture
def bobs(two_domains):
    first, onion = two_domains.domains
    return SimpleNamespace(first=two_domains.local_user('bob', first), onion=two_domains.local_user('bob', onion))


def row(url, targets=None):
    return Notification(title='t', url=url, notif_type=NOTIF_REPORT, targets=targets)


def test_an_old_bare_url_resolves_to_the_first_domains_account(app, bobs):
    with onion_request(app):
        assert notification_href(row('/u/bob')) == f'/user/{bobs.first.id}'


def test_an_old_url_resolves_by_the_suspect_id_in_its_targets_first(app, bobs):
    with default_request(app):
        assert notification_href(row('/u/bob', {'suspect_user_id': bobs.onion.id})) == f'/user/{bobs.onion.id}'


def test_an_old_url_with_a_domain_resolves_to_that_domains_account(app, bobs):
    with default_request(app):
        assert notification_href(row('/u/bob@abc.onion')) == f'/user/{bobs.onion.id}'


def test_an_old_url_that_names_nothing_has_no_href(app, bobs):
    with default_request(app):
        assert notification_href(row('/u/ghost')) is None


def test_an_admin_following_the_onion_bobs_notification_reaches_the_onion_bob(app, bobs):
    admin = bobs.first
    notification = Notification(title='t', url=f'/user/{bobs.onion.id}', user_id=admin.id, notif_type=NOTIF_REPORT)
    db.session.add(notification)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction(base_url='http://abc.onion') as session:
        session['_user_id'] = str(admin.id)
        session['_fresh'] = True
    response = client.get(f'/notification/{notification.id}/goto', base_url='http://abc.onion')
    assert response.headers['Location'].endswith(f'/user/{bobs.onion.id}')
    # follow-up F6: off the onion it is followed nowhere, as its page would answer, with no Location to name it
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(admin.id)
        session['_fresh'] = True
    forget_login()
    response = client.get(f'/notification/{notification.id}/goto')
    assert response.status_code == 404 and 'Location' not in response.headers
