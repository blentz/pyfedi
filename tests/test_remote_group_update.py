"""A moderator's edit of a remote community is not sent as a Group Update (plan C3, task 3 fix round 2).

A remote Group is its home server's. Our copy of it is a snapshot that may be stale (title, summary, sensitive,
icon, our flair ids...), so an Update naming the real Group would make its home apply our copy and undo its own
moderators' changes. Before C3 the Update named a URL of ours, which the home ignored; now nothing is sent at all
(controller ruling). The edit still changes our copy. A local community's edit still sends its Group Update.

Every trigger is run: the web flair add, edit and delete, the API's three flair calls, the web settings form and
the API's community edit. Each runs the real edit_community task, with send_post_request recorded."""
import io
from unittest.mock import patch

import pytest

import app.shared.tasks.groups as groups_module
from app import db
from app.api.alpha.utils.community import post_community_flair_create, post_community_flair_delete, \
    put_community_flair_edit
from app.constants import SRC_API
from app.models import Community, CommunityFlair
from app.shared.community import edit_community
from tests.factories import a_keypair, make_community_member, make_instance, make_user
from tests.test_community_kind_rules import _remote, url, web  # noqa: F401  (fixture)
from tests.test_local_only_outbound import wire  # noqa: F401  (fixture)
from tests.test_shared_community_lifecycle import _api_input

PEER_INBOX = 'https://peer.example/inbox'
REMOTE_INBOX = 'https://remote.example/c/far/inbox'


@pytest.fixture
def scene(web):
    """`general` (local) and `far` (remote, with a stored inbox), both moderated by the founder (an admin), and a
    follower of `general` on a peer, so a local edit has somewhere to go."""
    web.founder.private_key, web.founder.public_key = a_keypair()
    general = web.community
    general.private_key, general.public_key = a_keypair()
    far = _remote()
    far.ap_inbox_url = REMOTE_INBOX
    make_community_member(web.founder, far, is_moderator=True)
    peer = make_instance('peer.example', software='lemmy')
    peer.inbox = PEER_INBOX
    make_community_member(make_user(peer, 'fan'), general)
    db.session.commit()
    return web, general, far


@pytest.fixture
def sent(monkeypatch):
    """What the Group Update task asked to send: (inbox, activity)."""
    calls = []
    monkeypatch.setattr(groups_module, 'send_post_request',
                        lambda inbox, body, *args, **kwargs: calls.append((inbox, body)))
    return calls


def _flair(community, text='old'):
    flair = CommunityFlair(community_id=community.id, flair=text, text_color='#000000', background_color='#ffffff')
    db.session.add(flair)
    db.session.commit()
    return flair


def _flair_form(web, community, flair_id, text):
    with patch('app.community.routes.render_template', return_value='rendered'):
        return web.client.post(url(web.app, 'community.community_flair_edit', community_id=community.id,
                                   flair_id=flair_id),
                               data={'flair': text, 'text_color': '#111111', 'background_color': '#eeeeee',
                                     'submit': 'Save', 'csrf_token': web.token})


def web_flair_add(web, community):
    _flair_form(web, community, 0, 'new')
    return lambda: CommunityFlair.query.filter_by(community_id=community.id, flair='new').count() == 1


def web_flair_edit(web, community):
    flair = _flair(community)
    _flair_form(web, community, flair.id, 'renamed')
    return lambda: db.session.get(CommunityFlair, flair.id).flair == 'renamed'


def web_flair_delete(web, community):
    flair_id = _flair(community).id
    web.client.post(url(web.app, 'community.community_flair_delete', community_id=community.id, flair_id=flair_id),
                    data={'csrf_token': web.token})
    return lambda: db.session.get(CommunityFlair, flair_id) is None


def _as_api_user(web):
    return patch('app.api.alpha.utils.community.authorise_api_user', return_value=web.founder)


def api_flair_create(web, community):
    with _as_api_user(web):
        post_community_flair_create('token', {'community_id': community.id, 'flair_title': 'new'})
    return lambda: CommunityFlair.query.filter_by(community_id=community.id, flair='new').count() == 1


def api_flair_edit(web, community):
    flair = _flair(community)
    with _as_api_user(web):
        put_community_flair_edit('token', {'flair_id': flair.id, 'flair_title': 'renamed'})
    return lambda: db.session.get(CommunityFlair, flair.id).flair == 'renamed'


def api_flair_delete(web, community):
    flair_id = _flair(community).id
    with _as_api_user(web):
        post_community_flair_delete('token', {'flair_id': flair_id})
    return lambda: db.session.get(CommunityFlair, flair_id) is None


def web_settings_form(web, community):
    payload = {'csrf_token': web.token, 'icon_file': (io.BytesIO(b''), ''), 'banner_file': (io.BytesIO(b''), ''),
               'title': 'Renamed here', 'description': '', 'theme': 'disabled', 'posting_warning': '',
               'invitations': '0', 'languages': str(web.english.id), 'default_layout': '', 'default_post_type': 'link',
               'downvote_accept_mode': '0', 'post_url_type': 'friendly', 'submit': 'Save'}
    with patch('app.community.routes.render_template', return_value='rendered'):
        web.client.post(url(web.app, 'community.community_edit', community_id=community.id), data=payload,
                        content_type='multipart/form-data')
    return lambda: db.session.get(Community, community.id).title == 'Renamed here'


def api_community_edit(web, community):
    with patch('app.shared.community.authorise_api_user', return_value=web.founder):
        edit_community(_api_input(title='Renamed here', discussion_languages=[web.english.id]), community, SRC_API,
                       'token')
    return lambda: db.session.get(Community, community.id).title == 'Renamed here'


TRIGGERS = [web_flair_add, web_flair_edit, web_flair_delete, api_flair_create, api_flair_edit, api_flair_delete,
            web_settings_form, api_community_edit]


@pytest.mark.parametrize('trigger', TRIGGERS, ids=lambda trigger: trigger.__name__)
def test_an_edit_of_a_remote_community_is_kept_here_and_sent_nowhere(scene, sent, wire, trigger):
    web, _general, far = scene
    changed = trigger(web, far)
    db.session.expire_all()
    assert changed()   # our copy changes as it always did
    assert sent == []
    assert wire.calls == []   # and nothing else left the server either


@pytest.mark.parametrize('trigger', TRIGGERS, ids=lambda trigger: trigger.__name__)
def test_an_edit_of_a_local_community_still_sends_its_group_update(scene, sent, wire, trigger):
    web, general, _far = scene
    changed = trigger(web, general)
    db.session.expire_all()
    assert changed()
    [(inbox, announce)] = sent
    assert inbox == PEER_INBOX
    assert (announce['type'], announce['object']['type'], announce['object']['object']['type']) == \
        ('Announce', 'Update', 'Group')
    assert announce['object']['object']['id'] == general.ap_profile_id
