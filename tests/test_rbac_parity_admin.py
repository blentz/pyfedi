"""RBAC parity rows: the admin blueprint's routes.

The callers, the probes and the world are in tests/rbac_parity.py, which explains the table.
"""
import pytest

from tests.rbac_parity import (Row, gate, post_gate, check, params)

pytestmark = pytest.mark.usefixtures('site')


ADMIN = [
    # admin_home: is_admin_or_staff (admin/routes.py 103)
    Row('admin.home', 'DDAAA', gate('/admin/')),
    # @permission_required('change instance settings')
    *[Row(f'admin.settings{path.replace("/", ".")}', 'DDDAA', gate(f'/admin{path}')) for path in (
        '/site', '/domains', '/misc', '/instance_chooser', '/federation', '/activities', '/newsletter',
        '/instances', '/emoji', '/emoji/add')],
    Row('admin.settings.perf_test', 'DDDAA', post_gate('/admin/perf_test?n=1')),
    Row('admin.settings.activity_json', 'DDDAA', gate(lambda w: f'/admin/activity_json/{w.activity.id}')),
    Row('admin.settings.emoji_edit', 'DDDAA', gate(lambda w: f'/admin/emoji/{w.emoji.id}/edit')),
    Row('admin.settings.masquerade', 'DDDAA', gate(lambda w: f'/admin/masquerade/{w.target.id}')),
    # admin_domain_branding: in-body user_access('change instance settings') + abort(403)
    Row('admin.domain_branding', 'DDDAA', gate(lambda w: f'/admin/domains/{w.shop.id}/branding')),
    # @permission_required('administer all communities'); since step 4, the permission split from it for each page
    *[Row(f'admin.communities{path.replace("/", ".")}', 'DDAAA', gate(f'/admin{path}')) for path in (
        '/communities', '/communities/no-topic', '/communities/low-quality', '/communities/un-moderated',
        '/topics', '/topic/add', '/content', '/blocked_images', '/blocked_image/add', '/media', '/votes',
        '/votes_for_noone', '/serial_downvoters', '/high_posts_per_day', '/no_link_diversity')],
    Row('admin.communities.community_edit', 'DDAAA',
        gate(lambda w: f'/admin/community/{w.community.id}/edit')),
    Row('admin.communities.topic_edit', 'DDAAA', gate(lambda w: f'/admin/topic/{w.topic.id}/edit')),
    Row('admin.communities.instance_edit', 'DDAAA', gate(lambda w: f'/admin/instance/{w.peer.id}/edit')),
    Row('admin.communities.blocked_image_edit', 'DDAAA',
        gate(lambda w: f'/admin/blocked_image/{w.blocked_image.id}/edit')),
    # @permission_required('administer all users')
    *[Row(f'admin.users{path.replace("/", ".")}', 'DDDAA', gate(f'/admin{path}')) for path in (
        '/users', '/referrals', '/reports', '/users/add')],
    Row('admin.users.user_edit', 'DDDAA', gate(lambda w: f'/admin/user/{w.target.id}/edit')),
    # @permission_required('approve registrations')
    Row('admin.registrations', 'DDAAA', gate('/admin/approve_registrations')),
    # @permission_required('edit cms pages')
    Row('admin.pages', 'DDDAA', gate('/admin/pages')),
    Row('admin.pages.edit', 'DDDAA', gate(lambda w: f'/admin/pages/{w.page.id}/edit')),
    # Admin only (RB5): it was @permission_required('change user roles'); /admin/roles replaced it in step 7
    Row('admin.permissions', 'DDDAA', gate('/admin/roles')),
]

ROWS = ADMIN


@pytest.mark.parametrize('row,actor,expected', params(ROWS))
def test_parity(app, db_session, monkeypatch, row, actor, expected):
    check(app, monkeypatch, row, actor, expected)
