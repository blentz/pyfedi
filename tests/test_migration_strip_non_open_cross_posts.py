"""Migration e5c1d8a2b7f9 (ruling R-XPOSTCLEAN): followers-only posts are taken out of every cross-post list.

The function is imported from the revision itself and run against seeded rows, so this tests what ships."""
import importlib.util
import pathlib

import pytest

from app import db
from app.constants import VISIBILITY_FOLLOWERS, VISIBILITY_UNLISTED
from app.models import Post
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')

_REVISION = pathlib.Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / \
    'e5c1d8a2b7f9_strip_non_open_cross_posts.py'


def _revision():
    spec = importlib.util.spec_from_file_location('strip_cross_posts_revision', _REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run():
    _revision().strip_non_open_cross_posts(db.session.connection())
    db.session.commit()
    db.session.expire_all()


@pytest.fixture
def posts(db_session):
    """a, b (public), u (unlisted) and f (followers-only) all share one link and were all cross-referenced, as
    calculate_cross_posts used to do. Reply counts 1, 2, 4, 8; x is an unrelated public pair, untouched."""
    local = make_instance('test.piefed.local', software='piefed')
    author = make_user(local, 'founder', local=True)
    community = make_community('general')
    rows = {}
    for n, (name, replies) in enumerate([('a', 1), ('b', 2), ('u', 4), ('f', 8), ('x', 16), ('y', 32)], start=1):
        rows[name] = make_post(community, author, f'https://test.piefed.local/post/{n}', title=name)
        rows[name].reply_count = replies
    rows['u'].visibility = VISIBILITY_UNLISTED
    rows['f'].visibility = VISIBILITY_FOLLOWERS
    db.session.commit()
    group = ['a', 'b', 'u', 'f']
    for name in group:
        rows[name].cross_posts = [rows[o].id for o in group if o != name]
        rows[name].reply_count_cross_posted = 15
    rows['x'].cross_posts, rows['y'].cross_posts = [rows['y'].id], [rows['x'].id]
    rows['x'].reply_count_cross_posted = rows['y'].reply_count_cross_posted = 999   # stale on purpose: untouched
    db.session.commit()
    return {name: post.id for name, post in rows.items()}


def _get(ids, name):
    return db.session.get(Post, ids[name])


def test_open_posts_lose_the_followers_only_id_and_keep_the_rest(posts):
    _run()
    assert sorted(_get(posts, 'a').cross_posts) == sorted([posts['b'], posts['u']])
    assert sorted(_get(posts, 'b').cross_posts) == sorted([posts['a'], posts['u']])
    assert sorted(_get(posts, 'u').cross_posts) == sorted([posts['a'], posts['b']])


def test_the_followers_only_post_has_no_cross_posts_and_counts_only_its_own_replies(posts):
    _run()
    f = _get(posts, 'f')
    assert f.cross_posts is None
    assert f.reply_count_cross_posted == 8


def test_affected_posts_have_their_cross_posted_reply_count_recomputed(posts):
    _run()
    assert [_get(posts, n).reply_count_cross_posted for n in ('a', 'b', 'u')] == [7, 7, 7]


def test_a_list_left_empty_counts_only_the_posts_own_replies(posts):
    a, f = _get(posts, 'a'), _get(posts, 'f')
    a.cross_posts = [f.id]
    db.session.commit()
    _run()
    a = _get(posts, 'a')
    assert a.cross_posts == []
    assert a.reply_count_cross_posted == 1


def test_a_null_reply_count_counts_as_zero(posts):
    """PostReply.new's sum() skips NULLs; the recount must not turn the whole total NULL."""
    a = _get(posts, 'a')
    a.reply_count = None
    db.session.commit()
    _run()
    assert _get(posts, 'a').reply_count_cross_posted == 6     # 0 + b 2 + u 4
    assert _get(posts, 'b').reply_count_cross_posted == 6     # 2 + a NULL + u 4


def test_a_post_listing_its_own_id_counts_its_replies_once(posts):
    a = _get(posts, 'a')
    a.cross_posts = [a.id, posts['b'], posts['f']]
    db.session.commit()
    _run()
    a = _get(posts, 'a')
    assert sorted(a.cross_posts) == sorted([posts['a'], posts['b']])
    assert a.reply_count_cross_posted == 3     # 1 + b 2, not 1 + 1 + 2


def test_posts_whose_lists_name_no_followers_only_post_are_left_alone(posts):
    _run()
    x, y = _get(posts, 'x'), _get(posts, 'y')
    assert (x.cross_posts, x.reply_count_cross_posted) == ([posts['y']], 999)
    assert (y.cross_posts, y.reply_count_cross_posted) == ([posts['x']], 999)


def test_running_it_twice_changes_nothing_more(posts):
    _run()
    _run()
    assert _get(posts, 'a').reply_count_cross_posted == 7
    assert _get(posts, 'f').cross_posts is None


def test_the_revision_follows_the_current_head_and_downgrade_is_a_no_op():
    module = _revision()
    assert (module.revision, module.down_revision) == ('e5c1d8a2b7f9', 'c4a8e2f61d3b')
    assert module.downgrade() is None
