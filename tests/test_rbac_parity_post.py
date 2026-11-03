"""RBAC parity rows: post and reply routes, user bans, feeds and a few pages.

The callers, the probes and the world are in tests/rbac_parity.py, which explains the table.
"""
import pytest

from app import db
from app.constants import POST_STATUS_PUBLISHED
from app.models import Post, PostReply, User
from tests.rbac_parity import (HX, PID, RID, Row, author_is_caller, deleted_post, deleted_reply, reviewing_post,
                               site_columns, field_enabled, page, post_effect, shows, check, params)

pytestmark = pytest.mark.usefixtures('site')


POST = [
    Row('post.show.distinguished', '-DAAA', field_enabled(lambda w: f'/post/{PID(w)}', 'distinguished')),
    Row('post.show.deleted_reply', 'DDAAA', shows(lambda w: f'/post/{PID(w)}', 'the parity reply'),
        setup=deleted_reply),
    Row('post.embed.deleted', 'DDAAA', page(lambda w: f'/post/{PID(w)}/embed'), setup=deleted_post),
    Row('post.source.deleted', 'DDDAA', shows(lambda w: f'/post/{PID(w)}/source/show', '<pre><code>', HX),
        setup=lambda ctx: (deleted_post(ctx), setattr(ctx.world.post, 'body', 'parity body'), db.session.commit())),
    Row('post.edit.sticky', '-DDAA', field_enabled(lambda w: f'/post/{PID(w)}/edit', 'sticky'),
        setup=lambda ctx: author_is_caller(ctx, 'post')),
    Row('post.purge', 'DDDAA',
        post_effect(lambda w: f'/post/{PID(w)}/purge', lambda w, r: db.session.get(Post, w.post.id) is None),
        setup=deleted_post),
    Row('post.sticky', 'DDAAA',
        post_effect(lambda w: f'/post/{PID(w)}/sticky/yes', lambda w, r: db.session.get(Post, w.post.id).sticky)),
    Row('post.instance_sticky', 'DDDAA',
        post_effect(lambda w: f'/post/{PID(w)}/instance_sticky/yes',
                    lambda w, r: db.session.get(Post, w.post.id).instance_sticky)),
    Row('post.set_flair', 'DDAAA', page(lambda w: f'/post/{PID(w)}/set_flair')),
    Row('post.flair_list', 'DDAAA', page(lambda w: f'/post/{PID(w)}/get_flair')),
    Row('post.approve', 'DDAAA',
        post_effect(lambda w: f'/post/{PID(w)}/approve',
                    lambda w, r: db.session.get(Post, w.post.id).status == POST_STATUS_PUBLISHED),
        setup=reviewing_post),
    Row('post.reply_source.deleted', 'DDDAA',
        shows(lambda w: f'/post/{PID(w)}/comment/{RID(w)}/source/show', '<pre><code>', HX), setup=deleted_reply),
    Row('post.reply_edit_inline.distinguished', '-DAAA',
        field_enabled(lambda w: f'/post/{PID(w)}/comment/{RID(w)}/edit_inline', 'distinguished', HX),
        setup=lambda ctx: author_is_caller(ctx, 'reply')),
    Row('post.reply_edit.distinguished', '-DAAA',
        field_enabled(lambda w: f'/post/{PID(w)}/comment/{RID(w)}/edit', 'distinguished'),
        setup=lambda ctx: author_is_caller(ctx, 'reply')),
    Row('post.reply_delete.form', 'DDAAA', page(lambda w: f'/post/{PID(w)}/comment/{RID(w)}/delete')),
    Row('post.reply_delete.done', 'DDDAA',
        post_effect(lambda w: f'/post/{PID(w)}/comment/{RID(w)}/delete',
                    lambda w, r: db.session.get(PostReply, w.reply.id).deleted, data={'reason': 'parity'})),
    Row('post.reply_purge', 'DDAAA',
        post_effect(lambda w: f'/post/{PID(w)}/comment/{RID(w)}/purge',
                    lambda w, r: db.session.get(PostReply, w.reply.id) is None), setup=deleted_reply),
    Row('post.voting_activity', 'DDAAA', page(lambda w: f'/post/{PID(w)}/voting_activity')),
    Row('post.reply_voting_activity', 'DDAAA', page(lambda w: f'/comment/{RID(w)}/voting_activity')),
    Row('post.set_ai', 'DDAAA',
        post_effect(lambda w: f'/post/{PID(w)}/set_ai', lambda w, r: db.session.get(Post, w.post.id).ai_generated)),
]

USER = [
    Row('user.ban', 'DDAAA', page('/u/target/ban')),
    Row('user.unban', 'DDAAA',
        post_effect('/u/target/unban', lambda w, r: not db.session.get(User, w.target.id).banned),
        setup=lambda ctx: (setattr(ctx.world.target, 'banned', True), db.session.commit())),
    Row('user.delete', 'DDAAA',
        post_effect('/u/target/delete', lambda w, r: db.session.get(User, w.target.id).deleted)),
]

FEED = [
    Row('feed.new.instance_feed', '-DDAA', field_enabled('/feed/new', 'is_instance_feed')),
    Row('feed.edit', 'DDDAA', page(lambda w: f'/feed/{w.feed.id}/edit')),
    Row('feed.copy.instance_feed', '-DDAA', field_enabled(lambda w: f'/feed/{w.feed.id}/copy', 'is_instance_feed'),
        setup=lambda ctx: (setattr(ctx.world.feed, 'public', True), db.session.commit())),
]

MISC = [
    Row('main.add_post.can_create', '-DDAA', shows('/add_post', '/community/add_local'),
        setup=site_columns(community_creation_admin_only=True)),
    Row('instance.people.unsearchable', 'DDDAA', shows('/instance/peer.example/people', 'remy'),
        setup=lambda ctx: (setattr(ctx.world.remote, 'searchable', False), db.session.commit())),
]

ROWS = POST + USER + FEED + MISC


@pytest.mark.parametrize('row,actor,expected', params(ROWS))
def test_parity(app, db_session, monkeypatch, row, actor, expected):
    check(app, monkeypatch, row, actor, expected)
