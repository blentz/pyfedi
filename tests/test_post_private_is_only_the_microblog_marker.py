"""What `Post.private` means, pinned.

Two changes rest on this: the community listing and the community RSS feed stopped
filtering `Post.private == False`, and the aggregate-feed gate moved onto the
community disjunct. Both are safe only if `Post.private` marks microblogs rather
than non-public content. That claim has three parts, and each has a test here:

1. `Post.new()` (app/models.py:1834) is the ONLY writer of the column, and it
   sets it inside `if 'name' not in request_json['object']:` -- so a titled post is
   never private.
2. `Post.new()` is reachable only through `create_post()`
   (app/activitypub/util.py ~2496), which refuses `followers` and `direct`
   object-level visibility first (pinned by tests/test_visibility_ingest.py). So
   the flag can never be the thing keeping non-public content out of a listing --
   non-public content is never stored as a Post at all.
3. Within microblogs the flag tracks ACTIVITY-level addressing, which is a
   different and less reliable source of truth than the object-level check in (2):
   a genuinely unlisted post comes out private=False, and a public post wrapped in
   a synthesised activity with no addressing comes out private=True. It is
   therefore not usable as a privacy signal in either direction.

`PostReply.private` (app/models.py:2866) is a different column with a different
meaning -- it really does mean followers-only. Nothing here applies to it.
"""

import ast
import inspect
import pathlib
import re

import pytest

from app.activitypub.util import create_post
from app.models import Post
from tests.factories import make_community, make_instance, make_site, make_user

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
FOLLOWERS = 'https://m.example/users/alice/followers'


@pytest.fixture
def ingest(db_session):
    """An author, a community and the Site row Post.new() -> blocked_phrases() needs."""
    author = make_user(make_instance('m.example'), 'alice')
    community = make_community()
    make_site()
    return author, community


def create_activity(obj_extra, activity_to=None, activity_cc=None):
    """A Create wrapping a public object. Activity-level addressing is optional.

    Omitting it reproduces create_resolved_object (app/activitypub/util.py ~4105),
    which synthesises `{'id': ..., 'object': post_data}` with no addressing of its
    own -- the shape ingested boosts and resolved URIs arrive in.
    """
    activity = {
        'id': 'https://m.example/users/alice/statuses/1/activity',
        'type': 'Create',
        'object': {
            'id': 'https://m.example/users/alice/statuses/1',
            'type': 'Note',
            'content': '<p>hello world, at some length so a title can be derived</p>',
            'attributedTo': 'https://m.example/users/alice',
            'to': [PUBLIC],
            'cc': [FOLLOWERS],
            **obj_extra,
        },
    }
    if activity_to is not None:
        activity['to'] = activity_to
    if activity_cc is not None:
        activity['cc'] = activity_cc
    return activity


def test_a_titled_post_is_never_private(ingest):
    """The column is written only inside Post.new()'s titleless branch."""
    author, community = ingest

    post = create_post(False, community, create_activity({'name': 'A titled article'}), author)

    assert post is not None
    assert post.private is False
    assert post.microblog is False


def test_a_titleless_post_is_stored_private_and_microblog(ingest):
    """The shape the owner sees in the live database: private=True on a public Note."""
    author, community = ingest

    post = create_post(False, community, create_activity({}), author)

    assert post is not None
    assert post.private is True
    assert post.microblog is True


def test_activity_level_public_addressing_clears_the_flag_on_the_same_object(ingest):
    """Same object, same audience -- only the wrapper differs, and the flag flips.

    This is what disqualifies `private` as a privacy signal: it is not a property of
    the content's audience at all, it is a property of how the delivering activity
    happened to be addressed.
    """
    author, community = ingest

    post = create_post(False, community, create_activity({}, activity_to=[PUBLIC]), author)

    assert post is not None
    assert post.private is False
    assert post.microblog is True


def test_every_stored_private_post_is_a_microblog(ingest):
    """The set relation, asserted over a mixed batch rather than one row at a time."""
    author, community = ingest
    for i, obj_extra in enumerate([{}, {'name': 'titled one'}, {}, {'name': 'titled two'}]):
        activity = create_activity(obj_extra)
        activity['id'] = f'https://m.example/users/alice/statuses/{i}/activity'
        activity['object']['id'] = f'https://m.example/users/alice/statuses/{i}'
        create_post(False, community, activity, author)

    assert Post.query.count() == 4
    assert Post.query.filter(Post.private == True).count() == 2  # noqa: E712 - SQLAlchemy column comparison
    assert Post.query.filter(Post.private == True, Post.microblog == False).count() == 0  # noqa: E712


def test_post_new_is_reached_only_through_create_post():
    """A source scan, because the safety argument is about paths, not one call.

    `create_post` refuses followers-only and direct objects before calling
    `Post.new` (tests/test_visibility_ingest.py). That refusal is what keeps
    non-public content out of the database, and it only covers everything for as
    long as `create_post` is the sole route to `Post.new`. A new caller elsewhere in
    app/ would reintroduce exactly the exposure the removed listing filters were
    wrongly credited with preventing, and nothing else in the suite would notice.
    """
    create_post_file = pathlib.Path(create_post.__code__.co_filename)
    app_dir = create_post_file.parent.parent
    body, first_line = inspect.getsourcelines(create_post)
    create_post_lines = range(first_line, first_line + len(body))

    # Parsed, not grepped: `Post.new()` appears in prose in three docstrings and
    # comments (app/activitypub/util.py's activitypub_visibility, app/models.py's
    # ScheduledPost), and a scan that counted those could never reach 1.
    callers = []
    for path in sorted(app_dir.rglob('*.py')):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'new' and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == 'Post'):
                callers.append((path, node.lineno))

    assert len(callers) == 1, f'expected one Post.new() call site in app/, found {callers}'
    path, lineno = callers[0]
    assert path == create_post_file and lineno in create_post_lines, (
        f'Post.new() is called from {path}:{lineno}, which is outside create_post(). '
        'Any caller must perform the same activitypub_visibility refusal create_post '
        'does before calling Post.new.'
    )


def test_the_visibility_refusal_still_guards_create_post():
    """The refusal named above is still in create_post's own body, not merely nearby."""
    source = inspect.getsource(create_post)
    normalized = re.sub(r'\s+', ' ', source)

    assert 'activitypub_visibility(request_json.get(\'object\'))' in normalized
    assert "if visibility == 'direct'" in normalized
    assert normalized.index("if visibility == 'direct'") < normalized.index('Post.new(')
