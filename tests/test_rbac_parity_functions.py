"""RBAC parity rows: functions, shared helpers and API handlers.

The callers, the probes and the world are in tests/rbac_parity.py, which explains the table.
"""
import pytest

from app import db
from app.api.alpha.utils.community import (get_community_moderate_bans, post_community_flair_create,
                                           post_community_flair_delete, post_community_moderate_ban,
                                           post_community_moderate_post_nsfw, put_community_flair_edit,
                                           put_community_moderate_unban)
from app.api.alpha.utils.feed import put_feed
from app.api.alpha.utils.misc import _feed_open_to, get_modlog
from app.api.alpha.utils.post import get_post_like_list, post_post_feature, put_post_set_flair
from app.api.alpha.utils.reply import get_reply_like_list
from app.api.alpha.utils.user import post_user_ban
from app.api.alpha.views import feed_view, user_view
from app.constants import SRC_API
from app.models import CommunityBan, Feed, ModLog, Post, Site
from app.post.util import deleted_post_state, post_refusal, post_replies
from app.referral import GENERATORS_SETTING, MAX_PER_USER_SETTING, may_generate, remaining_signups
from app.shared.community import add_mod_to_community, delete_community, remove_mod_from_community
from app.shared.feed import make_feed
from app.shared.user import block_another_user
from app.utils import add_to_modlog, can_create_post_reply, can_moderate, set_setting, upload_access_allows
from tests.factories import bearer
from tests.rbac_parity import (Row, ban_target, low_reply, mod2, modlog_entry, quietly, recent_accounts, recent_owner,
                               reviewing_post, staff2, call, refuses, check, params)

pytestmark = pytest.mark.usefixtures('site')


FUNCTIONS = [
    Row('fn.referral.may_generate.staff_setting', '-DAAA', call(lambda w, u: may_generate(u)),
        setup=lambda ctx: set_setting(GENERATORS_SETTING, 'staff')),
    Row('fn.referral.may_generate.admins_setting', '-DDAA', call(lambda w, u: may_generate(u))),
    Row('fn.referral.remaining_signups.exempt', '-DDAA',
        call(lambda w, u: remaining_signups(u) == (None, None)),
        setup=lambda ctx: set_setting(MAX_PER_USER_SETTING, 1)),
    Row('fn.user.trustworthy.new_account', '-DDAA', call(lambda w, u: u.trustworthy()), setup=recent_accounts),
    Row('fn.user.can_send_pm_to.new_sender', '-DAAA', call(lambda w, u: u.can_send_pm_to(w.target)),
        setup=recent_accounts),
    Row('target.user.can_send_pm_to.new_sender_writes_to', '-DAAA', call(lambda w, u: w.owner.can_send_pm_to(u)),
        setup=recent_owner),
    Row('fn.wiki.can_edit.mods_only', '-DAAA', call(lambda w, u: w.wiki.can_edit(u, w.community))),
    Row('fn.can_moderate', '-DAAA', call(lambda w, u: can_moderate(w.community, u))),
    Row('fn.can_create_post_reply.banned_from_community', '-DDAA',
        call(lambda w, u: can_create_post_reply(u, w.community)),
        setup=lambda ctx: ctx.world.actors[ctx.actor] and (
            db.session.add(CommunityBan(user_id=ctx.world.actors[ctx.actor].id, community_id=ctx.world.community.id,
                                        banned_by=ctx.world.owner.id, reason='parity')), db.session.commit())),
    Row('fn.upload_access_allows.admins', '-DAAA', call(lambda w, u: upload_access_allows('parity_uploads', u)),
        setup=lambda ctx: set_setting('parity_uploads', 'admins')),
    Row('fn.add_to_modlog.admin_type', '-DAAA',
        call(lambda w, u: (add_to_modlog('ban_user', actor=u, target_user=w.target),
                           ModLog.query.filter_by(user_id=u.id).one().type == 'admin')[1])),
    Row('fn.deleted_post_state.staff', '-DAAA', call(lambda w, u: deleted_post_state(w.post, u) == 'staff'),
        setup=lambda ctx: (setattr(ctx.world.post, 'deleted', True),
                           setattr(ctx.world.post, 'deleted_by', ctx.world.target.id), db.session.commit())),
    Row('fn.post_refusal.unpublished', '-DAAA', call(lambda w, u: post_refusal(w.post, u) is None),
        setup=reviewing_post),
    Row('fn.post_replies.hide_threshold', '-DAAA',
        call(lambda w, u: w.reply.id in [r['comment'].id for r in post_replies(w.post, 'hot', u)]),
        setup=low_reply),
    Row('fn.site.admins', '-DDAA', call(lambda w, u: u.id in [a.id for a in Site.admins()])),
    # Site.staff() went in step 8; the list it fed (/about, the API's site admins) is the listed roles' holders
    Row('fn.site.staff', '-DADD', call(lambda w, u: u.id in [a.id for a in Site.listed_role_holders()])),
    Row('target.api.user_view.is_admin', '-DDAA', call(lambda w, u: user_view(u, variant=2)['is_admin'])),
    Row('fn.feed_open_to.private', '-DDAA', call(lambda w, u: _feed_open_to(w.feed, u.id))),
    Row('fn.shared.delete_community', '-DDAA',
        refuses(lambda w, u: delete_community(w.community.id, SRC_API, bearer(u)), 'incorrect_login')),
    Row('fn.shared.add_mod', '-DAAA',
        refuses(lambda w, u: add_mod_to_community(w.community.id, w.target.id, SRC_API, bearer(u)),
                'no_permission')),
    Row('fn.shared.remove_mod', '-DAAA',
        refuses(lambda w, u: remove_mod_from_community(w.community.id, w.mod2.id, SRC_API, bearer(u)),
                'incorrect_login'), setup=mod2),
    Row('fn.shared.make_feed.instance_feed', '-DDAA',
        refuses(lambda w, u: make_feed(dict(title='Parity instance', url='parityinstance', description='',
                                            icon_url=None, banner_url=None, nsfw=False, nsfl=False, public=True,
                                            is_instance_feed=True, show_child_posts=False, parent_feed_id=None,
                                            communities=''), SRC_API, bearer(u)),
                'is_instance_feed requires an admin account')),
    Row('target.shared.block_another_user', '-ADDD',
        refuses(lambda w, u: block_another_user(u.id, SRC_API, bearer(w.owner)), 'cannot_block_admin_or_staff')),
]

API = [
    Row('api.community.bans', '-DAAA',
        refuses(lambda w, u: get_community_moderate_bans(bearer(u), {'community_id': w.community.id}),
                'incorrect_login')),
    Row('api.community.unban', '-DAAA',
        refuses(lambda w, u: put_community_moderate_unban(bearer(u), {'community_id': w.community.id,
                                                                       'user_id': w.target.id}),
                'incorrect_login'), setup=ban_target),
    Row('api.community.ban', '-DAAA',
        refuses(lambda w, u: post_community_moderate_ban(bearer(u), {'community_id': w.community.id,
                                                                      'user_id': w.target.id, 'reason': 'parity'}),
                'incorrect_login')),
    Row('api.community.post_nsfw', '-DAAA',
        refuses(lambda w, u: post_community_moderate_post_nsfw(bearer(u), {'post_id': w.post.id, 'nsfw_status': True}),
                'incorrect_login')),
    Row('api.community.flair_create', '-DAAA',
        refuses(lambda w, u: post_community_flair_create(bearer(u), {'community_id': w.community.id,
                                                                      'flair_title': 'new parity flair'}),
                'insufficient permissions')),
    Row('api.community.flair_edit', '-DAAA',
        refuses(lambda w, u: put_community_flair_edit(bearer(u), {'flair_id': w.flair.id, 'flair_title': 'edited'}),
                'insufficient permissions')),
    Row('api.community.flair_delete', '-DAAA',
        refuses(lambda w, u: post_community_flair_delete(bearer(u), {'flair_id': w.flair.id}),
                'insufficient permissions')),
    Row('api.post.feature_local', '-DDAA',
        call(lambda w, u: (quietly(post_post_feature, bearer(u), {'post_id': w.post.id, 'featured': True,
                                                                 'feature_type': 'Local'}),
                           db.session.get(Post, w.post.id).instance_sticky)[1])),
    Row('api.post.like_list', '-DAAA',
        refuses(lambda w, u: get_post_like_list(bearer(u), {'post_id': w.post.id}), 'Not a moderator')),
    Row('api.post.set_flair', '-DAAA',
        refuses(lambda w, u: put_post_set_flair(bearer(u), {'post_id': w.post.id, 'flair_id_list': [w.flair.id]}),
                'Insufficient permissions')),
    Row('api.reply.like_list', '-DAAA',
        refuses(lambda w, u: get_reply_like_list(bearer(u), {'comment_id': w.reply.id}), 'Not a moderator')),
    # A public feed: an admin may edit someone else's (D697). This row was the world's private feed until O5 (owner
    # ruling 2026-11-03) made that the owner's alone, admins included -- a row nobody passes, so it is pinned in
    # tests/test_api_feeds.py (test_an_admin_may_not_edit_somebody_elses_private_feed) instead.
    Row('api.feed.put.public', '-DDAA',
        call(lambda w, u: (quietly(put_feed, bearer(u), {'feed_id': w.feed.id, 'title': 'edited'}),
                           db.session.get(Feed, w.feed.id).title == 'edited')[1]),
        setup=lambda ctx: (setattr(ctx.world.feed, 'public', True), db.session.commit())),
    Row('api.feed.view_private', '-DDAA',
        refuses(lambda w, u: feed_view(w.feed, variant=1, user_id=u.id, subscribed=[], include_communities=False,
                                       communities_moderating=[], banned_from=[], communities_joined=[],
                                       blocked_community_ids=[], blocked_instance_ids=[]),
                'insufficient permissions')),
    Row('api.modlog.private_entries', '-DAAA',
        call(lambda w, u: 'the parity entry' in str(get_modlog(bearer(u), {}))), setup=modlog_entry),
    Row('target.api.user_ban.by_staff', '-AADD',
        refuses(lambda w, u: post_user_ban(bearer(w.staff2), {'person_id': u.id, 'reason': 'parity'}),
                'cannot_ban_admin'), setup=staff2),
]

ROWS = FUNCTIONS + API


@pytest.mark.parametrize('row,actor,expected', params(ROWS))
def test_parity(app, db_session, monkeypatch, row, actor, expected):
    check(app, monkeypatch, row, actor, expected)
