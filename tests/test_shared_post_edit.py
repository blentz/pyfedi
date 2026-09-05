"""`edit_post`'s source dispatch and its suspicious-domain block.

`app/shared/post.py:250-754` is the LOCAL post editor. Its federated twin,
`update_post_from_activity`, was taken to zero uncovered statements by
sub-project 17; two of that sub-project's findings (D287 and D292) are
statements about how the two copies disagree, and both were found by reading
this function without ever executing it. This file executes it.

SCOPE. Two clusters, 175 uncovered when this file was started:

  - the source dispatch, :251-383 -- SRC_API 76, SRC_WEB 61. Its lower bound is
    the function's own comment at :384, "beyond this point do not use the input
    variable as it can be either a dict or a form object!". Note that :388 and
    :390 read `input.sticky.data` and `input.nsfl.data` BELOW that marker,
    behind the conditional expression `False if src == SRC_API else ...` -- so
    the marker describes an intent the code does not quite keep, and the SRC_WEB
    double below carries `sticky` and `nsfl` for that reason.
  - the domain and notify block, :565-598 -- 38. This is the local copy of
    `app/activitypub/util.py:3508-3545`.

ENTRY is a direct call. `edit_post` opens with `if not user:` on BOTH branches
(:252 for SRC_API, :316 for SRC_WEB), so passing `user=` skips
`authorise_api_user` and `current_user` alike. No request context, no login, no
auth token is needed anywhere in this file.

THE HEAD REQUEST. `is_image_url` (app/utils.py:247) issues an httpx HEAD through
`mime_type_using_head`, which catches only `httpx.HTTPError` and
`httpx.InvalidURL`. respx's unmatched-request error is neither, so it escapes
into the test. `edit_post` can call `is_image_url` twice -- once at :410 on
`post.url`, once at :601 on the new `url` -- so every seeded post here has
`url=None`, leaving exactly one HEAD to register.

THE IMAGE BOUNDARY. Under this harness `make_image_sizes`
(app/activitypub/util.py:1724) EXECUTES rather than enqueues: Celery is eager
(tests/conftest.py:106). The technique used here is a bodiless 404 on the
source url. `make_image_sizes_async:1748` retries only when
'/api/v3/image_proxy' is in the url, and :1759 proceeds only on status 200;
neither holds, so it returns having done nothing. The OTHER technique in this
suite -- turning off `cache_remote_images_locally` -- does NOT work here: that
setting gates only the Event block's call in app/activitypub/util.py, and
:614/:616 call `make_image_sizes` directly.

`http_mock` is `assert_all_called=True` (tests/conftest.py:288-295), so each
test registers exactly the routes its own path reaches -- the crash tests below
register the HEAD only, because they raise at :607 before the GET.

WHICH COMMIT RAISES. `:459 db.session.commit()` runs only inside
`if not from_scratch:` (:421). Tests that need a durable pre-notify commit
therefore pass `from_scratch=False` and change the url, which is also what sets
`url_changed = True` at :436 and opens the :565 gate.
"""

import pytest
from datetime import datetime

from app import db
from app.constants import (
    NOTIF_REPORT, POST_TYPE_ARTICLE, POST_TYPE_EVENT, POST_TYPE_IMAGE,
    POST_TYPE_LINK, POST_TYPE_POLL, POST_TYPE_VIDEO, SRC_API, SRC_WEB,
)
from app.constants import ROLE_ADMIN
from app.models import Event, File, Notification, Poll, PollChoice, Role
from app.shared.post import edit_post
from tests.factories import (
    make_community, make_community_flair, make_community_member, make_domain,
    make_instance, make_post, make_user,
)

from types import SimpleNamespace


_OMIT = object()
"""Sentinel: a key mapped to this is left OUT of the built input entirely.

Needed because absence and falsity are different branches here. :265
`if 'tags' in input:` and :340 `hasattr(input, 'image_alt_text')` test
presence; :263 `if image_alt_text is None:` and :333 `if input.flair:` test
value. A builder that could only set values could not reach half the arms.
"""


class _Field:
    """One WTForms field. edit_post's SRC_WEB branch reads only `.data`."""

    def __init__(self, data):
        self.data = data


def _web_form(**over):
    """The SRC_WEB shape, as a plain object carrying `_Field` attributes.

    Defaults are a complete POST_TYPE_LINK submission. `over` replaces:
      value        -> wrapped in _Field
      None         -> the ATTRIBUTE is set to None, so `if input.flair:` (:333),
                      `if input.finish_in:` (:354) and `input.image_alt_text`
                      (:340) take their false arms
      _OMIT        -> the attribute is not set at all, so `hasattr` (:340) is
                      False

    `sticky` and `nsfl` are present because :388 and :390 read them below the
    :384 marker.
    """
    fields = {
        'title': 'a title', 'body': 'a body',
        'link_url': 'https://example.com/page', 'video_url': 'https://example.com/v.mp4',
        'nsfw': False, 'ai_generated': False, 'notify_author': True,
        'language_id': None, 'tags': '', 'flair': '',
        'scheduled_for': None, 'repeat': None, 'timezone': 'UTC',
        'image_alt_text': '', 'sticky': False, 'nsfl': False,
        'mode': 'single', 'local_only': False, 'finish_in': '3d',
        'event_timezone': 'UTC',
        'start_datetime': datetime(2030, 1, 1, 9, 0), 'end_datetime': datetime(2030, 1, 1, 10, 0),
        'max_attendees': 10, 'online': False, 'online_link': '',
        'join_mode': 'free', 'irl_address': '1 Road', 'irl_city': 'Town', 'irl_country': 'Nowhere',
    }
    for i in range(1, 16):
        fields[f'choice_{i}'] = ''
    fields.update(over)

    form = SimpleNamespace()
    for name, value in fields.items():
        if value is _OMIT:
            continue
        setattr(form, name, None if value is None else _Field(value))
    return form


def _api_input(**over):
    """The SRC_API shape: a plain dict. `_OMIT` removes a key."""
    data = {
        'title': 'a title', 'body': 'a body', 'url': None,
        'nsfw': False, 'ai_generated': False, 'notify_author': True,
        'language_id': None,
    }
    data.update(over)
    return {k: v for k, v in data.items() if v is not _OMIT}


def _seed(url=None, domain_name=None, notify_mods=False, notify_admins=False):
    """instance/user/community/post, and optionally a Domain with notify flags.

    The post is seeded with url=None on purpose: :403 `if post.url:` guards a
    second `is_image_url` call at :410, and every extra call is another HEAD
    route this file would have to register.

    make_community hardcodes instance_id=1 and user_id=1, and
    tests/conftest.py:143 truncates with RESTART IDENTITY, so the first
    make_instance here is id 1 and the first make_user is id 1.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'editor', local=True)
    community = make_community('c1')
    post = make_post(community, user, ap_id='https://test.piefed.local/post/1')
    post.url = url
    db.session.commit()

    domain = None
    if domain_name:
        domain = make_domain(domain_name)
        domain.notify_mods = notify_mods
        domain.notify_admins = notify_admins
        db.session.commit()

    return SimpleNamespace(instance=instance, user=user, community=community,
                           post=post, domain=domain)


def _make_admin(user):
    """Make `user` an admin as `Site.admins()` counts them.

    `Site.admins()` (app/models.py) takes its JOIN arm here:
    tests/conftest.py clears `flask.g` before every test and nothing in this
    file sets `admin_ids`, so `hasattr(g, 'admin_ids')` is False.

    A ROLE ROW IS REQUIRED EVEN FOR USER 1. `.join(user_role)` is an INNER join,
    so a user with no row there is dropped before the `or_` is evaluated --
    `User.id == 1` cannot rescue a user the join already eliminated. That is
    D295, and it is why `grant_permission` (which creates a Role with an AUTO
    id) is not enough: the filter is on `user_role.c.role_id == ROLE_ADMIN`, so
    the role's id must BE ROLE_ADMIN. The get-or-create below is the same shape
    as tests/test_ap_update_post_tails.py:3021.
    """
    role = db.session.get(Role, ROLE_ADMIN)
    if role is None:
        role = Role(id=ROLE_ADMIN, name=f'role-{ROLE_ADMIN}', weight=0)
        db.session.add(role)
        db.session.commit()
    user.roles.append(role)
    db.session.commit()


# ---------------------------------------------------------------------------
# SRC_API: the scalar reads, :251-264
# ---------------------------------------------------------------------------


def test_api_branch_reads_every_scalar_and_strips_the_title(db_session):
    """:251-260. The dict shape, entered with user= so :253 never runs.

    Title is `.strip()`ed at :254; the leading and trailing spaces here are the
    witness that :254 ran rather than a bare assignment.
    """
    s = _seed()
    edit_post(_api_input(title='  spaced  ', body='new body', nsfw=True,
                         ai_generated=True, notify_author=False),
              s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user)

    assert s.post.title == 'spaced'
    assert s.post.body == 'new body'
    assert s.post.nsfw is True
    assert s.post.ai_generated is True
    assert s.post.notify_author is False


def test_api_branch_falls_back_to_the_users_timezone_when_the_key_is_absent(db_session):
    """:261, false arm of `'timezone' in input`."""
    s = _seed()
    s.user.timezone = 'Pacific/Auckland'
    db.session.commit()

    edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user)

    assert s.post.timezone == 'Pacific/Auckland'


def test_api_branch_takes_the_supplied_timezone_when_the_key_is_present(db_session):
    """:261, true arm. The user's timezone differs, so a pass-through of the
    user value would not be distinguishable from the input value."""
    s = _seed()
    s.user.timezone = 'Pacific/Auckland'
    db.session.commit()

    edit_post(_api_input(timezone='Europe/Berlin'), s.post, POST_TYPE_ARTICLE,
              SRC_API, user=s.user)

    assert s.post.timezone == 'Europe/Berlin'


@pytest.mark.parametrize('supplied,expected', [
    (_OMIT, ''),     # :262 false arm -- key absent, never reaches :263
    (None, ''),      # :262 true arm, then :263 true arm -- present but None
    ('alt', 'alt'),  # :262 true arm, :263 false arm
])
def test_api_branch_normalises_absent_and_null_alt_text_to_empty(
        db_session, http_mock, supplied, expected):
    """:262-264. Three arms across two guards, and they are NOT the same arm:
    an absent key never reaches :263, a null one does.

    `image_alt_text` has an observable only on the image path, where :666
    `file.alt_text = image_alt_text` writes it to a real File row. Without a url
    the local is carried and dropped, and the three cases would be
    indistinguishable -- so this drives a .png and reads the File back. That
    also shows what :263 is FOR: without it, `None` would reach :666 and null
    the column rather than clearing it to ''.
    """
    s = _seed()
    http_mock.head('https://example.com/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://example.com/pic.png').respond(404)

    edit_post(_api_input(image_alt_text=supplied, url='https://example.com/pic.png'),
              s.post, POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=True)

    db.session.expire(s.post)
    file = File.query.get(s.post.image_id)
    assert file is not None
    assert file.alt_text == expected


# ---------------------------------------------------------------------------
# SRC_API: tags and flair, :265-282
# ---------------------------------------------------------------------------


def test_api_branch_parses_a_tag_string_when_the_key_is_present(db_session):
    """:265-266, true arm."""
    s = _seed()
    edit_post(_api_input(tags='alpha,beta'), s.post, POST_TYPE_ARTICLE, SRC_API,
              user=s.user)

    db.session.expire(s.post)
    assert sorted(t.name for t in s.post.tags) == ['alpha', 'beta']


def test_api_branch_leaves_tags_empty_when_the_key_is_absent(db_session):
    """:267-268, false arm. The post starts with a tag so an empty result is a
    clearing rather than a no-op -- :454 `post.tags.clear()` runs first."""
    s = _seed()
    edit_post(_api_input(tags='pre'), s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user)
    db.session.expire(s.post)
    assert [t.name for t in s.post.tags] == ['pre']

    edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user)
    db.session.expire(s.post)
    assert list(s.post.tags) == []


def test_api_branch_parses_a_flair_string_when_the_flair_key_is_present(db_session):
    """:269-270, first arm of the three-way flair dispatch.

    `flairs_from_string(input['flair'], post.community_id)`
    (app/community/util.py:381) resolves each comma-separated name through
    `find_flair(name, community_id)`, so the flair must belong to THIS post's
    community or the lookup returns nothing and the test passes vacuously.
    """
    s = _seed()
    flair = make_community_flair(s.community, name='news')

    edit_post(_api_input(flair='news'), s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user)

    db.session.expire(s.post)
    assert [f.id for f in s.post.flair] == [flair.id]


def test_api_branch_accepts_a_bare_integer_flair_id(db_session):
    """:271-275, the `isinstance(flair_id, int)` true arm -- the RSS shape."""
    s = _seed()
    flair = make_community_flair(s.community, name='rss')

    edit_post(_api_input(flair_id=flair.id), s.post, POST_TYPE_ARTICLE, SRC_API,
              user=s.user)

    db.session.expire(s.post)
    assert [f.id for f in s.post.flair] == [flair.id]


def test_api_branch_accepts_a_list_of_flair_ids(db_session):
    """:276-277, the isinstance false arm."""
    s = _seed()
    one = make_community_flair(s.community, name='one')
    two = make_community_flair(s.community, name='two')
    assert len({one.id, two.id}) == 2

    edit_post(_api_input(flair_id=[one.id, two.id]), s.post, POST_TYPE_ARTICLE,
              SRC_API, user=s.user)

    db.session.expire(s.post)
    assert sorted(f.id for f in s.post.flair) == sorted([one.id, two.id])


def test_api_branch_drops_flair_ids_that_match_no_row(db_session):
    """:278, the list comprehension's filter. `CommunityFlair.query.get()`
    returns None for a missing id, and :278 is what stops that None reaching
    post.flair."""
    s = _seed()
    real = make_community_flair(s.community, name='real')
    missing = real.id + 1000

    edit_post(_api_input(flair_id=[real.id, missing]), s.post, POST_TYPE_ARTICLE,
              SRC_API, user=s.user)

    db.session.expire(s.post)
    assert [f.id for f in s.post.flair] == [real.id]


def test_api_branch_leaves_flair_empty_when_flair_id_is_falsy(db_session):
    """:271's second conjunct false, so :279-280. An empty list is present but
    falsy -- distinct from the key being absent."""
    s = _seed()
    edit_post(_api_input(flair_id=[]), s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user)

    db.session.expire(s.post)
    assert list(s.post.flair) == []


def test_api_branch_leaves_flair_empty_when_neither_key_is_present(db_session):
    """:279-280 reached with both `'flair' in input` and `'flair_id' in input`
    false."""
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user)

    db.session.expire(s.post)
    assert list(s.post.flair) == []
