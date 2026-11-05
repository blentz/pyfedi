"""RBAC parity rows: community moderation and administration routes, and chat.

The callers, the probes and the world are in tests/rbac_parity.py, which explains the table.
"""
import pytest

from app import db
from app.models import CommunityBan, CommunityFlair, Conversation, Report
from app.utils import set_setting
from tests.rbac_parity import (CID, CONV, Row, ban_target, membership, mod2, recent_accounts, rss_feed, second_owner,
                               site_columns, field_enabled, page, post_effect, post_gate, shows, check, params)

pytestmark = pytest.mark.usefixtures('site')


COMMUNITY = [
    Row('community.add_local.admin_only', 'DDDAA', page('/community/add_local'),
        setup=site_columns(community_creation_admin_only=True)),
    Row('community.add_remote.restricted', 'DDAAA', page('/community/add_remote'),
        setup=lambda ctx: set_setting('allow_default_user_add_remote_community', False)),
    Row('community.add_post.sticky', '-DDAA', field_enabled('/community/parity/submit', 'sticky')),
    Row('community.remove_icon', 'DDDAA', post_gate(lambda w: f'/community/community/{CID(w)}/remove_icon')),
    Row('community.remove_header', 'DDDAA', post_gate(lambda w: f'/community/community/{CID(w)}/remove_header')),
    Row('community.delete', 'DDDAA', page(lambda w: f'/community/community/{CID(w)}/delete')),
    Row('community.mod_list', 'DDDAA', page(lambda w: f'/community/community/{CID(w)}/moderators')),
    Row('community.make_owner', 'DDAAA',
        post_effect(lambda w: f'/community/community/{CID(w)}/make_owner/{w.mod2.id}',
                    lambda w, r: membership(w, w.mod2.id).is_owner), setup=mod2),
    Row('community.remove_owner', 'DDAAA',
        post_effect(lambda w: f'/community/community/{CID(w)}/remove_owner/{w.owner.id}',
                    lambda w, r: not membership(w, w.owner.id).is_owner), setup=second_owner),
    Row('community.find_moderator', 'DDDAA', page(lambda w: f'/community/community/{CID(w)}/moderators/find')),
    Row('community.ban_user', 'DDAAA',
        page(lambda w: f'/community/community/{CID(w)}/{w.target.id}/ban_user_community')),
    Row('community.unban_user', 'DDAAA',
        post_effect(lambda w: f'/community/community/{CID(w)}/{w.target.id}/unban_user_community',
                    lambda w, r: CommunityBan.query.filter_by(user_id=w.target.id).first() is None),
        setup=ban_target),
    Row('community.moderate', 'DDDAA', page('/community/parity/moderate')),
    Row('community.rss_feeds', 'DDDAA', page('/community/parity/rss_feeds')),
    Row('community.rss_feed_new', 'DDDAA', page(lambda w: f'/community/community/{CID(w)}/feed/new'),
        setup=rss_feed),
    Row('community.rss_feed_delete', 'DDDAA',
        page(lambda w: f'/community/community/{CID(w)}/feed/{w.rss.id}/delete'), setup=rss_feed),
    Row('community.moderate_comments', 'DDDAA', page('/community/parity/moderate/comments')),
    Row('community.kick_user', 'DDDAA',
        post_effect(lambda w: f'/community/community/{CID(w)}/{w.target.id}/kick_user_community',
                    lambda w, r: membership(w, w.target.id) is None)),
    Row('community.wiki_list', 'DDDAA', page('/community/parity/moderate/wiki')),
    Row('community.wiki_add', 'DDDDD', page('/community/parity/moderate/wiki/add')),
    Row('community.modlog', 'DDDAA', page('/community/parity/moderate/modlog')),
    Row('community.report_escalate', 'DDDAA',
        page(lambda w: f'/community/community/{CID(w)}/moderate_report/{w.report.id}/escalate')),
    Row('community.report_resolve', 'DDDAA',
        page(lambda w: f'/community/community/{CID(w)}/moderate_report/{w.report.id}/resolve')),
    Row('community.report_ignore', 'DDDAA',
        post_effect(lambda w: f'/community/community/{CID(w)}/moderate_report/{w.report.id}/ignore',
                    lambda w, r: db.session.get(Report, w.report.id).status != 0)),
    Row('community.flair', 'DDDAA', page('/community/parity/moderate/flair')),
    Row('community.flair_edit', 'DDDAA', page(lambda w: f'/community/community/{CID(w)}/flair/{w.flair.id}')),
    Row('community.flair_delete', 'DDDAA',
        post_effect(lambda w: f'/community/community/{CID(w)}/flair/{w.flair.id}/delete',
                    lambda w, r: db.session.get(CommunityFlair, w.flair.id) is None)),
    Row('community.invite.new_account', 'DDDAA', page('/community/parity/invite'), setup=recent_accounts),
]

CHAT = [
    Row('chat.read', 'DDDAA', page(lambda w: f'/chat/{CONV(w)}')),
    Row('chat.options', 'DDDAA', page(lambda w: f'/chat/{CONV(w)}/options')),
    Row('chat.report', 'DDDAA', page(lambda w: f'/chat/{CONV(w)}/report')),
    Row('chat.refresh', 'DDDAA', shows(lambda w: f'/chat/refresh-conversation/{CONV(w)}', 'the parity message')),
    Row('chat.delete', 'DDDAA',
        post_effect(lambda w: f'/chat/{CONV(w)}/delete',
                    lambda w, r: db.session.get(Conversation, w.conversation.id) is None)),
    Row('chat.ban_from_mod', 'DDDAA', page(lambda w: f'/chat/ban_from_mod/{w.target.id}/{CID(w)}')),
]

ROWS = COMMUNITY + CHAT


@pytest.mark.parametrize('row,actor,expected', params(ROWS))
def test_parity(app, db_session, monkeypatch, row, actor, expected):
    check(app, monkeypatch, row, actor, expected)
