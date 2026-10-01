"""D1397: a peer's array element read as an object without checking that it is one.

`'type' in json_tag and json_tag['type'] == 'Mention'` is how this codebase reads
an element of a peer's array. Over a STRING element `in` is a substring test and
the subscript raises:

    PROBE  'type' in 'https://host/u/prototype'   ->  True
           'https://host/u/prototype'['type']     ->  TypeError: string indices
                                                      must be integers
           'type' in 'https://x/u/typewriter'     ->  True
           'type' in 'https://x/u/stereotype'     ->  True
           'type' in 'https://x.test/@type'       ->  True

The arrays themselves are `isinstance(..., list)` checked at every site. Their
elements were not, at all of them. An AST sweep over `app/` -- printed whole, not
piped through `head` (fact 820) -- found 41 sites of the shape "`'k' in X` with
`X['k']` in the same function, where X is a loop variable and nothing in that
function proves X is a dict". The peer-facing ones are repaired together here,
through one helper:

    app/models.py           _as_dict(value)  ->  value if isinstance(value, dict) else {}

beside `_as_text`, `_as_int`, `_as_float` and `parse_ap_timestamp`, which are the
same family: one coercion of one untrusted value, in one place, so the readers
after it need no second guard.

THE ANCHOR IS NOT AN ARRAY ELEMENT. `find_community` reassigns

    rj = request_json['object'] if 'object' in request_json else request_json

eleven lines after the `rjs` list takes the same value only when
`isinstance(..., dict)`. So the reassignment could hold anything:

    PROBE  find_community({'type': 'Create', 'object': 'https://peer.test/p/1'})
             AttributeError: 'str' object has no attribute 'get'
           ... 'object': 'https://peer.test/inReplyTo/1'
             TypeError: string indices must be integers   (the substring test)
           ... 'object': 42   /   None
             TypeError: argument of type 'int' is not iterable
           ... 'object': ['a']
             AttributeError: 'list' object has no attribute 'get'

A string `object` is the ordinary shape, not a crafted one: Lemmy's `Add` and
`Remove` name their object by url, and `app/activitypub/routes.py:1428` and
`:1501` hand this function the whole activity whenever one arrives unannounced.

tests/test_ap_find_community.py already recorded this, as a gap it could not
close: "a non-dict 'object' value also reaches the unguarded `rj = ...`
reassignment a few lines later, which has no isinstance guard of its own, so any
input built to isolate THIS operand crashes there regardless. Reported, not fixed,
and not synthesized into a misleading test." The reassignment is guarded now, so
that operand has its test at last -- in that file, where the claim lives.

WHERE A BARE STRING IS REAL DATA, it is accepted rather than dropped.
`retrieve_mods_and_backfill`'s `attributedTo` arm required an embedded `Person`
object, so a peer listing its moderators by url got **no moderators at all** --
silently, when the url did not contain 'type', and with a dead backfill task when
it did. Both sibling readers of a list `attributedTo`
(`app/activitypub/util.py:4472`, `:4556`) take `isinstance(a, dict)` first and a
plain string in an `elif`. That arm has the same shape now.

FACT 322 applies: one request per test where a route is involved. Nothing here
reaches the network -- every function is called directly.
"""
import contextlib

import pytest

from app import db
from app.models import CommunityMember, Post, PollChoice, _as_dict, utcnow
from tests.factories import (make_community, make_instance, make_poll, make_post,
                             make_post_reply, make_site, make_user,
                             seed_community_owner)

PEER = 'peer.test'


class _RedisLockOnlyDouble:
    """`app.redis_client` stand-in covering only `.lock(...)`.

    `update_post_from_activity` and `update_post_reply_from_activity` open with
    `redis_client.lock(...)`, which the shared `redis_double` cannot serve:
    fakeredis without lupa has no Lua scripting and redis-py's `Lock.release()`
    issues an EVALSHA. The FOURTH copy of this shape, and a local one for the
    reason tests/test_ap_update_post_tails.py:88-108 sets out at length -- it has
    no condition to get wrong, and a broken double fails every test in its own
    file loudly.
    """

    def lock(self, *args, **kwargs):
        return contextlib.nullcontext()


@pytest.fixture
def redis_lock_only_double(monkeypatch):
    monkeypatch.setattr('app.redis_client', _RedisLockOnlyDouble())

# Actor urls a peer can publish that contain the key being looked for, so that
# `'type' in element` is True and the subscript is reached. None is contrived:
# 'prototype' and 'stereotype' are ordinary words, and '/type/' an ordinary path.
URLS_CONTAINING_TYPE = [
    'https://peer.test/u/prototype',
    'https://peer.test/u/typewriter',
    'https://peer.test/users/stereotype',
    'https://peer.test/type/1',
    'https://peer.test/@type',
]

# The same urls with no 'type' in them. These took the SILENT arm: no crash, and
# nothing read either.
URLS_WITHOUT_TYPE = [
    'https://peer.test/u/alice',
    'https://peer.test/c/books',
]

# Values for which `'anything' in value` RAISES rather than answering False. These
# are what make a row discriminating whatever keys the site under test reads: a
# string element only reaches the subscript when the key happens to be a substring
# of it, and the first draft of this file used urls containing 'type' against loops
# that read 'href', 'url' and 'name' -- so eleven mutants survived, one per site
# whose loop the rows never actually entered.
NOT_ITERABLE = [42, 0, None, True, 3.5]

# Iterable but not a mapping: `'k' in value` answers False, so these reach no
# subscript and prove only that the loop survives them.
ITERABLE_NOT_MAPPING = [['nested'], (), 'https://peer.test/u/alice']

NOT_OBJECTS = URLS_CONTAINING_TYPE + URLS_WITHOUT_TYPE + NOT_ITERABLE + [
    ['nested'], ()]

# Urls containing the keys the attachment loops read, so a string element reaches
# each of those subscripts too.
URLS_CONTAINING_ATTACHMENT_KEYS = [
    'https://peer.test/href/1',
    'https://peer.test/url/1',
    'https://peer.test/name/1',
    'https://peer.test/type/1',
]

# Everything an attachment element can be that is not an object, with the values
# that reach each of the four keys the loops read.
BAD_ATTACHMENTS = URLS_CONTAINING_ATTACHMENT_KEYS + NOT_ITERABLE + [['nested'], ()]

# Likewise for the vote loops, which read 'name', 'replies' and 'totalItems'.
BAD_VOTES = ['https://peer.test/name/1', 'https://peer.test/replies/1'] + \
    NOT_ITERABLE + [['nested'], ()]


# --------------------------------------------------------------------------
# The helper
# --------------------------------------------------------------------------


class TestTheCoercion:
    def test_a_dict_is_itself(self):
        value = {'type': 'Mention', 'href': 'https://peer.test/u/alice'}
        assert _as_dict(value) is value

    @pytest.mark.parametrize('value', NOT_OBJECTS)
    def test_anything_else_is_an_empty_dict(self, value):
        assert _as_dict(value) == {}

    @pytest.mark.parametrize('value', NOT_OBJECTS)
    def test_the_result_answers_every_read_the_callers_make(self, value):
        """Why `{}` and not None: each caller goes straight on to `'k' in x` and
        `x['k']` or `x.get('k')`, and an empty dict answers all three without a
        second guard."""
        coerced = _as_dict(value)
        assert ('type' in coerced) is False
        assert coerced.get('type') is None

    def test_an_empty_dict_is_not_replaced(self):
        """`{}` is falsy, so a coercion written as `value or {}` would be
        indistinguishable -- but it would also swallow a legitimately empty
        object. The test is the type, not the truth."""
        value = {}
        assert _as_dict(value) is value


# --------------------------------------------------------------------------
# find_community -- the anchor
# --------------------------------------------------------------------------


class TestFindCommunitysObject:
    @pytest.mark.parametrize('obj', NOT_OBJECTS)
    def test_an_object_that_is_not_one_answers_none(self, app, db_session, obj):
        """Every one of these raised out of `find_community` before the repair,
        in three different ways depending on the value."""
        from app.activitypub.util import find_community
        seed_community_owner()
        make_community('somewhere')

        assert find_community({'type': 'Create', 'object': obj}) is None

    def test_a_string_object_does_not_stop_the_outer_addressing_from_matching(
            self, app, db_session):
        """The behaviour that has to survive: the addressing loop already read the
        OUTER activity, and a string `object` must not take that away.

        This is the row the repair makes possible. It is an `Add`, the activity
        shape that carries a string object in ordinary federation, addressed to a
        community the way a real one is.
        """
        from app.activitypub.util import find_community
        seed_community_owner()
        community = make_community('addressed')

        result = find_community({'type': 'Add',
                                 'object': 'https://peer.test/u/alice',
                                 'audience': community.ap_profile_id})

        assert result == community

    def test_a_dict_object_still_supplies_in_reply_to(self, app, db_session):
        """The control for the reassignment itself: when `object` IS a dict, `rj`
        must still become it, or the inReplyTo strategy stops working."""
        from app.activitypub.util import find_community
        seed_community_owner()
        community = make_community('parenthome')
        author = make_user(make_instance(PEER), 'author')
        parent = make_post(community, author, ap_id=f'https://{PEER}/objects/1')
        db.session.commit()

        result = find_community({'type': 'Create',
                                 'object': {'inReplyTo': parent.ap_id}})

        assert result == community

    def test_an_activity_with_no_object_still_reads_its_own_in_reply_to(
            self, app, db_session):
        """The `else` arm the repair now shares with the non-dict case: fall back
        to the outer activity, which is what it always did for an activity with no
        `object` at all."""
        from app.activitypub.util import find_community
        seed_community_owner()
        community = make_community('parenthome')
        author = make_user(make_instance(PEER), 'author')
        parent = make_post(community, author, ap_id=f'https://{PEER}/objects/1')
        db.session.commit()

        assert find_community({'inReplyTo': parent.ap_id}) == community

    @pytest.mark.parametrize('element', NOT_OBJECTS)
    def test_a_video_attributed_to_something_that_is_not_an_object(
            self, app, db_session, element):
        """The Video strategy's `elif a['type'] == 'Group'`. The `isinstance(a, str)`
        arm above it took the strings; everything else reached the subscript."""
        from app.activitypub.util import find_community
        seed_community_owner()
        make_community('videohome')

        result = find_community({'type': 'Video', 'attributedTo': [element]})

        assert result is None

    def test_a_video_attributed_to_a_group_object_still_matches(self, app,
                                                               db_session):
        """The control. A fix that stopped reading `attributedTo` at all would
        pass every row above."""
        from app.activitypub.util import find_community
        seed_community_owner()
        community = make_community('videohome')

        result = find_community({'type': 'Video', 'attributedTo': [
            {'type': 'Group', 'id': community.ap_profile_id}]})

        assert result == community

    def test_a_group_object_whose_id_is_not_a_string(self, app, db_session):
        """`a['id'].lower()` -- the second read of the same element, and an
        `AttributeError` for any id that is not a string."""
        from app.activitypub.util import find_community
        seed_community_owner()
        make_community('videohome')

        result = find_community({'type': 'Video', 'attributedTo': [
            {'type': 'Group', 'id': 42}]})

        assert result is None


# --------------------------------------------------------------------------
# The `tag` array, at all four of its readers
# --------------------------------------------------------------------------


@pytest.fixture
def ingest(app, db_session, redis_lock_only_double):
    """A remote community on PEER with one post and one reply, for the functions
    that take an already-loaded row plus the peer's dict."""
    from types import SimpleNamespace
    make_site()
    instance = make_instance(PEER, software='lemmy')
    make_user(instance, 'community_owner')
    community = make_community(host=PEER)
    community.ap_fetched_at = utcnow()
    author = make_user(instance, 'author')
    post = make_post(community, author, ap_id=f'https://{PEER}/objects/1')
    reply = make_post_reply(post, author, body='a reply')
    # `make_post_reply` takes no ap_id; the reply readers below look the row up by
    # nothing, but `update_post_reply_from_activity` writes `ap_updated` and the
    # row should still look remote.
    reply.ap_id = f'https://{PEER}/comments/1'
    db.session.commit()
    return SimpleNamespace(community=community, author=author, post=post,
                           reply=reply, instance=instance)


class TestTheTagArray:
    @pytest.mark.parametrize('element', URLS_CONTAINING_TYPE)
    def test_updating_a_post_with_a_string_tag(self, ingest, element):
        """`update_post_from_activity` reads `tag` three times -- Hashtag,
        lemmy:CommunityTag, Mention -- so one string element reached the subscript
        whichever of the three came first."""
        from app.activitypub.util import update_post_from_activity

        update_post_from_activity(ingest.post, {'object': {
            'id': ingest.post.ap_id, 'name': 'a new title',
            'tag': [element, element]}})

        assert ingest.post.title == 'a new title'

    @pytest.mark.parametrize('element', URLS_CONTAINING_TYPE)
    def test_updating_a_reply_with_a_string_tag(self, ingest, element):
        """The reply twin. Its guard is `len(...) > 1`, so two elements are
        needed to reach the loop at all -- which is why both rows send two."""
        from app.activitypub.util import update_post_reply_from_activity

        update_post_reply_from_activity(ingest.reply, {'object': {
            'id': ingest.reply.ap_id, 'content': '<p>edited</p>',
            'tag': [element, element]}})

        assert 'edited' in ingest.reply.body_html

    def test_a_real_mention_is_still_read_past_a_string_tag(self, ingest):
        """The control that matters most: coercing an element must not stop the
        loop, or a peer could suppress every Mention notification by prefixing
        one junk entry.
        """
        from app.activitypub.util import update_post_from_activity
        local = make_user(db.session.get(type(ingest.instance), 1), 'localreader',
                          local=True)
        local.ap_profile_id = f'https://test.piefed.local/u/{local.user_name}'
        db.session.commit()

        update_post_from_activity(ingest.post, {'object': {
            'id': ingest.post.ap_id, 'name': 'a new title',
            'tag': ['https://peer.test/u/prototype',
                    {'type': 'Hashtag', 'name': '#solarstorm'}]}})

        assert [tag.name for tag in ingest.post.tags] == ['solarstorm']


# --------------------------------------------------------------------------
# The `attachment` array
# --------------------------------------------------------------------------


# An attachment element 0 that IS a typed object, so the pre-check passes and the
# loop is entered. Without one in front, every row below would exercise only the
# pre-check -- which is how the first draft of this file left four mutants alive
# inside loops its rows never entered.
A_TYPED_ELEMENT = {'type': 'Note'}


class TestTheAttachmentArray:
    @pytest.mark.parametrize('element', BAD_ATTACHMENTS)
    def test_the_pre_check_survives_an_element_zero_that_is_not_an_object(
            self, ingest, element):
        """`'type' in request_json['object']['attachment'][0]` -- a membership test
        over whatever element 0 is, so a number was
        `TypeError: argument of type 'int' is not iterable` and a string
        containing 'type' passed it and crashed in the loop instead."""
        from app.activitypub.util import update_post_from_activity

        update_post_from_activity(ingest.post, {'object': {
            'id': ingest.post.ap_id, 'name': 'a new title',
            'attachment': [element]}})

        assert ingest.post.title == 'a new title'

    @pytest.mark.parametrize('element', BAD_ATTACHMENTS)
    def test_a_later_element_that_is_not_an_object(self, ingest, element):
        """The loop body, reached by putting a typed object in front. The loop
        reads `href`, `url` and `name`, so the strings here are urls containing
        each of those -- a url containing only 'type' never reaches any of these
        subscripts, which is why it proves nothing about this loop."""
        from app.activitypub.util import update_post_from_activity

        update_post_from_activity(ingest.post, {'object': {
            'id': ingest.post.ap_id, 'name': 'a new title',
            'attachment': [A_TYPED_ELEMENT, element]}})

        assert ingest.post.title == 'a new title'

    def test_a_dict_attachment_with_no_type_is_skipped_not_a_crash(self, ingest):
        from app.activitypub.util import update_post_from_activity

        update_post_from_activity(ingest.post, {'object': {
            'id': ingest.post.ap_id, 'name': 'a new title',
            'attachment': [{'href': 'https://peer.test/pic.png'}]}})

        assert ingest.post.title == 'a new title'

    def test_a_real_link_attachment_still_sets_the_url(self, ingest, http_mock):
        """The control. `http_mock` because a changed url is HEADed for its
        content type."""
        from app.activitypub.util import update_post_from_activity
        http_mock.head('https://example.test/an-article').respond(
            200, headers={'Content-Type': 'text/html'})

        update_post_from_activity(ingest.post, {'object': {
            'id': ingest.post.ap_id, 'name': 'a new title',
            'attachment': [{'type': 'Link',
                            'href': 'https://example.test/an-article'}]}})

        assert ingest.post.url == 'https://example.test/an-article'

    def test_a_junk_first_element_no_longer_hides_the_attachments_behind_it(
            self, ingest, http_mock):
        """R202, fixed (owner ruling): the pre-check was `'type' in <element 0>`
        -- ONE element deciding whether the loop ran at all -- so a peer could
        suppress a post's url by prefixing one entry that is not a typed object.
        The pre-check now scans every element, so the Link behind the junk entry
        is found. `http_mock` because a changed url is HEADed for its content
        type.
        """
        from app.activitypub.util import update_post_from_activity
        http_mock.head('https://example.test/an-article').respond(
            200, headers={'Content-Type': 'text/html'})

        update_post_from_activity(ingest.post, {'object': {
            'id': ingest.post.ap_id, 'name': 'a new title',
            'attachment': ['https://peer.test/type/1',
                           {'type': 'Link',
                            'href': 'https://example.test/an-article'}]}})

        assert ingest.post.title == 'a new title'
        assert ingest.post.url == 'https://example.test/an-article'

    def test_an_image_behind_a_junk_first_element_does_not_crash_the_alt_text_read(
            self, ingest, http_mock):
        """R202's consequence: element 0 no longer gates the loop, so the image
        alt-text read of element 0 can now meet a non-object and must not
        subscript it."""
        from app.activitypub.util import update_post_from_activity
        http_mock.head('https://example.test/pic.png').respond(
            200, headers={'Content-Type': 'image/png'})

        update_post_from_activity(ingest.post, {'object': {
            'id': ingest.post.ap_id, 'name': 'a new title',
            'attachment': [42, {'type': 'Image', 'url': 'https://example.test/pic.png'}]}})

        assert ingest.post.url == 'https://example.test/pic.png'

    def test_a_single_dict_attachment_with_no_url(self, ingest):
        """The Mastodon / a.gup.pe arm, which read `['url']` outright."""
        from app.activitypub.util import update_post_from_activity

        update_post_from_activity(ingest.post, {'object': {
            'id': ingest.post.ap_id, 'name': 'a new title',
            'attachment': {'type': 'Document'}}})

        assert ingest.post.title == 'a new title'

    @pytest.mark.parametrize('element', BAD_ATTACHMENTS)
    def test_a_replys_attachment_that_is_not_an_object(self, ingest, element):
        """The reply twin of the loop, which has no pre-check in front of it and
        reads `href`, `url` and `name`. `create_post_reply` holds a third copy,
        covered in TestCreatingAReply below."""
        from app.activitypub.util import update_post_reply_from_activity

        update_post_reply_from_activity(ingest.reply, {'object': {
            'id': ingest.reply.ap_id, 'content': '<p>edited</p>',
            'attachment': [element]}})

        assert 'edited' in ingest.reply.body_html

    @pytest.mark.parametrize('element', BAD_ATTACHMENTS)
    def test_a_replys_attachment_reached_through_the_image_loop(self, ingest,
                                                              element):
        """The post function's LAST attachment loop -- 'check for image posts' --
        runs only when nothing before it produced a url, so it needs a typed
        element that matches none of the earlier arms in front of the junk."""
        from app.activitypub.util import update_post_from_activity

        update_post_from_activity(ingest.post, {'object': {
            'id': ingest.post.ap_id, 'name': 'a new title',
            'attachment': [A_TYPED_ELEMENT, element]}})

        assert ingest.post.title == 'a new title'
        assert ingest.post.url is None


# --------------------------------------------------------------------------
# Poll votes
# --------------------------------------------------------------------------


class TestPollVotes:
    """Every object here carries `type: 'Question'`.

    Without it `update_post_from_activity` never reaches the vote loop at all --
    the branch is `request_json['object'].get('type') == 'Question'` -- so a row
    asserting "the update did not crash" would pass having exercised nothing. The
    first draft of this class omitted it and passed vacuously (fact 861's shape,
    a second time).

    A `Poll` row is created first for the same reason: the branch EDITS an
    existing poll, and with none it returns having written nothing.
    """

    def _question(self, post, **fields):
        object_fields = {'id': post.ap_id, 'type': 'Question',
                         'name': 'a new title',
                         'endTime': '2050-01-01T00:00:00Z'}
        object_fields.update(fields)
        return {'object': object_fields}

    @pytest.mark.parametrize('element', BAD_VOTES)
    def test_a_vote_entry_that_is_not_an_object(self, ingest, element):
        """`'name' in vote` over a string element is a substring test and
        `vote['name']` a TypeError, so one junk choice took the whole edit."""
        from app.activitypub.util import update_post_from_activity
        make_poll(ingest.post)

        update_post_from_activity(ingest.post,
                                  self._question(ingest.post, oneOf=[element]))

        assert ingest.post.title == 'a new title'
        assert PollChoice.query.filter_by(post_id=ingest.post.id).count() == 0

    @pytest.mark.parametrize('replies', NOT_ITERABLE)
    def test_a_vote_whose_replies_is_not_an_object(self, ingest, replies):
        """`vote['replies']['totalItems']` -- the second read of the same entry,
        and the one the sweep found only by looking at subscripts OF a loop
        variable rather than at the variable itself.

        The values are the ones for which `'totalItems' in replies` RAISES. A
        string `replies` answers False instead and the entry is skipped, so it
        proves nothing here -- the first draft used one and left this mutant
        alive.
        """
        from app.activitypub.util import update_post_from_activity
        make_poll(ingest.post)

        update_post_from_activity(ingest.post, self._question(
            ingest.post, oneOf=[{'name': 'yes', 'replies': replies}]))

        assert ingest.post.title == 'a new title'
        # 'yes' survives as a choice: a vote with no usable `replies` counts zero
        # votes, which is the "Edit, not a totals update" arm.
        assert [c.choice_text for c in PollChoice.query.filter_by(
            post_id=ingest.post.id).all()] == ['yes']

    @pytest.mark.parametrize('replies', NOT_ITERABLE)
    def test_a_totals_update_whose_replies_is_not_an_object(self, ingest,
                                                           replies):
        """The SECOND copy of the same read, in the totals loop -- reached only
        when some other entry reports a non-zero count, so a real one is sent
        alongside."""
        from app.activitypub.util import update_post_from_activity
        from tests.factories import make_poll_choice
        make_poll(ingest.post)
        make_poll_choice(ingest.post, 'yes', sort_order=1)
        make_poll_choice(ingest.post, 'no', sort_order=2)

        update_post_from_activity(ingest.post, self._question(
            ingest.post, oneOf=[{'name': 'yes', 'replies': {'totalItems': 4}},
                                {'name': 'no', 'replies': replies}]))

        counts = {c.choice_text: c.num_votes
                  for c in PollChoice.query.filter_by(post_id=ingest.post.id)}
        assert counts == {'yes': 4, 'no': 0}

    @pytest.mark.parametrize('element', BAD_VOTES)
    def test_a_totals_update_past_an_entry_that_is_not_an_object(self, ingest,
                                                                element):
        """The totals loop's own coercion of the ENTRY, as distinct from its
        `replies`."""
        from app.activitypub.util import update_post_from_activity
        from tests.factories import make_poll_choice
        make_poll(ingest.post)
        make_poll_choice(ingest.post, 'yes', sort_order=1)

        update_post_from_activity(ingest.post, self._question(
            ingest.post, oneOf=[{'name': 'yes', 'replies': {'totalItems': 4}},
                                element]))

        counts = {c.choice_text: c.num_votes
                  for c in PollChoice.query.filter_by(post_id=ingest.post.id)}
        assert counts == {'yes': 4}

    def test_a_real_poll_is_still_built_past_a_junk_entry(self, ingest):
        """The control: the choices have to survive the coercion, and the loop
        must not stop at the entry it cannot use."""
        from app.activitypub.util import update_post_from_activity
        make_poll(ingest.post)

        update_post_from_activity(ingest.post, self._question(
            ingest.post,
            oneOf=['https://peer.test/type/1', {'name': 'yes'}, {'name': 'no'}]))

        choices = PollChoice.query.filter_by(post_id=ingest.post.id).order_by(
            PollChoice.sort_order).all()
        assert [choice.choice_text for choice in choices] == ['yes', 'no']

    def test_a_totals_update_past_a_junk_entry(self, ingest):
        """The other arm of the same loop -- the one that runs when the peer sends
        non-zero totals -- reached twice more in the function and coerced at both.
        """
        from app.activitypub.util import update_post_from_activity
        from tests.factories import make_poll_choice
        make_poll(ingest.post)
        make_poll_choice(ingest.post, 'yes', sort_order=1)
        make_poll_choice(ingest.post, 'no', sort_order=2)

        update_post_from_activity(ingest.post, self._question(
            ingest.post, oneOf=[
                'https://peer.test/type/1',
                {'name': 'yes', 'replies': {'totalItems': 7}},
                {'name': 'no', 'replies': {'totalItems': 2}}]))

        counts = {c.choice_text: c.num_votes
                  for c in PollChoice.query.filter_by(post_id=ingest.post.id)}
        assert counts == {'yes': 7, 'no': 2}


# --------------------------------------------------------------------------
# Post.new's two readers
# --------------------------------------------------------------------------


class TestPostNew:
    def _activity(self, community, author, **object_fields):
        fields = {'id': f'https://{PEER}/objects/99', 'type': 'Page',
                  'attributedTo': author.ap_profile_id,
                  'audience': community.ap_profile_id,
                  'name': 'an ingested post'}
        fields.update(object_fields)
        return {'id': f'https://{PEER}/activities/99', 'type': 'Create',
                'actor': author.ap_profile_id, 'object': fields}

    @pytest.mark.parametrize('element', URLS_CONTAINING_TYPE)
    def test_a_string_tag_does_not_stop_the_post_being_created(self, ingest,
                                                              element):
        """This loop runs with the Post already added to the session, so the
        TypeError took a half-written row with it."""
        post = Post.new(ingest.author, ingest.community,
                        self._activity(ingest.community, ingest.author,
                                       tag=[element]))

        assert post is not None
        assert post.title == 'an ingested post'

    @pytest.mark.parametrize('element', NOT_OBJECTS)
    def test_an_attachment_entry_that_is_not_an_object(self, ingest, element):
        """`attachment_item['type']` with no membership test at all, so a dict
        without `type` was a KeyError as well as a string being a TypeError."""
        post = Post.new(ingest.author, ingest.community,
                        self._activity(ingest.community, ingest.author,
                                       attachment=[element]))

        assert post is not None
        assert post.url is None

    def test_a_real_link_attachment_still_sets_the_url(self, ingest, http_mock):
        """`http_mock` because `Post.new` HEADs a url it is given, to learn the
        content type -- the only outbound call in this file."""
        http_mock.head('https://example.test/a').respond(
            200, headers={'Content-Type': 'text/html'})

        post = Post.new(ingest.author, ingest.community,
                        self._activity(ingest.community, ingest.author,
                                       attachment=[{'type': 'Link',
                                                    'href': 'https://example.test/a'}]))

        assert post.url == 'https://example.test/a'

    def test_a_link_after_a_junk_entry_is_found(self, ingest, http_mock):
        """R202, fixed (owner ruling): `Post.new`'s attachment pre-check scans
        every element, as the Update twin's now does, so a junk first entry no
        longer hides the Link behind it."""
        http_mock.head('https://example.test/a').respond(
            200, headers={'Content-Type': 'text/html'})

        post = Post.new(ingest.author, ingest.community,
                        self._activity(ingest.community, ingest.author,
                                       attachment=[42, {'type': 'Link',
                                                        'href': 'https://example.test/a'}]))

        assert post.url == 'https://example.test/a'

    @pytest.mark.parametrize('element', BAD_ATTACHMENTS)
    def test_an_events_attachment_entry_that_is_not_an_object(self, ingest,
                                                             element):
        """`Post.new`'s SECOND attachment loop, inside the Event branch, which is
        where Mobilizon puts a link. It has no element-0 pre-check in front of it,
        so a single junk entry reaches the subscript directly -- and it is reached
        only by an object of type `Event` carrying a `startTime`, which is why the
        Page rows above left this one unproven.
        """
        post = Post.new(ingest.author, ingest.community,
                        self._activity(ingest.community, ingest.author,
                                       type='Event',
                                       startTime='2050-01-01T00:00:00Z',
                                       attachment=[element]))

        assert post is not None
        assert post.url is None

    def test_an_events_real_link_attachment_still_sets_the_url(self, ingest,
                                                              http_mock):
        """The control for that loop.

        `http_mock` here and NOT in the junk-only row above it, which measurement
        decided rather than guesswork. The HEAD comes from the FIRST attachment
        block, the one gated on some element being a typed object -- so a `Link`
        reaches it, and an all-junk list skips the whole block and makes no
        outbound call. respx's `assert_all_called` caught each mistake in turn:
        the route unused when the element was junk, then the request unmocked when
        it was not.
        """
        http_mock.head('https://example.test/an-event').respond(
            200, headers={'Content-Type': 'text/html'})

        post = Post.new(ingest.author, ingest.community,
                        self._activity(ingest.community, ingest.author,
                                       type='Event',
                                       startTime='2050-01-01T00:00:00Z',
                                       attachment=[{'type': 'Link',
                                                    'href': 'https://example.test/an-event'}]))

        assert post.url == 'https://example.test/an-event'

    def test_an_events_link_after_a_junk_entry_is_still_found(self, ingest, http_mock):
        """This loop has no element-0 pre-check. Since R202 the first block's
        pre-check scans every element too, so it now reaches the Link as well and
        HEADs it, which is why `http_mock` is here."""
        http_mock.head('https://example.test/an-event').respond(
            200, headers={'Content-Type': 'text/html'})
        post = Post.new(ingest.author, ingest.community,
                        self._activity(ingest.community, ingest.author,
                                       type='Event',
                                       startTime='2050-01-01T00:00:00Z',
                                       attachment=[42,
                                                   {'type': 'Link',
                                                    'href': 'https://example.test/an-event'}]))

        assert post.url == 'https://example.test/an-event'


# --------------------------------------------------------------------------
# create_post_reply -- the third copy of the attachment loop and the tag loop
# --------------------------------------------------------------------------


class TestCreatingAReply:
    """`create_post_reply` holds its own copies of the attachment loop and the tag
    loop, and no row of the first draft of this file called it -- so both of its
    coercions were unproven. Driven the way tests/test_ap_create_reply.py drives
    it: the envelope carries `id`, and the object must carry `to` Public or the
    visibility guard refuses before any of this runs.
    """

    def _doc(self, **fields):
        obj = {'id': f'https://{PEER}/comments/99', 'type': 'Note',
               'to': ['https://www.w3.org/ns/activitystreams#Public'], 'cc': [],
               'content': '<p>a reply</p>'}
        obj.update(fields)
        return {'id': f'https://{PEER}/activities/create/99', 'type': 'Create',
                'object': obj}

    @pytest.fixture
    def replier(self, ingest):
        """A SECOND remote account.

        `create_post_reply` refuses a duplicate -- `ap_log` message
        'Duplicate reply' -- and the shared `ingest` fixture already holds one
        reply under this post by `ingest.author`. Every row of this class returned
        None for that reason until the replier was separated from the author, which
        is a refusal that looks exactly like the crash the rows are about.
        """
        user = make_user(ingest.instance, 'replier')
        db.session.commit()
        return user

    def _create(self, ingest, replier, **fields):
        from app.activitypub.util import create_post_reply
        return create_post_reply(store_ap_json=False,
                                 community=ingest.community,
                                 in_reply_to=ingest.post.ap_id,
                                 request_json=self._doc(**fields),
                                 user=replier)

    @pytest.mark.parametrize('element', BAD_ATTACHMENTS)
    def test_an_attachment_that_is_not_an_object(self, ingest, replier, element):
        reply = self._create(ingest, replier, attachment=[element])

        assert reply is not None
        assert 'a reply' in reply.body_html

    @pytest.mark.parametrize('element', URLS_CONTAINING_TYPE + NOT_ITERABLE)
    def test_a_tag_that_is_not_an_object(self, ingest, replier, element):
        """The tag loop's guard is `len(...) > 1`, so two entries are needed to
        reach it."""
        reply = self._create(ingest, replier, tag=[element, element])

        assert reply is not None
        assert 'a reply' in reply.body_html

    def test_a_real_attachment_is_still_appended_to_the_body(self, ingest,
                                                            replier):
        """The control for the attachment loop: it rewrites the body, so a
        coercion that dropped every element would be invisible without this."""
        reply = self._create(ingest, replier, attachment=[
            {'type': 'Image', 'url': 'https://peer.test/pic.png',
             'name': 'a picture'}])

        assert '![a picture](https://peer.test/pic.png)' in reply.body


# --------------------------------------------------------------------------
# retrieve_mods_and_backfill's attributedTo, where a string IS the data
# --------------------------------------------------------------------------


class TestModeratorsFromAttributedTo:
    @pytest.fixture
    def remote(self, app, db_session):
        """A remote community with no `ap_moderators_url`, which is what sends
        `retrieve_mods_and_backfill` down the `attributedTo` arm -- and is itself
        the consequence of a list `attributedTo`: `actor_json_to_model` only takes
        that field as a moderators url when it is a string."""
        from types import SimpleNamespace
        make_site()
        instance = make_instance(PEER, software='lemmy')
        make_user(instance, 'community_owner')
        community = make_community(host=PEER)
        community.ap_moderators_url = None
        community.ap_outbox_url = None
        alice = make_user(instance, 'alice')
        alice.ap_profile_id = f'https://{PEER}/u/alice'
        db.session.commit()
        return SimpleNamespace(community=community, alice=alice,
                               instance=instance)

    def _run(self, remote, attributed_to):
        from app.community.util import retrieve_mods_and_backfill
        retrieve_mods_and_backfill(remote.community.id, PEER,
                                   remote.community.name,
                                   {'type': 'Group', 'attributedTo': attributed_to})

    def _moderators(self, remote):
        rows = CommunityMember.query.filter_by(community_id=remote.community.id,
                                              is_moderator=True).all()
        return sorted(row.user_id for row in rows)

    def test_a_moderator_named_by_url_is_recorded(self, remote):
        """The silent half of the defect. A list of actor urls matched no entry,
        so a community whose peer lists its moderators that way had none here --
        and the two sibling readers of a list `attributedTo` both accept a plain
        string."""
        self._run(remote, [f'https://{PEER}/u/alice'])

        assert self._moderators(remote) == [remote.alice.id]

    def test_a_moderator_named_by_an_embedded_person_is_still_recorded(self,
                                                                     remote):
        """The arm that already worked, kept working."""
        self._run(remote, [{'type': 'Person', 'id': f'https://{PEER}/u/alice'}])

        assert self._moderators(remote) == [remote.alice.id]

    @pytest.mark.parametrize('element', [42, None, True, ['nested'], {},
                                         {'type': 'Person'},
                                         {'type': 'Group',
                                          'id': f'https://{PEER}/c/books'},
                                         {'id': f'https://{PEER}/u/alice'}])
    def test_an_entry_that_names_no_person_records_nobody(self, remote, element):
        """`{'type': 'Person'}` with no `id` was an `AttributeError` inside
        `find_actor_or_create`, whose dict arm is `actor['id']`. A `Group` entry
        is not a moderator, and an entry with an `id` but no `type` is not one
        either -- the codebase's sibling readers require the type."""
        self._run(remote, [element])

        assert self._moderators(remote) == []

    @pytest.mark.parametrize('element', URLS_CONTAINING_TYPE)
    def test_a_url_containing_the_word_type_is_looked_up_as_an_actor(
            self, remote, element, monkeypatch):
        """The crashing half. These urls made `'type' in m` true and the subscript
        raised, which ended the task -- so the community was created and never
        filled in.

        Asserted on the lookup rather than on the membership rows, and
        deliberately: treating the url as an actor is the whole claim, and the
        alternative -- letting it reach the real `find_actor_or_create` -- is an
        outbound fetch of a url this peer does not host, which the suite's respx
        mock refuses by design. The local-actor rows above are what prove a
        successful lookup becomes a moderator.
        """
        seen = []
        monkeypatch.setattr('app.community.util.find_actor_or_create',
                            lambda actor, *a, **kw: seen.append(actor))

        self._run(remote, [element])

        assert seen == [element]
        assert self._moderators(remote) == []

    def test_a_real_moderator_after_a_junk_entry_is_still_recorded(self, remote):
        """The loop must not stop at the first entry it cannot use."""
        self._run(remote, [42, f'https://{PEER}/u/alice'])

        assert self._moderators(remote) == [remote.alice.id]

    def test_attributed_to_that_is_not_a_list_records_nobody(self, remote):
        """`isinstance(mods, list)` was already there and stays: a string
        `attributedTo` is a moderators URL, which `actor_json_to_model` handles,
        not a list of actors."""
        self._run(remote, f'https://{PEER}/c/books/moderators')

        assert self._moderators(remote) == []


# --------------------------------------------------------------------------
# retrieve_mods_and_backfill's other two peer arrays
# --------------------------------------------------------------------------


class TestTheModeratorsCollection:
    """The `ap_moderators_url` arm, which the class above deliberately switches
    off. `remote_object_to_json` is patched rather than mocked over HTTP: the
    question here is what the function does with the document, and the fetch has
    its own tests.
    """

    @pytest.fixture
    def remote(self, app, db_session):
        from types import SimpleNamespace
        make_site()
        instance = make_instance(PEER, software='lemmy')
        make_user(instance, 'community_owner')
        community = make_community(host=PEER)
        community.ap_moderators_url = f'https://{PEER}/c/books/moderators'
        community.ap_outbox_url = None
        alice = make_user(instance, 'alice')
        alice.ap_profile_id = f'https://{PEER}/u/alice'
        db.session.commit()
        return SimpleNamespace(community=community, alice=alice)

    def _run(self, remote, monkeypatch, document):
        from app.community.util import retrieve_mods_and_backfill
        monkeypatch.setattr('app.community.util.remote_object_to_json',
                            lambda uri: document)
        monkeypatch.setattr('app.community.util.sleep', lambda seconds: None)
        retrieve_mods_and_backfill(remote.community.id, PEER,
                                   remote.community.name, None)

    def _moderators(self, remote):
        rows = CommunityMember.query.filter_by(community_id=remote.community.id,
                                              is_moderator=True).all()
        return sorted(row.user_id for row in rows)

    def test_a_real_collection_records_its_moderators(self, remote, monkeypatch):
        """The control."""
        self._run(remote, monkeypatch, {
            'type': 'OrderedCollection',
            'orderedItems': [f'https://{PEER}/u/alice']})

        assert self._moderators(remote) == [remote.alice.id]

    @pytest.mark.parametrize('ordered_items', [
        'https://peer.test/u/alice',   # iterated its CHARACTERS, one call each
        42, None, True, {'a': 1},
    ])
    def test_ordered_items_that_is_not_a_list_records_nobody(
            self, remote, monkeypatch, ordered_items):
        """`'orderedItems' in mods_data` said only that the key was there. A string
        iterated one character at a time into `find_actor_or_create`, and a number
        was `TypeError: 'int' object is not iterable`.
        """
        seen = []
        monkeypatch.setattr('app.community.util.find_actor_or_create',
                            lambda actor, *a, **kw: seen.append(actor))

        self._run(remote, monkeypatch, {'type': 'OrderedCollection',
                                        'orderedItems': ordered_items})

        assert seen == []
        assert self._moderators(remote) == []

    @pytest.mark.parametrize('document', ['not a document', 42, ['a', 'b'], None])
    def test_a_collection_that_is_not_an_object_records_nobody(
            self, remote, monkeypatch, document):
        """`mods_data` itself: `'type' in <a string>` is a substring test, and
        `'type' in 42` is a TypeError."""
        self._run(remote, monkeypatch, document)

        assert self._moderators(remote) == []


class TestTheOutboxBackfill:
    """The `ap_outbox_url` arm, and its `orderedItems` entries. `Post.new` is not
    reached by any row here -- every document either has no usable entry or is
    refused -- so no outbound fetch is needed beyond the patched one.
    """

    @pytest.fixture
    def remote(self, app, db_session):
        from types import SimpleNamespace
        make_site()
        instance = make_instance(PEER, software='lemmy')
        make_user(instance, 'community_owner')
        community = make_community(host=PEER)
        community.ap_moderators_url = None
        community.ap_outbox_url = f'https://{PEER}/c/books/outbox'
        db.session.commit()
        return SimpleNamespace(community=community, instance=instance)

    def _run(self, remote, monkeypatch, document):
        from app.community.util import retrieve_mods_and_backfill
        monkeypatch.setattr('app.community.util.remote_object_to_json',
                            lambda uri: document)
        retrieve_mods_and_backfill(remote.community.id, PEER,
                                   remote.community.name, None)

    @pytest.mark.parametrize('element', NOT_ITERABLE + [
        'https://peer.test/object/1', 'https://peer.test/type/1'])
    def test_an_outbox_entry_that_is_not_an_object_is_skipped(
            self, remote, monkeypatch, element):
        """Every read in the loop treats the entry as an object. A string entry
        containing 'object' or 'type' reached a subscript; a number was a
        TypeError. Either ended the backfill for the whole community, and the
        `continue` a few lines down exists precisely so that one bad entry does
        not.
        """
        self._run(remote, monkeypatch, {
            'type': 'OrderedCollection',
            'totalItems': 1,
            'orderedItems': [element]})

        assert Post.query.count() == 0

    def test_an_entry_whose_object_is_not_an_object_is_skipped(self, remote,
                                                              monkeypatch):
        """`'object' in announce['object']` -- the nested read, where the outer
        entry IS a dict and the inner value is not."""
        self._run(remote, monkeypatch, {
            'type': 'OrderedCollection',
            'totalItems': 1,
            'orderedItems': [{'object': 'https://peer.test/object/1'}]})

        assert Post.query.count() == 0
