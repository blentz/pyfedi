"""refresh_community_profile_task's legacy `lemmy:tagsForPosts` loop.

This file exists because that task had no test coverage of any kind: before it
was written, no test module in tests/ called refresh_community_profile_task,
and the defect it pins (D26 in
docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md) could not be
characterised without one. It deliberately covers only the legacy flair loop
and the commit that precedes it; the rest of the task -- the actor re-fetch,
the moderators/followers/featured collections -- is out of scope and is
reported, not tested, here.

How the task is exercised
-------------------------

refresh_community_profile_task is a celery task, but it is called as a plain
function throughout: `refresh_community_profile` itself calls it inline under
`current_app.debug` and via `.apply_async` otherwise, and the app fixture puts
celery in eager mode anyway. Calling the undecorated behaviour directly is
therefore the same code path a worker runs, with no mock anywhere.

Two arrangements make that possible without outbound HTTP, and both are
choices about the DOCUMENT rather than about the code:

- `activity_json` is passed in. The task only fetches the actor document
  itself when the argument is falsy, so supplying it takes the same branch a
  caller that already has the document takes (routes.py's Update handler is
  one).
- the community's `ap_followers_url` is cleared in the fixture, and the
  document carries neither `attributedTo`/`moderators` nor `followers` nor
  `featured`. Those three collections are each fetched only when the
  corresponding url is set, so clearing the one tests.factories.make_community
  supplies leaves the task with nothing to fetch. `block_outbound_http` in
  conftest would raise on any request these tests did provoke, so a fix that
  started fetching something would fail here rather than pass quietly.

The session, and why autoflush is not load-bearing for this guard
-----------------------------------------------------------------

The task takes its session from get_task_session(), a plain
`sqlalchemy.orm.Session(bind=db.engine)` whose autoflush is left at SQLAlchemy's
default of True -- unlike db.session, which the application factory configures
with `session_options={"autoflush": False}`. That asymmetry is load-bearing one
frame down, inside find_flair_or_create: it is the only reason two entries in
one `lemmy:tagsForPosts` list sharing a display_name can collide, which is what
made find_flair_or_create's ap_id backfill (D9) reachable at all. See
tests/test_ap_find_flair_or_create.py's TestMissingIdKeyOnTheBackfill.

It is NOT load-bearing for the guard this file pins. The guard is a membership
test on the peer's own dict, taken before any query is issued, so it answers
the same on either session. The autoflush difference is still visible in these
tests -- test_two_entries_sharing_a_display_name_collapse_into_one_flair pins
it, and would give two rows on db.session -- but nothing about the skip
depends on it.

The rows the tests assert on
----------------------------

Every test asserts on the refreshed Community's own columns as well as on the
flair, because the defect was a PARTIALLY-APPLIED ingest rather than a crash:
the task commits the refreshed profile and only then walks the tag list, and
its `except Exception: session.rollback(); raise` cannot undo a commit. So
"the title was updated" was already true while the exception escaped, and a
test that only asserted "nothing raised" would not distinguish the fix from a
guard that abandoned the whole document.
"""

from app import db
from app.activitypub.util import refresh_community_profile_task
from app.models import Community, CommunityFlair
from tests.factories import make_community, make_community_flair, seed_community_owner

PEER = 'peer.example'


def _community_awaiting_refresh(name='memes'):
    """A remote community on an online instance, with no flair and nothing the
    task would fetch.

    make_community sets ap_followers_url; it is cleared here so the followers
    collection is never fetched. See the module docstring.
    """
    seed_community_owner(PEER)
    community = make_community(name)
    community.ap_followers_url = None
    db.session.commit()
    return community


def _actor_document(name='memes', fields=None):
    """The peer's Group document, holding only the keys the task reads
    unconditionally: `name`, which becomes the title, and
    `publicKey.publicKeyPem`. Every other key is opted into by `fields`, so the
    absent side of each of the task's guards is what the baseline already
    gives.

    `tag` is deliberately absent and must stay absent: the legacy block is
    gated on `"tag" not in activity_json` as well as on the list check, so a
    document carrying both takes update_community_flair_from_tags instead and
    never reaches the code under test. This is not how actor_json_to_model
    spells the same choice -- there the two are an `if`/`elif`.
    """
    document = {
        'type': 'Group',
        'id': f'https://{PEER}/c/{name}',
        'preferredUsername': name,
        'name': 'Memes, refreshed',
        'publicKey': {'publicKeyPem': '-----BEGIN PUBLIC KEY-----refreshed'},
    }
    if fields:
        document.update(fields)
    return document


class TestLegacyFlairOnRefresh:
    """`if 'lemmy:tagsForPosts' in activity_json and isinstance(..., list) and
    "tag" not in activity_json:` / `if len(community.flair) == 0:`, and the
    loop under it.

    The loop builds `{'display_name': flair['display_name']}` and then copies
    'text_color', 'background_color' and 'blur_images' each behind its own
    `in` test. Unlike actor_json_to_model's otherwise identical legacy block,
    it never copies 'id' -- which is what sends every entry from this caller
    down find_flair_or_create's derive-a-local-ap_id arm.

    Both gates are pinned below, because both of them can hide the loop from a
    test that gets the document wrong.
    """

    def test_a_well_formed_tag_list_becomes_community_flair(self, app, db_session):
        """The baseline. The refreshed profile and the flair both land, which
        is the state the malformed cases below have to reach as well."""
        community = _community_awaiting_refresh()
        document = _actor_document(fields={'lemmy:tagsForPosts': [
            {'display_name': 'Discussion'},
            {'display_name': 'Meta'},
        ]})

        refresh_community_profile_task(community.id, document)

        db.session.expire_all()
        assert community.title == 'Memes, refreshed'
        assert sorted(f.flair for f in community.flair) == ['Discussion', 'Meta']
        assert db.session.query(CommunityFlair).count() == 2

    def test_every_optional_key_is_copied_onto_the_flair(self, app, db_session):
        """The present side of the three optional `in` tests inside the loop.

        The peer's 'id' is supplied and deliberately does not survive: this
        block builds a fresh dict from four keys and 'id' is not one of them,
        unlike actor_json_to_model's otherwise identical legacy block, which
        copies it across. find_flair_or_create therefore inserts with a null
        ap_id -- it derives one only on its backfill path, for a row it FOUND
        rather than one it created."""
        community = _community_awaiting_refresh()
        document = _actor_document(fields={'lemmy:tagsForPosts': [{
            'display_name': 'Discussion',
            'text_color': '#ffffff',
            'background_color': '#000000',
            'blur_images': True,
            'id': f'https://{PEER}/c/memes/tag/1',
        }]})

        refresh_community_profile_task(community.id, document)

        db.session.expire_all()
        stored = db.session.query(CommunityFlair).one()
        assert stored.flair == 'Discussion'
        assert stored.text_color == '#ffffff'
        assert stored.background_color == '#000000'
        assert stored.blur_images is True
        assert stored.ap_id is None
        assert [f.id for f in community.flair] == [stored.id]

    def test_a_tag_list_is_ignored_when_the_community_already_has_flair(self, app, db_session):
        """`if len(community.flair) == 0`. A community that already has flair
        keeps exactly what it had; the peer's list is not merged in."""
        community = _community_awaiting_refresh()
        make_community_flair(community, name='Existing')
        community.flair.append(db.session.query(CommunityFlair).one())
        db.session.commit()
        document = _actor_document(fields={'lemmy:tagsForPosts': [
            {'display_name': 'Discussion'},
        ]})

        refresh_community_profile_task(community.id, document)

        db.session.expire_all()
        assert community.title == 'Memes, refreshed'
        assert [f.flair for f in community.flair] == ['Existing']
        assert db.session.query(CommunityFlair).count() == 1

    def test_a_document_carrying_tag_as_well_never_reaches_the_legacy_loop(self, app, db_session):
        """`"tag" not in activity_json`. With both keys present the sibling
        branch takes the document, so the legacy list is not read at all --
        which is why _actor_document leaves `tag` out. The new-style entry
        here is the one that becomes flair; the legacy entry does not."""
        community = _community_awaiting_refresh()
        document = _actor_document(fields={
            'lemmy:tagsForPosts': [{'display_name': 'Legacy'}],
            'tag': [{'type': 'CommunityPostTag',
                     'id': f'https://{PEER}/c/memes/tag/1',
                     'preferredUsername': 'NewStyle'}],
        })

        refresh_community_profile_task(community.id, document)

        db.session.expire_all()
        assert [f.flair for f in community.flair] == ['NewStyle']
        assert db.session.query(CommunityFlair).count() == 1

    def test_a_tags_for_posts_that_is_not_a_list_is_ignored(self, app, db_session):
        """`isinstance(activity_json['lemmy:tagsForPosts'], list)`. A mapping
        passes the `in` test and is iterable, so without the type test the
        loop would iterate its keys -- strings -- straight into the subscript
        this file's guard now covers."""
        community = _community_awaiting_refresh()
        document = _actor_document(fields={
            'lemmy:tagsForPosts': {'display_name': 'Discussion'}})

        refresh_community_profile_task(community.id, document)

        db.session.expire_all()
        assert community.title == 'Memes, refreshed'
        assert community.flair == []
        assert db.session.query(CommunityFlair).count() == 0

    def test_an_entry_without_a_display_name_is_skipped_and_the_rest_ingest(self, app, db_session, caplog):
        """FIXED -- D26. `flair_dict = {'display_name': flair['display_name']}`
        was an unguarded read sitting directly above three `in`-guarded reads
        of the same dict, so a legacy entry omitting the key raised KeyError
        out of refresh_community_profile_task.

        What made that a partially-applied ingest rather than a plain crash:
        the task commits the refreshed community profile immediately above
        this block, and its `except Exception: session.rollback(); raise`
        cannot undo a commit. The peer's Update therefore left a community
        whose title, description, nsfw flag and public key had all been
        updated, no flair, an exception at the caller, and -- because the
        raise skips log_incoming_ap -- no ActivityPubLog row saying so. That
        is why the title assertion is here as well as the flair one: adding a
        rollback would have changed nothing, and the fix is the guard.

        The malformed entry is deliberately in the MIDDLE of the list. The
        counts separate 'skipped the bad entry' from 'skipped the loop': both
        good entries are present, so a guard that swallowed the whole list, or
        that abandoned the loop at the first bad entry, fails here even though
        nothing raised.
        """
        community = _community_awaiting_refresh()
        document = _actor_document(fields={'lemmy:tagsForPosts': [
            {'display_name': 'Discussion'},
            {'text_color': '#ffffff'},
            {'display_name': 'Meta'},
        ]})

        with caplog.at_level('WARNING'):
            refresh_community_profile_task(community.id, document)

        db.session.expire_all()
        assert community.title == 'Memes, refreshed'
        assert sorted(f.flair for f in community.flair) == ['Discussion', 'Meta']
        assert db.session.query(CommunityFlair).count() == 2
        assert db.session.query(Community).count() == 1

        skips = [r for r in caplog.records
                 if 'refresh_community_profile_task' in r.getMessage()]
        assert len(skips) == 1
        assert skips[0].levelname == 'WARNING'
        assert "lemmy:tagsForPosts" in skips[0].getMessage()
        assert "{'text_color': '#ffffff'}" in skips[0].getMessage()
        assert "display_name" in skips[0].getMessage()

    def test_an_entry_that_is_not_an_object_is_skipped_and_the_rest_ingest(self, app, db_session, caplog):
        """FIXED -- the other half of D26. An entry that is not a dict at all
        raised `TypeError: string indices must be integers` on the subscript,
        again after the refreshed profile had been committed.

        The two malformed entries are chosen to make the ISINSTANCE half of
        the guard load-bearing, following the correction the same guard needed
        in tests/test_ap_actor_json_group.py: `'display_name' not in flair` is
        a perfectly legal SUBSTRING test on a string, so a plain string is
        turned away by the membership half alone and proves nothing about the
        isinstance half. The entries used instead are a string that DOES
        contain 'display_name' -- a peer that serialised its tag twice -- and
        an integer, for which the membership test is itself a TypeError.
        Either one kills a mutant that drops the isinstance half.

        Both skips are logged, and each log names the entry it dropped: a
        single warning covering the pair would not tell an operator which of
        the peer's entries was lost.
        """
        community = _community_awaiting_refresh()
        document = _actor_document(fields={'lemmy:tagsForPosts': [
            {'display_name': 'Discussion'},
            '{"display_name": "Discussion"}',
            5,
            {'display_name': 'Meta'},
        ]})

        with caplog.at_level('WARNING'):
            refresh_community_profile_task(community.id, document)

        db.session.expire_all()
        assert community.title == 'Memes, refreshed'
        assert sorted(f.flair for f in community.flair) == ['Discussion', 'Meta']
        assert db.session.query(CommunityFlair).count() == 2

        skips = [r.getMessage() for r in caplog.records
                 if 'refresh_community_profile_task' in r.getMessage()]
        assert len(skips) == 2
        assert any('{"display_name": "Discussion"}' in message for message in skips)
        assert any(' 5 ' in message for message in skips)
        assert all("lemmy:tagsForPosts" in message for message in skips)
        assert all('not an object carrying' in message for message in skips)

    def test_a_list_of_only_malformed_entries_leaves_the_profile_refreshed(self, app, db_session):
        """The whole list dropped. Nothing raises, no flair is written, and
        the profile commit above the loop still stands -- which is the state
        the defect used to reach by raising instead of returning."""
        community = _community_awaiting_refresh()
        document = _actor_document(fields={'lemmy:tagsForPosts': ['x', {}, None]})

        refresh_community_profile_task(community.id, document)

        db.session.expire_all()
        assert community.title == 'Memes, refreshed'
        assert community.flair == []
        assert db.session.query(CommunityFlair).count() == 0

    def test_two_entries_sharing_a_display_name_collapse_into_one_flair(self, app, db_session):
        """The autoflush difference described in the module docstring, seen
        from this caller rather than from find_flair_or_create's own tests.

        get_task_session() leaves autoflush on, so the first entry's pending
        CommunityFlair insert is visible to the second entry's query and the
        second call finds it instead of inserting a duplicate. The same list
        against db.session would give two rows with the same name. Nothing
        about the skip guard depends on this; it is pinned so that a future
        change to get_task_session cannot alter this caller's behaviour
        unnoticed.
        """
        community = _community_awaiting_refresh()
        document = _actor_document(fields={'lemmy:tagsForPosts': [
            {'display_name': 'Discussion'},
            {'display_name': 'Discussion'},
            {'display_name': 'Meta'},
        ]})

        refresh_community_profile_task(community.id, document)

        db.session.expire_all()
        assert db.session.query(CommunityFlair).count() == 2
        assert sorted(f.flair for f in community.flair) == ['Discussion', 'Meta']


class TestTheCommitBeforeTheLoop:
    """The `session.commit()` that makes D26 a partially-applied ingest rather
    than a failed one.

    Nothing in the loop can undo it, so the fix had to be the guard. This class
    pins the commit itself, so that a later change moving the commit below the
    loop -- which would be a genuine fix of the shape, and a much larger one --
    flips a test rather than passing silently.
    """

    def test_the_refreshed_profile_is_committed_before_the_flair_loop_runs(self, app, db_session):
        """A second session, opened after the task returns, sees the refreshed
        columns. The flair list here is well formed, so this asserts the
        ordering by asserting that BOTH halves landed; its malformed sibling
        above asserts the first half lands without the second."""
        community = _community_awaiting_refresh()
        document = _actor_document(fields={
            'sensitive': True,
            'summary': '<p>refreshed description</p>',
            'lemmy:tagsForPosts': [{'display_name': 'Discussion'}],
        })

        refresh_community_profile_task(community.id, document)

        db.session.expire_all()
        stored = db.session.query(Community).one()
        assert stored.title == 'Memes, refreshed'
        assert stored.nsfw is True
        assert stored.description_html == '<p>refreshed description</p>'
        assert stored.public_key == '-----BEGIN PUBLIC KEY-----refreshed'
        assert [f.flair for f in stored.flair] == ['Discussion']


class TestTheGatesAboveTheProfileUpdate:
    """The two conditions the whole body sits under: a community that exists,
    and an instance that is online. Both are false arms nothing else in this
    file takes, and both would silently hide every assertion above if a future
    change made them easier to fall into."""

    def test_an_unknown_community_id_is_a_no_op(self, app, db_session):
        refresh_community_profile_task(999999, _actor_document())
        assert db.session.query(Community).count() == 0

    def test_a_dormant_instance_is_skipped(self, app, db_session):
        """`community.instance.online()`. The task does nothing at all for a
        community whose instance is dormant, so the profile is not refreshed
        and no flair is written even from a well-formed list."""
        community = _community_awaiting_refresh()
        community.instance.dormant = True
        db.session.commit()
        document = _actor_document(fields={'lemmy:tagsForPosts': [
            {'display_name': 'Discussion'}]})

        refresh_community_profile_task(community.id, document)

        db.session.expire_all()
        assert community.title == 'memes'
        assert db.session.query(CommunityFlair).count() == 0
