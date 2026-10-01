"""`edit_post`'s source dispatch and its suspicious-domain block.

`app/shared/post.py:250-754` is the LOCAL post editor. Its federated twin,
`update_post_from_activity`, was taken to zero uncovered statements by
sub-project 17; two of that sub-project's findings (D287 and D286) are
statements about how the two copies disagree, and both were found by reading
this function without ever executing it. This file executes it. (The other
finding formerly mislabelled here is a different, still-open one -- `Post.new`'s
unguarded `choice_ap['name']` read at `app/models.py:2205-2209`, the
create-path sibling of D284 -- and has no test in this file.)

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
`authorise_api_user` and `current_user` alike -- which is why almost every test
here needs no request context, no login and no auth token, and is the reason the
file works at all. Two tests deliberately do the opposite, because those two
`if not user:` are themselves branch arms and the only way to take them is to
withhold `user=`:
`test_api_branch_authorises_from_the_bearer_token_when_no_user_is_passed` takes
:253 and therefore mints a real Bearer JWT, and
`test_web_branch_falls_back_to_the_logged_in_user_when_none_is_passed` takes
:317 and therefore runs inside a Flask-Login request context. So the escape is a
choice made per test, not a property of the file.

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

`http_mock` is `assert_all_called=True` (tests/conftest.py:336-343), so each
test registers exactly the routes its own path reaches -- a registered route
that is never reached FAILS the test at teardown. The two crash tests
(`test_a_banned_domain_raises_before_anything_is_notified` and
`test_a_pages_dev_domain_raises_even_when_it_is_not_banned`) therefore take no
`http_mock` at all and register nothing: :569's raise sits inside :567's
`if domain:`, which runs before :600-601, so those paths issue no HTTP request
whatsoever.

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
from app.constants import ROLE_ADMIN, ROLE_ADMIN_NAME
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


_WEB_FORM_BARE_NONE_FIELDS = {'flair', 'finish_in', 'image_alt_text'}
"""Fields the SRC_WEB branch tests at the FIELD level rather than through
`.data`: `if input.flair:` (:333), `if input.finish_in:` (:354), and
`hasattr(input, 'image_alt_text')` (:340). For these three, and only these
three, a `None` value must land on the form as a bare attribute -- `_Field`
has no `__bool__`, so a `_Field(None)` would be truthy and never take the
false arm the caller asked for. Every other field is read through `.data`
unconditionally (e.g. :331 `input.language_id.data`, :337-338
`input.scheduled_for.data` / `input.repeat.data`), so a `None` there must be
`_Field(None)` -- `.data` on a bare `None` attribute raises AttributeError.
"""


def _web_form(**over):
    """The SRC_WEB shape, as a plain object carrying `_Field` attributes.

    Defaults are a complete POST_TYPE_LINK submission. `over` replaces:
      value        -> wrapped in _Field
      None, for a field in _WEB_FORM_BARE_NONE_FIELDS
                   -> the ATTRIBUTE is set to None, so `if input.flair:` (:333),
                      `if input.finish_in:` (:354) and `input.image_alt_text`
                      (:340) take their false arms
      None, for any other field
                   -> wrapped as `_Field(None)`, so `.data` reads back None
                      instead of raising (:331, :337-338 and others read
                      `.data` unconditionally)
      _OMIT        -> the attribute is not set at all, so `hasattr` (:340) is
                      False

    `sticky` and `nsfl` are present because :388 and :390 read them below the
    :384 marker.

    `language_id`, `scheduled_for` and `repeat` default to `None` here
    (meaning: the field's `.data` is None), which is why they are NOT in
    `_WEB_FORM_BARE_NONE_FIELDS` -- unlike `flair`/`finish_in`, nothing
    downstream tests them at the field-object level.

    `flair` defaults to `[]`, not `''`. `_Field` has no `__bool__`, so
    `if input.flair:` (:333) is true for ANY `_Field` instance regardless of
    `.data` -- the default therefore always takes the true arm and calls
    `flair_from_form(input.flair.data)` (:334). `flair_from_form` (:334,
    app/community/util.py:375-378) does `CommunityFlair.id.in_(tag_ids)`,
    and SQLAlchemy accepts an empty list there but raises `ArgumentError` on a
    bare string -- `''` is iterable but not a valid `IN` operand.
    """
    fields = {
        'title': 'a title', 'body': 'a body',
        'link_url': 'https://example.com/page', 'video_url': 'https://example.com/v.mp4',
        'nsfw': False, 'ai_generated': False, 'notify_author': True,
        'language_id': None, 'tags': '', 'flair': [],
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
        if value is None and name in _WEB_FORM_BARE_NONE_FIELDS:
            setattr(form, name, None)
        else:
            setattr(form, name, _Field(value))
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
    the db_session teardown resets every sequence (tests/conftest.py:131-132), so
    the first
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

    `Site.admins()` (app/models.py) runs its query here: tests/conftest.py
    clears `flask.g` before every test and nothing in this file sets
    `admin_ids`. It matches a role by NAME (D481, as `is_admin()` does), so the
    get-or-create below names the role ROLE_ADMIN_NAME. The id stays
    ROLE_ADMIN so the row is shared with any other helper that made it.
    """
    role = db.session.get(Role, ROLE_ADMIN)
    if role is None:
        role = Role(id=ROLE_ADMIN, name=ROLE_ADMIN_NAME, weight=0)
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


def test_api_branch_authorises_from_the_bearer_token_when_no_user_is_passed(db_session):
    """:252-253, the TRUE arm of `if not user:` -- the one SRC_API test in this
    file that does not pass `user=`.

    Every other test here passes `user=` so that :253 is skipped (see the module
    docstring). This one takes the other arm, so `authorise_api_user`
    (app/utils.py:3585) has to actually succeed and hand back a User.

    THE WITNESS is :261's false arm, `user.timezone`. `timezone` is left out of
    the input, so the value written to the post can only have come from the
    object :253 bound -- a call that returned the wrong user, or that was
    skipped, could not produce 'Pacific/Auckland'.

    WHY password_updated_at IS PINNED TO THE PAST. The column defaults to
    `utcnow` (app/models.py:1051), so `make_user` stamps it with a
    sub-second-precision "now". :3630-3634 compares it against the token's
    `iat`, which `encode_jwt_token` (app/models.py:1618) truncates to whole
    seconds -- a token minted in the same wall-clock second as the user is
    therefore sometimes read as predating a password change and rejected. This
    is the same pin, for the same reason, that tests/conftest.py's
    `api_baseline` fixture applies to its user 1.

    Nothing else has to be arranged: `make_user` already produces the shape
    :3628 demands (ap_id None, verified True, banned False, deleted False), and
    `_seed`'s post is authored by this same user, so :253's
    `id_match=post.user_id` matches.
    """
    s = _seed()
    s.user.timezone = 'Pacific/Auckland'
    s.user.password_updated_at = datetime(2000, 1, 1)
    db.session.commit()

    edit_post(_api_input(title='from the token'), s.post, POST_TYPE_ARTICLE,
              SRC_API, auth=f'Bearer {s.user.encode_jwt_token()}')

    db.session.expire(s.post)
    assert s.post.title == 'from the token'
    assert s.post.timezone == 'Pacific/Auckland'


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
    file = db.session.get(File, s.post.image_id)
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
    clearing rather than a no-op.

    A CORRECTION, MEASURED BY SUB-PROJECT 39 TASK 9's MUTATION PASS. An earlier
    revision of this docstring credited `:454`'s `post.tags.clear()` with the
    clearing. It does not perform it. `:725` assigns unconditionally::

        454	        post.tags.clear()
        455	        post.flair.clear()
        ...
        725	    post.tags = tags
        726	    post.flair = flair

    and there is NO `return` anywhere between `:420` and `:726` -- only five
    `raise`s, at `:468`, `:501`, `:533`, `:540` and `:569`. So on every path
    that returns normally, `:725` overwrites whatever `:454` left, and the
    empty list this test sees is `:725` assigning an empty `tags`. Deleting
    `:454` outright survives all 354 tests in tests/test_shared_post_*.py.

    `:454`/`:455` are therefore reachable only as a pre-`raise` effect: a
    `from_scratch=False` edit whose `:459` commits the clear and which then
    raises before `:725`. This test does not exercise that, and must not be
    read as covering it."""
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
    falsy -- distinct from the key being absent.

    THAT ARM IS NOT SEPARATELY OBSERVABLE, though, so this test documents it
    rather than pinning it. Measured: dropping :271's `and input['flair_id']`
    leaves this passing, because the `[]` then falls through to :277 and
    `CommunityFlair.id.in_([])` selects nothing, so :278 yields the same `[]`
    the :279-280 arm assigns. The two arms are distinguishable only in the
    query issued, which nothing here observes."""
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


# ---------------------------------------------------------------------------
# SRC_API: event and poll parsing, :284-314
# ---------------------------------------------------------------------------


def test_api_event_start_string_is_parsed_and_stripped_of_its_offset(db_session):
    """:288-290. `.replace(tzinfo=None)` AFTER fromisoformat means a peer
    offset is DISCARDED rather than converted: 09:00+05:00 is stored as 09:00,
    not as 04:00 UTC.

    The federated copy does the opposite. `app/activitypub/util.py:3375-3376`
    parses startTime/endTime with a bare `fromisoformat` and keeps the value
    aware, so the two editors disagree about what a peer's event time means.
    """
    s = _seed()
    edit_post(_api_input(event={'start': '2030-06-01T09:00:00+05:00',
                                'end': '2030-06-01T10:00:00+05:00'}),
              s.post, POST_TYPE_EVENT, SRC_API, user=s.user)

    event = Event.query.filter_by(post_id=s.post.id).first()
    assert event is not None
    assert event.start == datetime(2030, 6, 1, 9, 0)
    assert event.start.tzinfo is None


def test_api_event_start_that_is_already_a_datetime_passes_through(db_session):
    """:291-292, the isinstance false arm -- the self-assignment."""
    s = _seed()
    naive = datetime(2030, 6, 1, 9, 0)
    edit_post(_api_input(event={'start': naive, 'end': datetime(2030, 6, 1, 10, 0)}),
              s.post, POST_TYPE_EVENT, SRC_API, user=s.user)

    event = Event.query.filter_by(post_id=s.post.id).first()
    assert event.start == naive


def test_api_event_end_is_parsed_when_there_is_no_start(db_session):
    """:288, the FALSE arm -- the jump from :288 straight to :293.

    Every other event test in this file supplies `start`, so :288 always took
    its true arm and the edge :288 -> :293 was never walked. An `event` dict
    carrying only `end` walks it: the start block is skipped entirely and the
    end block still parses.

    The shape is not hypothetical -- :288 and :293 are independent `in` tests,
    not an if/elif -- and :703 `if 'start' in event_data:` mirrors it when the
    Event row is written, so `event.start` simply stays None.

    `community.local_only = True` for the same reason as the two siblings
    below: :743's federate step reaches `ap_datetime` on the event's datetimes
    (app/shared/tasks/pages.py:234) and WOULD ONCE HAVE CRASHED on the None
    start, on a line that has nothing to do with the arm under test. :736 sets
    `federate = False` before that call.

    NO LONGER A CRASH: fixed in e1692167, which guards the read and omits
    the key. The route-around is retained pending a follow-up task -- it is
    another sub-project's behaviour to change, and removing it here would
    alter what this test covers.
    """
    s = _seed()
    s.community.local_only = True
    db.session.commit()
    edit_post(_api_input(event={'end': '2030-06-01T10:00:00+05:00'}),
              s.post, POST_TYPE_EVENT, SRC_API, user=s.user)

    event = Event.query.filter_by(post_id=s.post.id).first()
    assert event is not None
    assert event.start is None
    assert event.end == datetime(2030, 6, 1, 10, 0)


def test_api_event_end_is_skipped_when_the_key_is_absent(db_session):
    """:293, first conjunct false.

    Not in the brief: with `end` unset, `event.end` stays None, and this
    function's own federate step (:743, unconditional on `from_scratch`)
    synchronously runs `send_post`, which for a POST_TYPE_EVENT post does
    `ap_datetime(event.end)` (app/shared/tasks/pages.py:236) -- once a crash
    on None unrelated to what this test probes. `community.local_only = True`
    makes :736 set `federate = False` before that call, so the parsing under
    test still runs but the unrelated federate crash does not.

    NO LONGER A CRASH: fixed in e1692167, which guards the read and omits
    the key. The route-around is retained pending a follow-up task -- it is
    another sub-project's behaviour to change, and removing it here would
    alter what this test covers.
    """
    s = _seed()
    s.community.local_only = True
    db.session.commit()
    edit_post(_api_input(event={'start': '2030-06-01T09:00:00Z'}),
              s.post, POST_TYPE_EVENT, SRC_API, user=s.user)

    event = Event.query.filter_by(post_id=s.post.id).first()
    assert event.start == datetime(2030, 6, 1, 9, 0)


def test_api_event_end_is_skipped_when_the_value_is_falsy(db_session):
    """:293, second conjunct false. Present-but-None is a different arm from
    absent, and neither reaches :294.

    `community.local_only = True` for the same reason as the sibling test
    above: `event.end` is None here too, and would otherwise crash the
    federate call at :743 on an unrelated line.
    """
    s = _seed()
    s.community.local_only = True
    db.session.commit()
    edit_post(_api_input(event={'start': '2030-06-01T09:00:00Z', 'end': None}),
              s.post, POST_TYPE_EVENT, SRC_API, user=s.user)

    event = Event.query.filter_by(post_id=s.post.id).first()
    assert event.start == datetime(2030, 6, 1, 9, 0)


def test_api_event_end_that_is_already_a_datetime_passes_through(db_session):
    """:296-297, the isinstance false arm for `end`."""
    s = _seed()
    naive = datetime(2030, 6, 1, 10, 0)
    edit_post(_api_input(event={'start': '2030-06-01T09:00:00Z', 'end': naive}),
              s.post, POST_TYPE_EVENT, SRC_API, user=s.user)

    event = Event.query.filter_by(post_id=s.post.id).first()
    assert event.end == naive


def test_api_event_block_is_skipped_when_there_is_no_event_key(db_session):
    """:285-286, false arm. `input.get('event', None)` is the API dict's only
    `.get` -- the surrounding reads all subscript."""
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user)

    assert Event.query.filter_by(post_id=s.post.id).first() is None


def test_api_poll_defaults_every_field_when_only_choices_are_given(db_session):
    """:303-307. Two of the three `.get` defaults at once: `mode` -> 'single'
    (:304) and `local_only` -> False (:305). `choices` (:306) is SUPPLIED here,
    so its `[]` default is NOT taken -- the choices are what make the defaults
    observable, since a poll with no choices writes no PollChoice rows to read
    back.

    Not in the brief: with no `end_poll`, `poll.end_poll` stays None, and
    this function's federate step (:743) synchronously runs `send_post`,
    which for a POST_TYPE_POLL post does `ap_datetime(poll.end_poll)`
    (app/shared/tasks/pages.py:225) -- once a crash on None unrelated to what
    this test probes. `community.local_only = True` makes :736 set
    `federate = False` first, so the parsing under test still runs but the
    unrelated federate crash does not.

    NO LONGER A CRASH: fixed in e1692167, which guards the read and omits
    the key. The route-around is retained pending a follow-up task -- it is
    another sub-project's behaviour to change, and removing it here would
    alter what this test covers.
    """
    s = _seed()
    s.community.local_only = True
    db.session.commit()
    edit_post(_api_input(poll={'choices': [{'choice_text': 'yes', 'sort_order': 1},
                                           {'choice_text': 'no', 'sort_order': 2}]}),
              s.post, POST_TYPE_POLL, SRC_API, user=s.user)

    poll = Poll.query.filter_by(post_id=s.post.id).first()
    assert poll is not None
    assert poll.mode == 'single'
    assert poll.local_only is False
    assert sorted(c.choice_text for c in
                  PollChoice.query.filter_by(post_id=s.post.id).all()) == ['no', 'yes']


def test_api_poll_takes_supplied_mode_and_local_only(db_session):
    """:304-305, the non-default arms."""
    s = _seed()
    edit_post(_api_input(poll={'mode': 'multiple', 'local_only': True,
                               'choices': [{'choice_text': 'a', 'sort_order': 1}]}),
              s.post, POST_TYPE_POLL, SRC_API, user=s.user)

    poll = Poll.query.filter_by(post_id=s.post.id).first()
    assert poll.mode == 'multiple'
    assert poll.local_only is True


def test_api_poll_end_is_skipped_when_the_key_is_absent(db_session):
    """:308, first conjunct false.

    `community.local_only = True` for the reason documented on
    `test_api_poll_defaults_every_field_when_only_choices_are_given` above:
    `poll.end_poll` is None here too, and the federate call at :743 would
    otherwise crash on an unrelated line.
    """
    s = _seed()
    s.community.local_only = True
    db.session.commit()
    edit_post(_api_input(poll={'choices': [{'choice_text': 'a', 'sort_order': 1}]}),
              s.post, POST_TYPE_POLL, SRC_API, user=s.user)

    assert Poll.query.filter_by(post_id=s.post.id).first().end_poll is None


def test_api_poll_end_is_skipped_when_the_value_is_falsy(db_session):
    """:308, second conjunct false.

    `community.local_only = True` for the same reason as the sibling test
    above: `poll.end_poll` is None here too.
    """
    s = _seed()
    s.community.local_only = True
    db.session.commit()
    edit_post(_api_input(poll={'end_poll': None,
                               'choices': [{'choice_text': 'a', 'sort_order': 1}]}),
              s.post, POST_TYPE_POLL, SRC_API, user=s.user)

    assert Poll.query.filter_by(post_id=s.post.id).first().end_poll is None


def test_api_poll_end_that_is_already_a_datetime_passes_through(db_session):
    """:311-312, the isinstance false arm."""
    s = _seed()
    naive = datetime(2030, 6, 1, 12, 0)
    edit_post(_api_input(poll={'end_poll': naive,
                               'choices': [{'choice_text': 'a', 'sort_order': 1}]}),
              s.post, POST_TYPE_POLL, SRC_API, user=s.user)

    assert Poll.query.filter_by(post_id=s.post.id).first().end_poll == naive


def test_api_poll_end_string_is_converted_to_utc_preserving_the_instant(db_session):
    """:309-310. THE D297 PROBE, and the reason this cluster exists.

    :310 is `datetime.fromisoformat(...replace('Z', '+00:00'))` with NO
    `.replace(tzinfo=None)` -- unlike its two siblings at :290 and :295. So it
    yields an AWARE datetime, and writes it into `Poll.end_poll`, which is
    `db.Column(db.DateTime)` -- TIMESTAMP WITHOUT TIME ZONE (app/models.py:3782).

    The federated copy does not parse at all: `app/activitypub/util.py:3338` is
    `poll.end_poll = request_json['object']['endTime']`, the raw string (D290).

    This test records what the write ACTUALLY does, measured, not argued.

    MEASURED (D297): `2030-06-01T12:00:00+05:00` in, `2030-06-01 07:00:00`
    (naive) out -- the brief's SECOND outcome, not its first. The driver did
    not merely drop the offset and keep the wall-clock digits (which would
    have stored hour 12, tzinfo None, the same lossy shape as :290/:295) --
    it converted the aware value to UTC and only then dropped the tzinfo,
    storing hour 7. So the local editor and the federated editor
    (app/activitypub/util.py:3338, which stores the raw string unparsed) now
    disagree about the *instant* a peer's poll ends, not merely about how
    that instant is represented.

    THE CONTRAST WITH EVENTS, same SRC_API branch, same input offset
    (+05:00), same 3-hour gap between the two siblings' code and this one's:
    `test_api_event_start_string_is_parsed_and_stripped_of_its_offset` feeds
    `09:00:00+05:00` through :288-290 and gets back hour 9 -- the wall-clock
    digits survive, the instant does not. This test feeds `12:00:00+05:00`
    through :309-310 and gets back hour 7 -- the instant survives, the
    wall-clock digits do not. The only code difference between the two sites
    is that :290 ends in `.replace(tzinfo=None)` and :310 does not -- one
    `.replace` call is the entire reason events and polls, edited through the
    same function, disagree with each other about what a peer's aware
    timestamp means, before either is compared against its own federated
    twin.
    """
    s = _seed()
    edit_post(_api_input(poll={'end_poll': '2030-06-01T12:00:00+05:00',
                               'choices': [{'choice_text': 'a', 'sort_order': 1}]}),
              s.post, POST_TYPE_POLL, SRC_API, user=s.user)

    stored = Poll.query.filter_by(post_id=s.post.id).first().end_poll
    assert stored.tzinfo is None, 'psycopg2 strips tzinfo on the way into a naive column'
    assert stored == datetime(2030, 6, 1, 7, 0), (
        'the driver converted 12:00+05:00 to its 07:00 UTC equivalent before '
        'stripping tzinfo, rather than keeping the wall-clock digits 12:00')


def test_api_poll_block_is_skipped_when_there_is_no_poll_key(db_session):
    """:300-301, false arm."""
    s = _seed()
    edit_post(_api_input(), s.post, POST_TYPE_ARTICLE, SRC_API, user=s.user)

    assert Poll.query.filter_by(post_id=s.post.id).first() is None


# ---------------------------------------------------------------------------
# SRC_WEB: the form shape, :315-383
# ---------------------------------------------------------------------------


def test_web_branch_falls_back_to_the_logged_in_user_when_none_is_passed(app, db_session):
    """:316-317, the TRUE arm of `if not user:` -- the one SRC_WEB test in this
    file that does not pass `user=`.

    Every other test here passes `user=` so that :317 is skipped (see the module
    docstring), which is what lets the rest of the file run with no request
    context at all. This one supplies the context instead: `login_user` inside
    `app.test_request_context('/')` is the same shape tests/factories.py:117-118
    and tests/test_utils_upload_video.py:222-223 use.

    THE WITNESS is :386 `post.indexable = user.indexable`. The user is flipped
    to non-indexable first, and Post.indexable defaults to True
    (app/models.py:1731), so a False on the post can only have come from the
    object :317 bound. An anonymous context would not merely give a different
    answer -- Flask-Login's AnonymousUserMixin has no `indexable`, so :386 would
    raise.

    POST_TYPE_ARTICLE keeps `url` None at :327, so no HEAD is issued and no
    `http_mock` route is needed -- the whole point of this test is the two lines
    above the type dispatch.
    """
    from flask_login import login_user

    s = _seed()
    s.user.indexable = False
    db.session.commit()

    with app.test_request_context('/'):
        login_user(s.user)
        edit_post(_web_form(title='from current_user'), s.post,
                  POST_TYPE_ARTICLE, SRC_WEB)

    db.session.expire(s.post)
    assert s.post.title == 'from current_user'
    assert s.post.indexable is False


def test_web_branch_takes_the_link_url_for_a_link_post(db_session, http_mock):
    """:320-321. `.strip()` at :321 is proved by the padding.

    A url means :601 `is_image_url(url)` fires one HEAD. The seeded post has
    url=None so :410's second call never happens.
    """
    s = _seed()
    http_mock.head('https://example.com/doc').respond(200, headers={'Content-Type': 'text/html'})
    http_mock.get('https://example.com/doc').respond(200, html='<html></html>')

    edit_post(_web_form(link_url='  https://example.com/doc  '), s.post,
              POST_TYPE_LINK, SRC_WEB, user=s.user)

    assert s.post.url == 'https://example.com/doc'


def test_web_branch_takes_the_video_url_for_a_video_post(db_session, http_mock):
    """:322-323."""
    s = _seed()
    http_mock.head('https://example.com/clip.mp4').respond(200, headers={'Content-Type': 'video/mp4'})
    http_mock.get('https://example.com/clip.mp4').respond(200, html='')

    edit_post(_web_form(video_url='  https://example.com/clip.mp4  '), s.post,
              POST_TYPE_VIDEO, SRC_WEB, user=s.user)

    assert s.post.url == 'https://example.com/clip.mp4'
    assert s.post.type == POST_TYPE_VIDEO


def test_web_branch_keeps_the_existing_url_for_an_image_edit(db_session, http_mock):
    """:324-325, BOTH conjuncts true: POST_TYPE_IMAGE and not from_scratch.

    This is the one arm that reads `post.url` rather than the form. The brief
    called for TWO HEADs (:410 and :601) against the same address, but that is
    not what happens here: since `url = post.url` at :325, `url != post.url`
    at :435 is False, so `url_changed` stays False, and the whole :565 block
    -- including the :601 `is_image_url` call -- is skipped. Only the
    unconditional :410 `is_image_url(post.url)` fires, so exactly one HEAD is
    registered; a GET registration here would go uncalled and fail
    `http_mock`'s `assert_all_called=True`.

    Not in the brief: because :565's block never runs, `post.image_id` is
    never set, and this test does not pass `from_scratch` as True, so it is
    the `:743 elif federate:` arm that synchronously runs `task_selector`
    (app/shared/post.py:739-743) -> `edit_post` (app/shared/tasks/pages.py:76)
    -> `send_post` (:88), which for a POST_TYPE_IMAGE post does
    `attachment.append({'type': 'Image', 'url': post.image.source_url, ...})`
    (app/shared/tasks/pages.py:181, guarded only by `elif post.type ==
    POST_TYPE_IMAGE` with no `image_id` check) -- a crash on `post.image is
    None` unrelated to what this test probes. The outbox builder
    (`post_to_activity`, app/activitypub/util.py:100) guards the same
    dereference at :172 with `if post.image_id is not None:`; the two copies
    of this logic disagree about whether a POST_TYPE_IMAGE post with no image
    is safe to serialise, and that disagreement is exactly why this test needs
    a workaround. `community.local_only = True` makes :736 set
    `federate = False` first, so the parsed url under test still lands but the
    unrelated federate crash does not.

    THE URL ASSERTION ALONE IS TAUTOLOGICAL, so it is not the observable here.
    `post.url` is written nowhere outside :565's block, which this arm skips, so
    `assert s.post.url == ...` merely restates the seed -- measured: mutating
    :325 to `url = None` left it passing, because :410's HEAD is guarded by
    `post.url` and not by `url`. What distinguishes the arm is `url_changed`
    staying False at :435: under the mutant `url != post.url` becomes true, so
    :442-445 fires and decrements the seeded domain's `post_count`. The
    `post_count == 5` assertion below is therefore the one that kills it.
    """
    s = _seed(url='https://example.com/pic.png')
    s.community.local_only = True
    old_domain = make_domain('example.com')
    old_domain.post_count = 5
    db.session.commit()
    http_mock.head('https://example.com/pic.png').respond(200, headers={'Content-Type': 'image/png'})

    edit_post(_web_form(), s.post, POST_TYPE_IMAGE, SRC_WEB, user=s.user,
              from_scratch=False)

    assert s.post.url == 'https://example.com/pic.png'
    assert old_domain.post_count == 5


def test_web_branch_clears_the_url_for_an_image_created_from_scratch(db_session):
    """:324's second conjunct false, so :326-327 sets url=None. No HEAD, because
    :565 `if url and ...` and :601 both short-circuit on a falsy url.

    Not in the brief: with no url, `post.image_id` is never set, and this
    test passes `from_scratch=True`, so it is the `:739-741 if from_scratch:
    task_selector('make_post', ...)` arm (not `:743`'s `elif federate:`) that
    synchronously runs `make_post` (app/shared/tasks/pages.py:63) ->
    `send_post` (:67/:88), which for a POST_TYPE_IMAGE post does
    `attachment.append({'type': 'Image', 'url': post.image.source_url, ...})`
    (app/shared/tasks/pages.py:181, guarded only by `elif post.type ==
    POST_TYPE_IMAGE` with no `image_id` check) -- a crash on `post.image is
    None` unrelated to what this test probes. The outbox builder
    (`post_to_activity`, app/activitypub/util.py:100) guards the same
    dereference at :172 with `if post.image_id is not None:`; the two copies
    of this logic disagree about whether a POST_TYPE_IMAGE post with no image
    is safe to serialise, and that disagreement is exactly why this test needs
    a workaround. `community.local_only = True` makes :736 set
    `federate = False` first, so the parsed url under test still lands but the
    unrelated federate crash does not.
    """
    s = _seed()
    s.community.local_only = True
    db.session.commit()
    edit_post(_web_form(), s.post, POST_TYPE_IMAGE, SRC_WEB, user=s.user,
              from_scratch=True)

    assert s.post.url is None


def test_web_branch_clears_the_url_for_an_article(db_session):
    """:326-327 via the type dispatch falling all the way through."""
    s = _seed()
    edit_post(_web_form(), s.post, POST_TYPE_ARTICLE, SRC_WEB, user=s.user)

    assert s.post.url is None


def test_web_branch_reads_every_remaining_scalar_from_form_data(db_session):
    """:318-319, :328-330, :339, and :388/:390 below the :384 marker.

    :388 needs the user to be a moderator, owner or admin of the community for
    `post.sticky` to be written at all; without that the sticky read never runs.

    THREE OF THE READS IN THAT RANGE ARE CARRIED BUT NOT OBSERVED, and this
    docstring used to claim them: `language_id` (:331), `scheduled_for` (:337)
    and `repeat` (:338). `_web_form`'s defaults pin all three to `None`
    (tests/test_shared_post_edit.py:154-155) and no test in this file overrides
    them; the API branch does not read a form at all -- `language_id` comes
    from `_api_input`'s own `None` default (:183) and `scheduled_for`/`repeat`
    are hard-coded `None` in production at app/shared/post.py:281-282. No
    assertion anywhere in this file reads any of the three back. The statements
    execute -- that is all this test proves about them. The consequence is that
    `app/shared/post.py:413-416`'s `if scheduled_for:` block, and with it the
    `POST_STATUS_SCHEDULED` write at :416, NEVER RUNS in this suite.
    """
    s = _seed()
    make_community_member(s.user, s.community, is_moderator=True)

    edit_post(_web_form(title='  web title  ', body='web body', nsfw=True,
                        ai_generated=True, notify_author=False, timezone='Europe/Berlin',
                        sticky=True, nsfl=True),
              s.post, POST_TYPE_ARTICLE, SRC_WEB, user=s.user)

    assert s.post.title == 'web title'
    assert s.post.body == 'web body'
    assert s.post.nsfw is True
    assert s.post.ai_generated is True
    assert s.post.notify_author is False
    assert s.post.timezone == 'Europe/Berlin'
    assert s.post.sticky is True
    assert s.post.nsfl is True


def test_web_branch_parses_tags_from_the_form_string(db_session):
    """:332. Unconditional here -- the API branch guards the same call at :265."""
    s = _seed()
    edit_post(_web_form(tags='alpha,beta'), s.post, POST_TYPE_ARTICLE, SRC_WEB,
              user=s.user)

    db.session.expire(s.post)
    assert sorted(t.name for t in s.post.tags) == ['alpha', 'beta']


def test_web_branch_reads_flair_when_the_field_object_is_truthy(db_session):
    """:333-334, true arm."""
    s = _seed()
    flair = make_community_flair(s.community, name='news')

    edit_post(_web_form(flair=[flair.id]), s.post, POST_TYPE_ARTICLE,
              SRC_WEB, user=s.user)

    db.session.expire(s.post)
    assert [f.id for f in s.post.flair] == [flair.id]


def test_web_branch_leaves_flair_empty_when_the_field_object_is_falsy(db_session):
    """:335-336. The guard at :333 tests the FIELD, not `.data`, so a form
    without a flair field at all takes this arm -- `_web_form(flair=None)` sets
    the attribute to None rather than to a _Field."""
    s = _seed()
    edit_post(_web_form(flair=None), s.post, POST_TYPE_ARTICLE, SRC_WEB, user=s.user)

    db.session.expire(s.post)
    assert list(s.post.flair) == []


@pytest.mark.parametrize('image_alt_text,expected', [
    ('alt words', 'alt words'),  # :340 both conjuncts true
    (None, ''),                  # hasattr true, field falsy -- second conjunct false
    (_OMIT, ''),                 # hasattr false -- first conjunct false
])
def test_web_branch_alt_text_needs_both_hasattr_and_a_truthy_field(
        db_session, http_mock, image_alt_text, expected):
    """:340. Two conjuncts, three arms, and the witness is a real File.

    `post.image` is set only on the image path, so this drives a .png url and
    reads back `File.alt_text` written at :666 `if url and post.image:`.

    THE FALSE ARMS ASSERT THE EXACT VALUE, not `(alt_text == 'alt words') is
    False`. Under the boolean form the false cases only proved that :340's
    `else` produced something other than the true arm's string, so mutating
    `else ''` to `else 'MUTANT'` left all three parameterisations passing --
    measured. The API sibling
    `test_api_branch_normalises_absent_and_null_alt_text_to_empty` (:262-264)
    has always compared against the exact `''`; this now matches it.
    """
    s = _seed()
    http_mock.head('https://example.com/pic.png').respond(200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://example.com/pic.png').respond(404)

    edit_post(_web_form(link_url='https://example.com/pic.png',
                        image_alt_text=image_alt_text),
              s.post, POST_TYPE_LINK, SRC_WEB, user=s.user)

    db.session.expire(s.post)
    file = db.session.get(File, s.post.image_id)
    assert file is not None
    assert file.alt_text == expected


def test_web_branch_collects_non_empty_poll_choices_in_form_order(db_session):
    """:343-353. The loop runs 1..15 and :347 drops the empty slots, so a poll
    filled at 1, 3 and 15 proves both arms of the guard and the loop's extent."""
    s = _seed()
    edit_post(_web_form(choice_1='first', choice_3='third', choice_15='last'),
              s.post, POST_TYPE_POLL, SRC_WEB, user=s.user)

    rows = PollChoice.query.filter_by(post_id=s.post.id).order_by(PollChoice.sort_order).all()
    assert [(r.choice_text, r.sort_order) for r in rows] == [
        ('first', 1), ('third', 3), ('last', 15)]


def test_web_branch_strips_poll_choice_text(db_session):
    """:348's `.strip()`."""
    s = _seed()
    edit_post(_web_form(choice_1='  padded  '), s.post, POST_TYPE_POLL, SRC_WEB,
              user=s.user)

    rows = PollChoice.query.filter_by(post_id=s.post.id).all()
    assert [r.choice_text for r in rows] == ['padded']


def test_web_branch_sets_the_poll_end_when_finish_in_is_present(db_session):
    """:354-355, true arm."""
    s = _seed()
    edit_post(_web_form(choice_1='a', finish_in='3d'), s.post, POST_TYPE_POLL,
              SRC_WEB, user=s.user)

    assert Poll.query.filter_by(post_id=s.post.id).first().end_poll is not None


def test_web_branch_leaves_the_poll_end_unset_when_finish_in_is_absent(db_session):
    """:354, false arm. The guard tests the FIELD, so finish_in=None takes it.

    Not in the brief: with `finish_in=None`, `poll.end_poll` stays None, and
    this function's federate step (:743) synchronously runs `task_selector`
    (app/shared/post.py:739-743) -> `edit_post` (app/shared/tasks/pages.py:76)
    -> `send_post` (:88), which for a POST_TYPE_POLL post does
    `page['endTime'] = ap_datetime(poll.end_poll)` (app/shared/tasks/pages.py:225)
    -- once a crash on None unrelated to what this test probes (`ap_datetime`
    itself is app/utils.py:2294, `date_time.isoformat() + '+00:00'` with no
    None guard). `post_to_activity` (app/activitypub/util.py:195 does the same
    `ap_datetime(poll.end_poll)`) is NOT on this path -- its only caller is the
    outbox collection view at app/activitypub/routes.py:2033.
    `community.local_only = True` makes :736 set `federate = False` first, so
    the parsing under test still runs but the unrelated federate crash does
    not.

    NO LONGER A CRASH: fixed in e1692167, which guards the read and omits
    the key. The route-around is retained pending a follow-up task -- it is
    another sub-project's behaviour to change, and removing it here would
    alter what this test covers. app/activitypub/util.py:195 is NOT fixed and
    stays registered as D298.
    """
    s = _seed()
    s.community.local_only = True
    db.session.commit()
    edit_post(_web_form(choice_1='a', finish_in=None), s.post, POST_TYPE_POLL,
              SRC_WEB, user=s.user)

    assert Poll.query.filter_by(post_id=s.post.id).first().end_poll is None


def test_web_branch_leaves_poll_data_none_for_a_non_poll_type(db_session):
    """:356-357, EXECUTED BUT NOT OBSERVABLE.

    The `else` arm runs -- the type is not POST_TYPE_POLL -- but the value it
    assigns is never read: :669's `if type == POST_TYPE_POLL and poll_data:`
    tests `type` FIRST, and `type` is already POST_TYPE_ARTICLE here, so the
    second conjunct short-circuits away. Measured: replacing :357's `= None`
    with a full poll dict leaves this test passing. What the assertion below
    pins is that no Poll row was created, which is a fact about :669, not
    about :357.
    """
    s = _seed()
    edit_post(_web_form(), s.post, POST_TYPE_ARTICLE, SRC_WEB, user=s.user)

    assert Poll.query.filter_by(post_id=s.post.id).first() is None


def test_web_branch_converts_event_times_from_the_forms_timezone_to_utc(db_session):
    """:360-380. THE CONTRAST WITH :290.

    The web branch attaches the form's timezone at :362-363 and CONVERTS with
    `.astimezone(ZoneInfo('UTC'))` at :364-365 before stripping tzinfo at
    :368-369. The API branch at :290 strips WITHOUT converting. So the same wall
    clock submitted through the two branches lands on two different instants.

    Europe/Berlin is UTC+2 on 2030-06-01, so 09:00 local is 07:00 UTC.
    """
    s = _seed()
    edit_post(_web_form(event_timezone='Europe/Berlin',
                        start_datetime=datetime(2030, 6, 1, 9, 0),
                        end_datetime=datetime(2030, 6, 1, 10, 0)),
              s.post, POST_TYPE_EVENT, SRC_WEB, user=s.user)

    event = Event.query.filter_by(post_id=s.post.id).first()
    assert event.start == datetime(2030, 6, 1, 7, 0)
    assert event.end == datetime(2030, 6, 1, 8, 0)
    assert event.start.tzinfo is None


def test_web_branch_carries_every_remaining_event_field(db_session):
    """:370-379, including the nested location dict.

    Column names re-derived against app/models.py:3839-3855 and the write-back
    at app/shared/post.py:709 (`max_attendees`), :712 (`online`), :713
    (`online_link`) and :714 (`join_mode`) -- all four are real columns, so no
    substitution was needed.
    """
    s = _seed()
    edit_post(_web_form(max_attendees=42, online=True,
                        online_link='https://meet.example/x', join_mode='request',
                        irl_address='9 Lane', irl_city='Ville', irl_country='Elsewhere'),
              s.post, POST_TYPE_EVENT, SRC_WEB, user=s.user)

    event = Event.query.filter_by(post_id=s.post.id).first()
    assert event.max_attendees == 42
    assert event.online is True
    assert event.online_link == 'https://meet.example/x'
    assert event.join_mode == 'request'


def test_web_branch_leaves_event_data_none_for_a_non_event_type(db_session):
    """:381-382, EXECUTED BUT NOT OBSERVABLE -- the same shape as :356-357 above.

    :696's `if type == POST_TYPE_EVENT and event_data:` tests `type` FIRST, and
    `type` is POST_TYPE_ARTICLE here, so the assigned value is never read.
    Measured: replacing :382's `= None` with a full event dict leaves this test
    passing. The assertion below pins :696, not :382.
    """
    s = _seed()
    edit_post(_web_form(), s.post, POST_TYPE_ARTICLE, SRC_WEB, user=s.user)

    assert Event.query.filter_by(post_id=s.post.id).first() is None


# ---------------------------------------------------------------------------
# The domain and notify block, :565-598 -- D286 (the finding formerly
# mislabelled here is a different, still-open one: Post.new's
# app/models.py:2205-2209 unguarded choice_ap['name'] read)
# ---------------------------------------------------------------------------


def test_the_notify_dict_is_json_serialisable_and_the_edit_is_not_half_applied(
        db_session, http_mock):
    """D286, at the site the register calls the worst of its four.

    Before the fix, :577 puts the Domain ORM object into `targets_data`, which
    becomes `Notification.targets`, a db.JSON column. The flush raises
    `TypeError: Object of type Domain is not JSON serializable`.

    THE CONSEQUENCE IS WORSE THAN A CRASH, and the session boundaries inside
    edit_post are what make it so:

      :459  db.session.commit()      -- title/body/edited_at are already durable
      :598  db.session.add(notify)   -- the poisoned row is pending, nothing raises
      :606  db.session.add(file)
      :607  db.session.commit()      -- HERE

    (:588 is the moderator loop's `db.session.add(notify)`; this test reaches
    the admin loop's copy at :598. See the amendment note below.)

    MEASURED on the unfixed tree, this sub-project: after the StatementError and
    a rollback, `post.title` is `'the new title'` and `post.edited_at` is set,
    while `post.url` is still None, `post.image_id` is still None, and File and
    Notification both have zero rows. The edit is HALF-APPLIED -- the caller sees
    an exception, but the new title is already committed and the url the title
    now describes was never stored. The File at :606 is *not* separately
    orphaned: it is added in the same flush that raises, so it rolls back with
    the notification. The durable damage is everything :459 committed.
    `assert s.post.title == 'the new title'` below is the surviving witness of
    that boundary on the fixed tree.

    :459 runs only inside `if not from_scratch:` (:421), which is why this test
    passes from_scratch=False and changes the url -- the same change that sets
    url_changed at :436 and opens the :565 gate.

    TWO ROUTES ARE REGISTERED. Pre-fix the run dies at :607 and only the HEAD
    (from `is_image_url` at :601) is ever issued. On the FIXED tree :607 commits,
    :608 sets post.image_id, and :616 make_image_sizes fetches the source url --
    which is what the bodiless 404 on the GET absorbs. The GET is therefore
    required for this test to pass, and http_mock's assert_all_called only has
    to hold on the passing tree.

    CONTROLLER AMENDMENT (this task runs before the D287 task): this domain is
    seeded with notify_admins=True, not notify_mods=True. :582's
    `community_member.is_local()` is still broken (AttributeError) while D287
    is unfixed, so any moderator seeded on a notify_mods domain would die
    there before ever reaching :577. The notification here arrives through the
    ADMIN loop at :590-598; the moderator loop at :580-589 is still dead code
    at this point in the sub-project.
    """
    s = _seed(domain_name='suspicious.example', notify_admins=True)
    admin = make_user(s.instance, 'admin', local=True)
    _make_admin(admin)

    http_mock.head('https://suspicious.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://suspicious.example/pic.png').respond(404)

    edit_post(_api_input(title='the new title',
                         url='https://suspicious.example/pic.png'),
              s.post, POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=False)

    db.session.expire(s.post)
    assert s.post.title == 'the new title'

    notification = Notification.query.filter_by(user_id=admin.id).one()
    assert notification.targets['orig_post_domain'] == 'suspicious.example'
    assert isinstance(notification.targets['orig_post_domain'], str)


def test_the_notify_dict_carries_every_key_the_four_writers_share(db_session, http_mock):
    """:573-579. The dict's SHAPE is the thing D286's arbitration protects:
    `orig_post_domain` has four writers and zero readers, so the argument for
    keeping the key is that the four stay comparable.

    author_user_name at :578 is a conditional expression. coverage.py emits no
    arc for one (tests/README.md fact 87), so 100% statements and 100% branches
    can both hold while one arm has never run -- exactly D293's shape. This test
    takes the ap_id arm; the next takes the user_name arm.

    CONTROLLER AMENDMENT: seeded with notify_admins=True and an admin (not a
    moderator) for the same reason as the test above -- :582 is still broken
    while D287 is unfixed, so the notification here arrives through the ADMIN
    loop at :590-598, never the moderator loop at :580-589.
    """
    s = _seed(domain_name='suspicious.example', notify_admins=True)
    s.user.ap_id = 'editor@peer.example'
    db.session.commit()
    admin = make_user(s.instance, 'admin', local=True)
    _make_admin(admin)

    http_mock.head('https://suspicious.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://suspicious.example/pic.png').respond(404)

    edit_post(_api_input(title='shaped', url='https://suspicious.example/pic.png'),
              s.post, POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=False)

    targets = Notification.query.filter_by(user_id=admin.id).one().targets
    # CORRECTED BY D1391: `author_user_name` -> `suspect_user_user_name`, the key
    # app/templates/user/notifs/20.html:110 actually reads for this subtype. The
    # rename is what makes all four writers of this dict share one shape, which is
    # what this test is for.
    assert set(targets) == {'gen', 'post_id', 'orig_post_title', 'orig_post_body',
                            'orig_post_domain', 'suspect_user_user_name'}
    assert targets['gen'] == '0'
    assert targets['post_id'] == s.post.id
    assert targets['orig_post_title'] == 'shaped'
    assert targets['orig_post_domain'] == 'suspicious.example'
    assert targets['suspect_user_user_name'] == 'editor@peer.example'


def test_the_notify_dict_falls_back_to_user_name_when_there_is_no_ap_id(
        db_session, http_mock):
    """:578, the OTHER arm of the conditional expression. A local editor has
    ap_id None (tests/factories.py make_user), which is the ordinary case.

    CONTROLLER AMENDMENT: admin/notify_admins seeding, same reason as the two
    tests above -- reaches :577 through the ADMIN loop at :590-598.
    """
    s = _seed(domain_name='suspicious.example', notify_admins=True)
    assert s.user.ap_id is None
    admin = make_user(s.instance, 'admin', local=True)
    _make_admin(admin)

    http_mock.head('https://suspicious.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://suspicious.example/pic.png').respond(404)

    edit_post(_api_input(url='https://suspicious.example/pic.png'), s.post,
              POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=False)

    targets = Notification.query.filter_by(user_id=admin.id).one().targets
    # CORRECTED BY D1391, as in the shape test above: the key is
    # `suspect_user_user_name`. The `ap_id or user_name` fallback this test is
    # about is unchanged.
    assert targets['suspect_user_user_name'] == 'editor'


# ---------------------------------------------------------------------------
# The moderator loop, :580-589 -- D287
# ---------------------------------------------------------------------------


def test_a_moderator_of_a_notify_mods_domain_is_notified(db_session, http_mock):
    """D287. :580-589.

    Before the fix this raises `AttributeError: 'CommunityMember' object has no
    attribute 'is_local'` at :582 -- CommunityMember (app/models.py:3499-3513)
    has no such method. It has a `user` relationship at :3509, and User.is_local
    is at :1251.

    The failure is a CRASH, not a wrong value, and that is what makes D287 live
    rather than latent: every notify_mods domain took down the whole edit.

    This is the FIRST test to reach :588 `db.session.add(notify)` through the
    moderator loop; the D286 tests above reach the identical Notification
    construction through the ADMIN loop at :590-598 instead. :577 DOES NOT GAIN
    A SECOND CALLER from that -- `targets_data` is built once at :573-579,
    above both loops, and passed to each Notification BY REFERENCE, so :577
    executes exactly once per edit no matter how many recipients there are.
    What the moderator loop newly exercises is its own DB row and the JSON
    round-trip through it; the assertion on `orig_post_domain` below re-proves
    D286's fix as it is read back from a moderator's Notification rather than
    an admin's.
    """
    s = _seed(domain_name='suspicious.example', notify_mods=True)
    moderator = make_user(s.instance, 'mod', local=True)
    make_community_member(moderator, s.community, is_moderator=True)
    assert len({s.user.id, moderator.id}) == 2

    http_mock.head('https://suspicious.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://suspicious.example/pic.png').respond(404)

    edit_post(_api_input(url='https://suspicious.example/pic.png'), s.post,
              POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=False)

    notifications = Notification.query.filter_by(user_id=moderator.id).all()
    assert len(notifications) == 1
    assert notifications[0].notif_type == NOTIF_REPORT
    assert notifications[0].subtype == 'post_from_suspicious_domain'
    assert notifications[0].targets['orig_post_domain'] == 'suspicious.example'


def test_a_remote_moderator_of_a_notify_mods_domain_is_not_notified(db_session, http_mock):
    """:582, false arm -- and the reason D287's fix is `user.is_local()` rather
    than deleting the guard.

    D288, fixed (owner ruling): the federated copy of this loop in
    update_post_from_activity had no locality gate; it now has this one, so
    both editors notify local moderators only
    (test_ap_update_post_tails.py::test_a_remote_moderator_is_not_notified).

    DEVIATION FROM THE BRIEF: the brief built `peer` before `_seed()`. That
    ordering makes peer.example the id-1 Instance, and make_community hardcodes
    instance_id=1 (tests/factories.py:139, and see _seed's docstring), so the
    community under edit would have been stamped with an `instance_id` pointing
    at the PEER's Instance row instead of the local one.

    It would NOT have become a remote community: `Community.is_local()`
    (app/models.py:3201-3202) reads `ap_id`, and `make_community` never sets
    that column at all, so the community stays local however the instances are
    ordered. The damage is narrower and quieter than that -- a scrambled FK, a
    community whose rows say it lives on peer.example while every locality
    predicate still calls it local -- which is precisely why it is worth writing
    down rather than leaving to whoever next reorders these two lines.
    `_seed()` runs first, so the only remote thing in the fixture is the
    moderator, which is what the test is about.
    """
    s = _seed(domain_name='suspicious.example', notify_mods=True)
    peer = make_instance('peer.example', software='lemmy')
    remote_mod = make_user(peer, 'remotemod', local=False)
    make_community_member(remote_mod, s.community, is_moderator=True)
    assert remote_mod.ap_id is not None

    http_mock.head('https://suspicious.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://suspicious.example/pic.png').respond(404)

    edit_post(_api_input(url='https://suspicious.example/pic.png'), s.post,
              POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=False)

    assert Notification.query.filter_by(user_id=remote_mod.id).count() == 0


def test_a_moderator_is_not_notified_when_the_domain_does_not_ask(db_session, http_mock):
    """:580, false arm. notify_mods defaults to False
    (app/models.py:3458), which is the shape almost every Domain row has."""
    s = _seed(domain_name='quiet.example', notify_mods=False)
    moderator = make_user(s.instance, 'mod', local=True)
    make_community_member(moderator, s.community, is_moderator=True)

    http_mock.head('https://quiet.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://quiet.example/pic.png').respond(404)

    edit_post(_api_input(url='https://quiet.example/pic.png'), s.post,
              POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=False)

    assert Notification.query.filter_by(user_id=moderator.id).count() == 0


# ---------------------------------------------------------------------------
# The rest of the suspicious-domain block, :565-598
# ---------------------------------------------------------------------------


def test_the_domain_block_is_reached_from_scratch_without_a_url_change(db_session, http_mock):
    """:565, `from_scratch` true arm. `url_changed` is False here because :435's
    block runs only inside `if not from_scratch:` (:421) -- so this is the one
    route into :566 that does not depend on the url having changed."""
    s = _seed(domain_name='suspicious.example', notify_mods=True)
    moderator = make_user(s.instance, 'mod', local=True)
    make_community_member(moderator, s.community, is_moderator=True)

    http_mock.head('https://suspicious.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://suspicious.example/pic.png').respond(404)

    edit_post(_api_input(url='https://suspicious.example/pic.png'), s.post,
              POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=True)

    assert Notification.query.filter_by(user_id=moderator.id).count() == 1


def test_the_domain_block_is_skipped_when_the_url_is_unchanged(db_session, http_mock):
    """:565, both `from_scratch` and `url_changed` false. The post already HAS
    the url, so :435 `url != post.url` is false and :436 never runs.

    DEVIATION FROM THE BRIEF, on two points, both found by running the test.

    First: :600-601 (`fixup_url` / the second `is_image_url` call) are NOT
    unconditional -- they sit at the SAME indentation as :566's `domain =
    domain_from_url(url)`, both inside :565's `if url and (from_scratch or
    url_changed):`. With both false here, that whole block -- including :601
    -- never runs, so :410's HEAD (on the pre-existing `post.url`) is the ONLY
    request this path makes. The brief's claim that the route "is matched
    twice" and its registered GET-404 for the make_image_sizes retry are both
    wrong for this arm; the GET route is never reached and http_mock's
    assert_all_called=True fails on it if registered. Removed here.

    Second: :410 alone still sets `post.type = POST_TYPE_IMAGE` (the HEAD
    reports image/png), and because :601's file-creation block never runs,
    `post.image_id` stays None. `from_scratch=False` means :743
    `task_selector('edit_post', ...)` runs synchronously (Celery eager) and
    reaches the registered defect this campaign does not fix: pages.py:181
    `post.image.source_url` with no `image_id` guard, `post.image` is None,
    AttributeError. `s.community.local_only = True` (committed before the
    call) makes :736 set `federate = False`, so that dead branch of send_post
    is never entered -- confirmed this cannot affect the notify block, which
    is long done by :736. Applied only to this test in this file's new
    section: it is the only one whose post ends up POST_TYPE_IMAGE with
    image_id left None. The hostless-url test below crashes inside :601
    itself and never reaches :736; every other test here reaches :601 through
    the true arm of :565, which sets image_id before send_post ever runs.
    """
    s = _seed(url='https://suspicious.example/pic.png',
              domain_name='suspicious.example', notify_mods=True)
    s.community.local_only = True
    moderator = make_user(s.instance, 'mod', local=True)
    make_community_member(moderator, s.community, is_moderator=True)
    db.session.commit()

    http_mock.head('https://suspicious.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})

    edit_post(_api_input(url='https://suspicious.example/pic.png'), s.post,
              POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=False)

    assert Notification.query.filter_by(user_id=moderator.id).count() == 0


def test_the_domain_block_is_skipped_for_a_hostless_url(db_session):
    """:567, false arm. domain_from_url (app/utils.py:1561) returns None when
    urlparse finds no hostname, and every caller writes `if domain:` first.

    THE URL CARRIES A SCHEME AND NO AUTHORITY ('https:///etc/passwd') rather than
    the `file:///etc/passwd` this row used before D1407. That round added an
    http(s) check on the API's `url` above this line, so a `file:` url is now
    refused before the domain block is reached and no longer exercises it. The
    hostless-but-parseable shape still gets there, and it is the one
    `update_post_from_activity`'s own comment names: 'https:///x' parses and
    `.hostname` is None. Everything below still holds -- the request httpx builds
    from it is the same bare `/etc/passwd` path, which is what `match=` pins.

    MEASURED, against httpx 0.28.1 and this harness's respx setup, rather than
    assumed: httpx does not raise while BUILDING a HEAD request for a url with no
    authority -- Client.build_request succeeds for it, so
    mime_type_using_head's `except (httpx.HTTPError, httpx.InvalidURL):` guard
    is not what is at stake here. What matters is what happens once the
    request is actually sent. This harness's session-scoped
    `block_outbound_http` (tests/conftest.py) replaces httpx's transport with
    an empty respx router BEFORE `http_mock` even exists, so a request already
    goes through respx whether or not a test asks for `http_mock`. respx's own
    route matcher never matches an authority-less URL -- confirmed by registering
    the url itself in an isolated respx router and
    sending the same request through it: the call still misses the route and
    respx raises `respx.models.AllMockedAssertionError` (a plain
    AssertionError subclass, not httpx.HTTPError or httpx.InvalidURL), so
    mime_type_using_head's except clause does not catch it. `http_mock` is
    therefore deliberately NOT a fixture of this test: no route this test
    could register would ever be exercised, and http_mock's
    assert_all_called=True would fail on it regardless.

    :566-567 still run and take the false arm (domain is None) before :601's
    `is_image_url(url)` -- reached because :565's own gate is true here
    (`from_scratch=True`) -- makes that same HEAD attempt and the call raises.
    The raise is expected and is not this test's concern; what it proves is
    that no domain-block side effect (no post.domain write, no notification)
    happened first.

    THE `match=` IS LOAD-BEARING, and this file's own harness fact 102
    (tests/README.md) is about exactly this shape: :569 raises a bare
    `Exception`, so a bare `pytest.raises(Exception)` here swallowed anything
    at all -- measured: mutating :567 to `if True:` produced
    `AttributeError: 'NoneType' object has no attribute 'banned'`, which a bare
    `raises` accepted, and `Notification.query.count() == 0` held either way.
    Matching respx's own unmatched-request message pins that the raise came
    from :601's HEAD and not from a dereference of the None domain. A Domain
    row is seeded under the name a naive parse would pull off this url's PATH
    ('etc'), so `s.post.domain_id is None` below says the block attached
    nothing rather than merely that the table was empty.
    """
    s = _seed(domain_name='etc')

    with pytest.raises(Exception,
                       match=r"RESPX: <Request\('HEAD', '/etc/passwd'\)> not mocked!"):
        edit_post(_api_input(url='https:///etc/passwd'), s.post, POST_TYPE_LINK,
                  SRC_API, user=s.user, from_scratch=True)

    assert s.post.domain_id is None
    assert Notification.query.count() == 0


def test_a_banned_domain_raises_before_anything_is_notified(db_session):
    """:568-569, first conjunct. The message is the domain name plus a fixed
    suffix, and it is what the web route surfaces to the person editing.

    DEVIATION FROM THE BRIEF: dropped the registered HEAD route (and the
    `http_mock` fixture with it). :569's raise sits INSIDE :567's `if domain:`,
    which runs before :600-601 (see the sibling test above) -- so this path
    never issues an HTTP request at all, and a registered-but-unreached route
    would fail http_mock's assert_all_called=True at teardown. Measured: with
    the route registered, the run failed exactly that way.
    """
    s = _seed(domain_name='banned.example')
    s.domain.banned = True
    db.session.commit()

    with pytest.raises(Exception) as excinfo:
        edit_post(_api_input(url='https://banned.example/pic.png'), s.post,
                  POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=True)

    assert 'banned.example is blocked by admin' in str(excinfo.value)


def test_a_pages_dev_domain_raises_even_when_it_is_not_banned(db_session):
    """:568, second conjunct. `.pages.dev` is hardcoded, so a domain nobody has
    banned still raises -- the two conjuncts are separately load-bearing.

    DEVIATION FROM THE BRIEF: no `http_mock` fixture, for the same reason as
    the sibling banned-domain test above -- the raise at :569 precedes any
    HTTP request, so a registered HEAD route would go uncalled.
    """
    s = _seed(domain_name='thing.pages.dev')
    assert s.domain.banned is False

    with pytest.raises(Exception) as excinfo:
        edit_post(_api_input(url='https://thing.pages.dev/pic.png'), s.post,
                  POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=True)

    assert 'thing.pages.dev is blocked by admin' in str(excinfo.value)


def test_the_new_domain_gains_a_post_and_the_old_one_loses_one(db_session, http_mock):
    """:570-571, against :443-445's decrement on the old url. The two are a
    pair: a url change moves the count from one Domain to the other.

    Three HEAD/GET requests are reached, and this test registers exactly
    those three: :410's `is_image_url(post.url)` on the OLD url (seeded
    non-None on purpose, so that guard's true arm runs too), :601's
    `is_image_url(url)` on the NEW url, and the bodiless-404 GET the
    module docstring describes for `make_image_sizes`'s source-url retry.
    No GET is ever made against the old url -- :410's call is HEAD-only and
    its result (POST_TYPE_IMAGE) is never read again once the type is
    overwritten by the new url's own is_image_url check at :601.
    """
    s = _seed(url='https://old.example/a.png')
    old = make_domain('old.example')
    old.post_count = 5
    new = make_domain('new.example')
    new.post_count = 2
    db.session.commit()
    assert len({old.id, new.id}) == 2

    http_mock.head('https://old.example/a.png').respond(200, headers={'Content-Type': 'image/png'})
    http_mock.head('https://new.example/b.png').respond(200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://new.example/b.png').respond(404)

    edit_post(_api_input(url='https://new.example/b.png'), s.post, POST_TYPE_LINK,
              SRC_API, user=s.user, from_scratch=False)

    db.session.expire(old)
    db.session.expire(new)
    assert old.post_count == 4
    assert new.post_count == 3
    assert s.post.domain_id == new.id


def test_an_admin_of_a_notify_admins_domain_is_notified(db_session, http_mock):
    """:590-598. Site.admins() matches a role named ROLE_ADMIN_NAME (D481),
    which is what _make_admin arranges."""
    s = _seed(domain_name='suspicious.example', notify_admins=True)
    admin = make_user(s.instance, 'admin', local=True)
    _make_admin(admin)

    http_mock.head('https://suspicious.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://suspicious.example/pic.png').respond(404)

    edit_post(_api_input(url='https://suspicious.example/pic.png'), s.post,
              POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=True)

    assert Notification.query.filter_by(user_id=admin.id).count() == 1


def test_no_admin_is_notified_when_the_domain_does_not_ask(db_session, http_mock):
    """:590, false arm. THE ADMIN MUST BE SEEDED FOR THIS TO SAY ANYTHING.

    An earlier version of this test seeded no admin, so `Site.admins()`
    returned `[]` and `Notification.query.count() == 0` held whichever way
    `:590` went -- measured: mutating `:590` to `if True:` left it passing.
    Its moderator sibling `test_a_moderator_is_not_notified_when_the_domain_
    does_not_ask` (`:580`'s false arm) has always seeded its moderator, which
    is why that one did kill the equivalent mutant.
    """
    s = _seed(domain_name='quiet.example', notify_admins=False)
    admin = make_user(s.instance, 'admin', local=True)
    _make_admin(admin)

    http_mock.head('https://quiet.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://quiet.example/pic.png').respond(404)

    edit_post(_api_input(url='https://quiet.example/pic.png'), s.post,
              POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=True)

    assert Notification.query.filter_by(user_id=admin.id).count() == 0
    assert Notification.query.count() == 0


def test_a_user_who_is_both_moderator_and_admin_is_notified_once(db_session, http_mock):
    """:589 and :592, the dedup. THIS TEST IS ONLY REACHABLE BECAUSE D287 IS
    FIXED: before that fix, :582 raised for every moderator, so :589 never ran
    and already_notified was always empty when :592 read it.

    That is the real reason Task 4 lands before Task 6 -- not, as the spec's
    section 4.1 said, because D286's test needed it. D286's test reaches :577
    through the admin loop with D287 unfixed, because the dict at :573-579 is
    built before both loops.
    """
    s = _seed(domain_name='suspicious.example', notify_mods=True, notify_admins=True)
    both = make_user(s.instance, 'both', local=True)
    make_community_member(both, s.community, is_moderator=True)
    _make_admin(both)

    http_mock.head('https://suspicious.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://suspicious.example/pic.png').respond(404)

    edit_post(_api_input(url='https://suspicious.example/pic.png'), s.post,
              POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=True)

    assert Notification.query.filter_by(user_id=both.id).count() == 1


def test_a_moderator_and_a_separate_admin_are_both_notified(db_session, http_mock):
    """:592, the true arm -- `admin.id not in already_notified`. Two distinct
    people, so the dedup must NOT suppress the second."""
    s = _seed(domain_name='suspicious.example', notify_mods=True, notify_admins=True)
    moderator = make_user(s.instance, 'mod', local=True)
    make_community_member(moderator, s.community, is_moderator=True)
    admin = make_user(s.instance, 'admin', local=True)
    _make_admin(admin)
    assert len({s.user.id, moderator.id, admin.id}) == 3

    http_mock.head('https://suspicious.example/pic.png').respond(
        200, headers={'Content-Type': 'image/png'})
    http_mock.get('https://suspicious.example/pic.png').respond(404)

    edit_post(_api_input(url='https://suspicious.example/pic.png'), s.post,
              POST_TYPE_LINK, SRC_API, user=s.user, from_scratch=True)

    assert Notification.query.filter_by(user_id=moderator.id).count() == 1
    assert Notification.query.filter_by(user_id=admin.id).count() == 1
