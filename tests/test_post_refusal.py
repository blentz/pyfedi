"""Follow-up F2 fix round, item 4: the post page's refusals are decided in one place, `post_refusal`, which `show_post`
and a link to a post both ask, so they cannot drift."""
import pytest
from flask import current_app
from flask_login import login_user

from app import db
from app.constants import POST_STATUS_DRAFT, VISIBILITY_FOLLOWERS
from app.post.util import NEEDS_LOGIN, deleted_post_state, post_refusal
from app.utils import set_setting
from tests.factories import make_user
from tests.test_local_only_invisible import _post, world  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')


def refusal(app, world, change=None, viewer=True, monkeypatch=None, **config):
    post = _post(app, world.clearnet, world.bob)
    if change:
        change(post)
    db.session.commit()
    who = make_user(None, 'carol', local=True) if viewer else None
    with app.test_request_context():
        for key, value in config.items():
            monkeypatch.setitem(current_app.config, key, value)
        if who is not None:
            login_user(who)
        return post_refusal(post), deleted_post_state(post)


def test_an_ordinary_post_is_not_refused(app, world):
    assert refusal(app, world) == (None, 'live')


@pytest.mark.parametrize('change', [lambda p: setattr(p, 'visibility', VISIBILITY_FOLLOWERS),
                                    lambda p: setattr(p, 'status', POST_STATUS_DRAFT),
                                    lambda p: setattr(p, 'deleted', True)])
@pytest.mark.parametrize('viewer', [True, False])
def test_what_the_reader_may_not_see_is_404(app, world, change, viewer):
    assert refusal(app, world, change, viewer)[0] == 404


def test_a_private_community_is_403_to_a_reader_who_is_not_a_member(app, world):
    assert refusal(app, world, lambda p: setattr(p.community, 'private', True))[0] == 403
    assert refusal(app, world, lambda p: setattr(p.community, 'private', True), viewer=False)[0] == 403


def test_a_restricted_country_is_403_to_a_reader_in_it(app, world):
    set_setting('nsfw_country_restriction', 'XX')
    post = _post(app, world.clearnet, world.bob)
    post.nsfw = True
    carol = make_user(None, 'carol', local=True)
    carol.ip_address_country = 'XX'
    db.session.commit()
    with app.test_request_context():
        login_user(carol)
        assert post_refusal(post) == 403
        carol.ip_address_country = 'YY'
        assert post_refusal(post) is None


@pytest.mark.parametrize('warning, kind, outcome', [(False, 'nsfw', NEEDS_LOGIN), (False, 'nsfl', NEEDS_LOGIN),
                                                    (True, 'nsfw', None), (True, 'nsfl', NEEDS_LOGIN)])
def test_an_anonymous_reader_of_adult_content_is_sent_to_log_in(app, world, monkeypatch, warning, kind, outcome):
    result = refusal(app, world, lambda p: setattr(p, kind, True), viewer=False, monkeypatch=monkeypatch,
                     CONTENT_WARNING=warning)
    assert result[0] == outcome
