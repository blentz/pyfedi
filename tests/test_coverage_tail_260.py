"""Round 260: reconciling a community's flair with what its own server says.

`update_community_flair_from_tags` and `remove_outdated_community_flair` keep this instance's
`CommunityFlair` rows in step with the tag list in a peer's Group document. Posts are attached to
flair rows, so the reconciliation decides whether a post keeps its label -- and a row removed here
takes its associations with it.

`tests/test_ap_refresh_community_profile.py` covers `find_flair_or_create` itself. What had no rows
was the pair above it: which existing flair survives a refresh, and by what key. There are TWO keys,
and which one is used depends on whether the row carries an `ap_id`:

    an ap_id'd row survives if its ap_id is still in the document
    a row with no ap_id survives if its NAME is still in the document

so a community whose peer renames a flair keeps the row when the id matches and loses it when only
the name does.
"""
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.activitypub.util import (find_flair_or_create, remove_outdated_community_flair,
                                  update_community_flair_from_tags)
from app.models import CommunityFlair, Post, Site
from tests.factories import (make_community, make_community_flair, make_community_member,
                             make_post, make_user)


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('flairland')
    author = make_user(api_baseline.instance_local, 'flairauthor', local=True)
    db.session.commit()
    make_community_member(author, community)
    return SimpleNamespace(app=app, community=community, author=author,
                           baseline=api_baseline)


def tag(name, ap_id=None, **extra):
    """One entry of the peer's tag list, in the newer camelCase spelling."""
    entry = {'type': 'CommunityPostTag', 'preferredUsername': name}
    if ap_id is not None:
        entry['id'] = ap_id
    entry.update(extra)
    return entry


def flair_names(community):
    db.session.expire_all()
    return sorted(f.flair for f in community.flair)


# --------------------------------------------------------------------------
# What a refresh adds
# --------------------------------------------------------------------------


class TestWhatATagListAdds:

    def test_a_new_tag_becomes_flair(self, env):
        update_community_flair_from_tags(env.community, [tag('Spoilers')])
        db.session.commit()

        assert flair_names(env.community) == ['Spoilers']

    def test_the_colours_and_the_blur_flag_are_taken_from_the_tag(self, env):
        """Both spellings are read -- `textColor`/`text_color` and `blurImages`/`blur_images` --
        and `blur_images` is the one that hides images, so it is a moderation setting arriving
        from somebody else's server."""
        update_community_flair_from_tags(env.community, [
            tag('Spoilers', textColor='#ffffff', backgroundColor='#000000',
                blurImages=True)])
        db.session.commit()

        flair = CommunityFlair.query.filter_by(community_id=env.community.id).one()
        assert flair.text_color == '#ffffff'
        assert flair.background_color == '#000000'
        assert flair.blur_images is True

    def test_a_tag_with_no_name_stops_the_reconciliation(self, env):
        """`if updated_flair_obj is None: return`. A tag with no usable name means the document
        cannot be trusted to say which flair should exist -- so the function returns BEFORE the
        removal pass, and nothing this community already has is deleted."""
        existing = make_community_flair(env.community, 'Existing')
        db.session.commit()

        update_community_flair_from_tags(env.community, [tag('Spoilers'), {'type': 'x'}])
        db.session.commit()

        assert 'Existing' in flair_names(env.community)

    def test_the_name_is_stripped(self, env):
        update_community_flair_from_tags(env.community, [tag('  Spoilers  ')])
        db.session.commit()

        assert flair_names(env.community) == ['Spoilers']


# --------------------------------------------------------------------------
# What a refresh keeps and removes
# --------------------------------------------------------------------------


class TestWhatATagListRemoves:

    def test_flair_the_document_no_longer_names_is_removed(self, env):
        """The point of the second pass: a moderator on the community's own server deleted a
        flair, and this instance has to stop offering it."""
        make_community_flair(env.community, 'Gone')
        db.session.commit()

        update_community_flair_from_tags(env.community, [tag('Kept')])
        db.session.commit()

        assert flair_names(env.community) == ['Kept']

    def test_flair_the_document_still_names_survives(self, env):
        """The control. A row with no `ap_id` -- what this instance creates for its own flair --
        survives a document that names it.

        `processed_names` is an EQUIVALENT MUTANT in this path, and the reason is
        `find_flair_or_create`: a matched row with no `ap_id` gets one BACKFILLED (derived
        locally when the peer supplies none), so by the time the removal pass runs every
        surviving row has an id and the id arm keeps it. The name keep-set matters only when
        `remove_outdated_community_flair` is called directly, which the last row in this class
        does.
        """
        make_community_flair(env.community, 'Kept')
        db.session.commit()

        update_community_flair_from_tags(env.community, [tag('Kept')])
        db.session.commit()

        assert flair_names(env.community) == ['Kept']

    def test_an_ap_id_row_survives_a_rename(self, env):
        """The other key. A flair carrying an `ap_id` is matched on the id, so the community's
        server renaming it updates the row rather than replacing it -- and every post already
        labelled with it keeps its label."""
        existing = make_community_flair(env.community, 'Old Name')
        existing.ap_id = 'https://peer.example/c/flairland/tag/1'
        db.session.commit()
        post = make_post(env.community, env.author, ap_id='https://test.piefed.local/f/1')
        post.flair.append(existing)
        db.session.commit()
        flair_id = existing.id

        update_community_flair_from_tags(
            env.community, [tag('New Name', ap_id='https://peer.example/c/flairland/tag/1')])
        db.session.commit()

        db.session.expire_all()
        surviving = db.session.get(CommunityFlair, flair_id)
        assert surviving is not None
        assert db.session.get(Post, post.id).flair[0].id == flair_id

    def test_a_reused_name_under_a_new_id_keeps_the_existing_row_and_its_own_id(self, env):
        """The lookup order decides this, and it is worth stating. `find_flair_or_create` tries
        the document's `id` FIRST and the name second, and the update arm only backfills an
        `ap_id` when the row has none -- so a document reusing the NAME under a new id matches
        by name, keeps the row, and keeps the row's ORIGINAL ap_id.

        The keep-set is then built from the ROW's ap_id rather than from the document's, which is
        why the removal pass leaves it alone. Recorded rather than changed: the row's post
        associations survive, which is the conservative outcome.
        """
        existing = make_community_flair(env.community, 'Spoilers')
        existing.ap_id = 'https://peer.example/c/flairland/tag/1'
        db.session.commit()
        old_id = existing.id

        update_community_flair_from_tags(
            env.community, [tag('Spoilers', ap_id='https://peer.example/c/flairland/tag/2')])
        db.session.commit()

        db.session.expire_all()
        surviving = db.session.get(CommunityFlair, old_id)
        assert surviving is not None
        assert surviving.ap_id == 'https://peer.example/c/flairland/tag/1'

    def test_an_ap_id_row_the_document_drops_entirely_is_removed(self, env):
        """`if existing_flair.ap_id and existing_flair.ap_id not in keep_ap_ids`. This is the arm
        for a row matched by NEITHER key: the peer deleted that flair and named a different one,
        so the id is not in the keep-set and the row goes with its associations."""
        existing = make_community_flair(env.community, 'Old Label')
        existing.ap_id = 'https://peer.example/c/flairland/tag/1'
        db.session.commit()
        old_id = existing.id

        update_community_flair_from_tags(
            env.community,
            [tag('Brand New', ap_id='https://peer.example/c/flairland/tag/2')])
        db.session.commit()

        db.session.expire_all()
        assert db.session.get(CommunityFlair, old_id) is None
        assert flair_names(env.community) == ['Brand New']

    def test_a_row_with_no_ap_id_is_kept_by_name_alone(self, env):
        """The `elif`. Locally created flair has no `ap_id`, so the name is the only key -- and
        a document naming it keeps it even though it carries an id the row does not have."""
        existing = make_community_flair(env.community, 'Local Flair')
        existing.ap_id = None
        db.session.commit()
        flair_id = existing.id

        update_community_flair_from_tags(
            env.community,
            [tag('Local Flair', ap_id='https://peer.example/c/flairland/tag/9')])
        db.session.commit()

        db.session.expire_all()
        assert db.session.get(CommunityFlair, flair_id) is not None

    def test_an_empty_tag_list_removes_everything(self, env):
        """A community whose server has deleted all its flair. The removal pass runs with empty
        keep-sets, which is the case a guard against empty input would get wrong in the other
        direction."""
        make_community_flair(env.community, 'One')
        make_community_flair(env.community, 'Two')
        db.session.commit()

        update_community_flair_from_tags(env.community, [])
        db.session.commit()

        assert flair_names(env.community) == []

    def test_the_removal_pass_can_be_called_on_its_own(self, env):
        """`remove_outdated_community_flair` is also called directly, with keep-sets a caller
        built -- so it is asserted on its own terms rather than only through the pair."""
        keep = make_community_flair(env.community, 'Keep')
        make_community_flair(env.community, 'Drop')
        db.session.commit()

        remove_outdated_community_flair(env.community, set(), {'Keep'})
        db.session.commit()

        assert flair_names(env.community) == ['Keep']
        assert keep.id is not None
        assert CommunityFlair.query.filter_by(community_id=env.community.id).count() == 1

    def test_the_removal_both_detaches_and_deletes(self, env):
        """`community.flair.remove(flair)` and `session.delete(flair)` are each an EQUIVALENT
        MUTANT of the other: the relationship cascades, so detaching deletes the row and
        deleting it empties the collection. Both lines are kept because they say what is meant
        at both ends, and the assertion covers both -- the row is gone from the table AND from
        the community's collection.
        """
        make_community_flair(env.community, 'Drop')
        db.session.commit()

        remove_outdated_community_flair(env.community, set(), set())
        db.session.commit()

        db.session.expire_all()
        assert CommunityFlair.query.filter_by(community_id=env.community.id).count() == 0
        assert list(db.session.get(type(env.community), env.community.id).flair) == []


class TestFindFlairOrCreate:
    """The single-entry helper the pair is built on. `tests/test_ap_refresh_community_profile.py`
    drives it through the refresh; these rows call it directly for the arms that refresh does not
    reach.
    """

    def test_an_existing_flair_is_found_by_name(self, env):
        existing = make_community_flair(env.community, 'Spoilers')
        db.session.commit()

        found = find_flair_or_create(tag('Spoilers'), env.community.id)

        assert found.id == existing.id

    def test_a_peer_entry_with_no_id_backfills_a_local_one(self, env):
        """The comment in the source explains why: reading `flair['id']` unconditionally raised
        `KeyError` out of `refresh_community_profile_task` AFTER it had committed the refreshed
        profile. The row asserts the derived id rather than the absence of a crash."""
        existing = make_community_flair(env.community, 'Spoilers')
        existing.ap_id = None
        db.session.commit()

        found = find_flair_or_create(tag('Spoilers'), env.community.id)
        db.session.commit()

        assert found.ap_id == found.get_ap_id()

    def test_an_entry_with_no_name_creates_nothing(self, env):
        """`if flair_text:` / `else: return None`. The name is the only field with no default,
        and a flair with an empty name would render as a blank chip beside every post."""
        assert find_flair_or_create({'type': 'CommunityPostTag'}, env.community.id) is None
        assert CommunityFlair.query.filter_by(community_id=env.community.id).count() == 0

    def test_the_older_lemmy_spelling_is_read_too(self, env):
        """`display_name` / `text_color` / `background_color` / `blur_images` -- the snake_case
        set Lemmy sends, beside the camelCase set above. Two spellings of five fields, and a
        peer using either must produce the same row."""
        found = find_flair_or_create(
            {'type': 'lemmy:CommunityTag', 'display_name': 'Spoilers',
             'text_color': '#111111', 'background_color': '#222222',
             'blur_images': True},
            env.community.id)
        db.session.commit()

        assert found.flair == 'Spoilers'
        assert found.text_color == '#111111'
        assert found.background_color == '#222222'
        assert found.blur_images is True
