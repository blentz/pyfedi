"""RBAC parity rows: what pages show to each caller.

The callers, the probes and the world are in tests/rbac_parity.py, which explains the table.
"""
import pytest

from app.utils import set_setting
from tests.rbac_parity import (CID, PID, PROFILE, RID, Row, caller_joins, caller_profile, deleted_post, deleted_reply,
                               inactive_mods, link_domain, profile_extras, remote_community, site_columns, page,
                               redirects_to, shows, shows_as_founder, check, params)

pytestmark = pytest.mark.usefixtures('site')


VISIBILITY = [
    Row('tpl.base.admin_menu', 'DDAAA', shows('/communities', 'admin_menu_parent')),
    Row('tpl.base.admin_content_link', 'DDAAA', shows('/communities', '/admin/content')),
    Row('tpl.profile.admin_edit_link', 'DDAAA', shows(PROFILE, lambda w: f'/admin/user/{w.target.id}/edit')),
    Row('tpl.profile.moderate_user', 'DDAAA', shows(PROFILE, 'Moderate user')),
    Row('tpl.profile.voting_card', 'DDDAA', shows(PROFILE, 'Posts upvoted by')),
    Row('tpl.profile.voting_patterns', 'DDDAA', shows(PROFILE, '/voting_patterns')),
    Row('tpl.profile.same_ip', 'DDAAA', shows(PROFILE, 'Same IP address'), setup=profile_extras),
    Row('tpl.profile.private_mods', 'DDDAA', shows(PROFILE, '<h4>Moderates</h4>'), setup=profile_extras),
    Row('tpl.profile.hidden_subscriptions', 'DDAAA', shows(PROFILE, 'joinedonly'), setup=profile_extras),
    Row('tpl.profile.deleted_post', 'DDAAA', shows('/u/owner', 'the parity post'), setup=deleted_post),
    Row('tpl.post_options.voting_activity', 'DDAAA',
        shows(lambda w: f'/post/{PID(w)}/options_menu', 'View Voting Activity')),
    Row('tpl.reply_options.lock', 'DDAAA', shows(lambda w: f'/post/{PID(w)}/comment/{RID(w)}/options_menu',
                                                  'fe fe-lock')),
    Row('tpl.reply_options.restore', 'DDAAA',
        shows(lambda w: f'/post/{PID(w)}/comment/{RID(w)}/options_menu', 'fe fe-arrow-up'), setup=deleted_reply),
    Row('tpl.side_pane.fixup_from_remote', 'DDDAA',
        shows('/c/farcomm@peer.example', 'Refresh community information from remote'), setup=remote_community),
    Row('tpl.community.un_moderated', 'DDAAA',
        shows('/c/parity', 'Moderators have not been active recently.'), setup=inactive_mods),
    Row('tpl.list_communities.add_remote', 'DDAAA', shows('/communities', '/community/add_remote'),
        setup=lambda ctx: set_setting('allow_default_user_add_remote_community', False)),
    Row('tpl.list_communities.add_local', 'DDDAA', shows('/communities', '/community/add_local'),
        setup=site_columns(community_creation_admin_only=True)),
    Row('tpl.communities_menu.add_remote', 'DDAAA', shows('/communities_menu', '/community/add_remote'),
        setup=lambda ctx: (set_setting('allow_default_user_add_remote_community', False), caller_joins(ctx))),
    Row('tpl.search.add_remote', 'DDAAA', shows('/search', '/community/add_remote'),
        setup=lambda ctx: set_setting('allow_default_user_add_remote_community', False)),
    Row('tpl.modlog.moderator_search', 'DDAAA', shows('/modlog', 'Moderator user@instance')),
    Row('tpl.link_domain.post_warning_form', 'DDAAA',
        shows(lambda w: f'/d/{w.link_domain.id}', 'name="post_warning"'), setup=link_domain),
    Row('community.edit.delete_link', 'DDDAA',
        shows(lambda w: f'/community/{CID(w)}/edit', lambda w: f'/community/community/{CID(w)}/delete')),
    Row('post.lazy_replies.deleted_reply', 'DDAAA',
        shows(lambda w: f'/post/{PID(w)}/lazy_replies/parity', 'the parity reply'), setup=deleted_reply),
    Row('activitypub.private_message.browser', 'DDDAA',
        redirects_to(lambda w: f'/private_message/{w.message.id}', '/chat/')),
    Row('post.continue_discussion.deleted', 'DDAAA',
        page(lambda w: f'/post/{PID(w)}/comment/{RID(w)}'), setup=deleted_reply),
]

# Target rows: the column is the role of the account shown; the founder looks.
TARGET_VISIBILITY = [
    Row('target.profile.admin_badge', '-DDAA', shows_as_founder(caller_profile, '(Admin)')),
    Row('target.profile.role_permissions', '-DAAA', shows_as_founder(caller_profile, 'Role permissions')),
]

ROWS = VISIBILITY + TARGET_VISIBILITY


@pytest.mark.parametrize('row,actor,expected', params(ROWS))
def test_parity(app, db_session, monkeypatch, row, actor, expected):
    check(app, monkeypatch, row, actor, expected)
