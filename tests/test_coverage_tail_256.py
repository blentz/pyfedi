"""Round 256: the optional fields a community's own server can change.

`refresh_community_profile_task` applies a Group document a peer serves for its own community.
`tests/test_ap_refresh_community_profile.py` covers the flair loop and the task's guards; the rest
of the body -- every optional field, the images, and the moderator list -- had no rows.

Each of these is the REMOTE community's settings overwriting what this instance holds, once a day.
Three of them change what this instance does with the content:

    postUrlType             the URL shape every new post here gets
    restrictedToMods        whether local accounts may post at all
    genAI / sensitive / nsfl  the labels a reader filters on

and the icon/cover replacement deletes a file from this instance's disk when the peer changes a
url, exactly as round 253 found for feeds. The moderator list is the same shape: a name added to
their `attributedTo` becomes a moderator HERE.
"""
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from flask import g

from app import db
from app.activitypub.util import refresh_community_profile_task
from app.models import Community, CommunityMember, File, Site, User, utcnow
from tests.factories import make_community, make_user, seed_community_owner

PEER = 'peer.example'


@pytest.fixture
def env(app, db_session):
    g.admin_ids = []
    instance = seed_community_owner(PEER)
    community = make_community('memes', host=PEER)
    community.ap_public_url = f'https://{PEER}/c/memes'
    community.ap_followers_url = None
    community.ap_fetched_at = utcnow()
    db.session.commit()
    return SimpleNamespace(app=app, instance=instance, community=community)


def group_document(fields=None):
    document = {
        'type': 'Group',
        'id': f'https://{PEER}/c/memes',
        'preferredUsername': 'memes',
        'name': 'Memes, refreshed',
        'publicKey': {'publicKeyPem': '-----BEGIN PUBLIC KEY-----refreshed'},
    }
    if fields:
        document.update(fields)
    return document


def serve(http_mock, url, document=None, status=200):
    return http_mock.get(url).mock(
        return_value=httpx.Response(status, json=document))


def reread(community_id):
    """The task commits in its own session, so this one's identity map is stale (fact 976)."""
    db.session.expire_all()
    return db.session.get(Community, community_id)


def refresh(env, fields):
    """The task takes the document as its SECOND argument and fetches only when it is falsy,
    so a row about the body hands it over directly and makes no request at all."""
    refresh_community_profile_task(env.community.id, group_document(fields))
    return reread(env.community.id)


# --------------------------------------------------------------------------
# The fields that change what this instance does
# --------------------------------------------------------------------------


class TestTheSettingsTheirServerSends:

    def test_the_post_url_shape_is_taken_from_them(self, env):
        """`postUrlType` decides whether a post here gets `/c/<name>@<host>/p/<id>/<slug>` or
        `/post/<id>` -- see round 251. The community's own server owns that choice, so a
        refresh can change the URL shape of every post made here afterwards."""
        community = refresh(env, {'postUrlType': 'legacy'})

        assert community.post_url_type == 'legacy'

    def test_a_restricted_community_stops_accepting_local_posts(self, env):
        """`postingRestrictedToMods` -- that spelling, not `restrictedToMods` -- is an access
        control: once set, only the community's moderators may post. It arrives from THEIR
        server, so this is a remote instance closing a community local accounts were posting
        in."""
        community = refresh(env, {'postingRestrictedToMods': True})

        assert community.restricted_to_mods is True

    def test_a_peertube_community_is_restricted_whatever_it_says(self, env):
        """The line below the optional fields: a PeerTube channel is a broadcast, so posting is
        restricted to its moderators regardless of what the document carries. The instance's
        SOFTWARE decides, which makes this the one setting the community cannot opt out of."""
        env.instance.software = 'peertube'
        db.session.commit()

        community = refresh(env, {'postingRestrictedToMods': False})

        assert community.restricted_to_mods is True

    def test_the_ai_label_is_taken_from_them(self, env):
        """`genAI` marks a community whose content is model-generated, which viewers filter
        on."""
        community = refresh(env, {'genAI': True})

        assert community.ai_generated is True

    @pytest.mark.parametrize('key,column', [('sensitive', 'nsfw'), ('nsfl', 'nsfl')])
    def test_the_content_labels_are_taken_from_them(self, env, key, column):
        community = refresh(env, {key: True})

        assert getattr(community, column) is True

    def test_an_absent_sensitive_flag_clears_the_label(self, env):
        """`community.nsfw = activity_json['sensitive'] if 'sensitive' in ... else False` --
        assignment, not an update, so a community that stops marking itself NSFW stops being
        marked here. `nsfl` is the ASYMMETRIC one: it is only ever set, never cleared."""
        env.community.nsfw = True
        env.community.nsfl = True
        db.session.commit()

        community = refresh(env, {})

        assert community.nsfw is False
        assert community.nsfl is True

    def test_the_collection_urls_are_recorded(self, env, http_mock):
        """`followers` and `featured` are where this instance fetches the community's follower
        count and its pinned posts from, so a peer moving them has to be followed.

        Recording either url makes the task FETCH it -- the followers collection for the count
        and the featured one for the pinned posts -- which is why this row serves both, and is
        itself the reason the two urls are worth pinning.
        """
        http_mock.get(f'https://{PEER}/c/memes/followers').mock(
            return_value=httpx.Response(200, json={'type': 'OrderedCollection',
                                                   'totalItems': 7}))
        http_mock.get(f'https://{PEER}/c/memes/featured').mock(
            return_value=httpx.Response(200, json={'type': 'OrderedCollection',
                                                   'orderedItems': [], 'totalItems': 0}))

        community = refresh(env, {
            'followers': f'https://{PEER}/c/memes/followers',
            'featured': f'https://{PEER}/c/memes/featured'})

        assert community.ap_followers_url == f'https://{PEER}/c/memes/followers'
        assert community.ap_featured_url == f'https://{PEER}/c/memes/featured'

    def test_a_theme_is_recorded(self, env):
        community = refresh(env, {'theme': 'high-contrast'})

        assert community.theme == 'high-contrast'

    def test_a_document_with_none_of_them_changes_none_of_them(self, env):
        """Every one of these keys is optional, and most Group documents carry none. The
        baseline document is the absent side of every guard at once."""
        env.community.post_url_type = 'friendly'
        env.community.theme = 'existing'
        db.session.commit()

        community = refresh(env, {})

        assert community.post_url_type == 'friendly'
        assert community.theme == 'existing'
        assert community.restricted_to_mods is not True


class TestTheDescriptionTheirServerSends:

    def test_their_markdown_is_preferred(self, env):
        """D1346 again: `source` markdown is stored and re-rendered, so what a moderator sees
        in the edit box round-trips."""
        community = refresh(env, {
            'summary': '<p>rendered by them</p>',
            'source': {'content': 'written **by them**', 'mediaType': 'text/markdown'}})

        assert community.description == 'written **by them**'
        assert '<strong>by them</strong>' in community.description_html

    def test_without_a_source_the_text_comes_from_the_html(self, env):
        community = refresh(env, {'summary': '<p>only rendered</p>'})

        assert 'only rendered' in community.description
        assert community.description_html.startswith('<p>')


# --------------------------------------------------------------------------
# The images
# --------------------------------------------------------------------------


class TestReplacingACommunitysImages:
    """Identical in shape to the feed refresh (round 253): a url change in the peer's document
    deletes a File on this instance's disk.
    """

    @pytest.fixture
    def deletions(self, monkeypatch):
        deleted = []
        monkeypatch.setattr(File, 'delete_from_disk',
                            lambda self, *args, **kwargs: deleted.append(self.source_url))
        return deleted

    def _existing(self, env, field, url):
        row = File(source_url=url)
        db.session.add(row)
        db.session.commit()
        setattr(env.community, f'{field}_id', row.id)
        db.session.commit()
        return row

    def test_a_first_icon_is_attached_without_deleting_anything(self, env, deletions):
        community = refresh(env,
                            {'icon': {'type': 'Image', 'url': f'https://{PEER}/i.png'}})

        assert community.icon.source_url == f'https://{PEER}/i.png'
        assert deletions == []

    def test_a_changed_icon_deletes_the_old_file(self, env, deletions):
        self._existing(env, 'icon', f'https://{PEER}/old.png')

        community = refresh(env,
                            {'icon': {'type': 'Image', 'url': f'https://{PEER}/new.png'}})

        assert deletions == [f'https://{PEER}/old.png']
        assert community.icon.source_url == f'https://{PEER}/new.png'

    def test_an_unchanged_icon_is_left_alone(self, env, deletions):
        """The refresh runs daily, so without the equality test every community's icon would be
        deleted and re-downloaded every day."""
        existing = self._existing(env, 'icon', f'https://{PEER}/i.png')

        community = refresh(env,
                            {'icon': {'type': 'Image', 'url': f'https://{PEER}/i.png'}})

        assert deletions == []
        assert community.icon_id == existing.id

    def test_the_banner_is_handled_separately(self, env, deletions):
        """A second copy of the block on `image_id`, and the row asserts the icon is
        untouched."""
        self._existing(env, 'icon', f'https://{PEER}/i.png')
        self._existing(env, 'image', f'https://{PEER}/old-banner.png')

        community = refresh(env, {
            'icon': {'type': 'Image', 'url': f'https://{PEER}/i.png'},
            'image': {'type': 'Image', 'url': f'https://{PEER}/new-banner.png'}})

        assert deletions == [f'https://{PEER}/old-banner.png']
        assert community.image.source_url == f'https://{PEER}/new-banner.png'
        assert community.icon.source_url == f'https://{PEER}/i.png'


# --------------------------------------------------------------------------
# The moderator list
# --------------------------------------------------------------------------


class TestWhoTheirServerSaysModeratesIt:
    """The task fetches the community's moderators collection and applies it: an account named
    there becomes a moderator HERE, and one no longer named stops being one. That is a remote
    server deciding who may delete content in a community local accounts use.
    """

    def _moderators(self, http_mock, urls):
        serve(http_mock, f'https://{PEER}/c/memes/moderators',
              {'type': 'OrderedCollection', 'orderedItems': urls, 'totalItems': len(urls)})

    def _refresh_with_mods(self, env, http_mock, urls, fields=None):
        """The document is handed over directly; the only request the task makes here is for
        the moderators collection it names."""
        document = group_document(fields or {})
        document['attributedTo'] = f'https://{PEER}/c/memes/moderators'
        self._moderators(http_mock, urls)
        refresh_community_profile_task(env.community.id, document)
        return reread(env.community.id)

    def test_an_account_named_in_their_collection_becomes_a_moderator(self, env,
                                                                     http_mock):
        moderator = make_user(env.instance, 'their_mod')
        moderator.ap_profile_id = f'https://{PEER}/u/their_mod'
        moderator.ap_public_url = f'https://{PEER}/u/their_mod'
        db.session.commit()

        self._refresh_with_mods(env, http_mock, [moderator.ap_profile_id])

        db.session.expire_all()
        membership = CommunityMember.query.filter_by(
            community_id=env.community.id, user_id=moderator.id).one()
        assert membership.is_moderator is True

    def test_an_existing_member_is_promoted_rather_than_duplicated(self, env, http_mock):
        """`if existing_membership: existing_membership.is_moderator = True`. A moderator who
        was already a member must not get a second CommunityMember row -- the table's primary
        key is the pair, so a duplicate is an IntegrityError mid-refresh."""
        moderator = make_user(env.instance, 'their_mod')
        moderator.ap_profile_id = f'https://{PEER}/u/their_mod'
        moderator.ap_public_url = f'https://{PEER}/u/their_mod'
        db.session.add(CommunityMember(community_id=env.community.id,
                                       user_id=moderator.id, is_moderator=False))
        db.session.commit()

        self._refresh_with_mods(env, http_mock, [moderator.ap_profile_id])

        db.session.expire_all()
        rows = CommunityMember.query.filter_by(community_id=env.community.id,
                                               user_id=moderator.id).all()
        assert len(rows) == 1
        assert rows[0].is_moderator is True

    def test_an_account_no_longer_named_stops_moderating(self, env, http_mock):
        """The other half: the collection is authoritative, so a moderator dropped from it
        loses the flag here. Without this, a demoted moderator keeps deleting content."""
        former = make_user(env.instance, 'former_mod')
        former.ap_profile_id = f'https://{PEER}/u/former_mod'
        former.ap_public_url = f'https://{PEER}/u/former_mod'
        db.session.add(CommunityMember(community_id=env.community.id,
                                       user_id=former.id, is_moderator=True))
        db.session.commit()

        self._refresh_with_mods(env, http_mock, [])

        db.session.expire_all()
        membership = CommunityMember.query.filter_by(
            community_id=env.community.id, user_id=former.id).first()
        assert membership is None or membership.is_moderator is False

    def test_an_entry_given_as_an_object_is_read_by_its_id(self, env, http_mock):
        """`if isinstance(actor, dict): actor = actor['id']`. Some implementations send the
        collection as objects rather than strings, and the id is the only part this instance
        can resolve."""
        moderator = make_user(env.instance, 'their_mod')
        moderator.ap_profile_id = f'https://{PEER}/u/their_mod'
        moderator.ap_public_url = f'https://{PEER}/u/their_mod'
        db.session.commit()

        self._refresh_with_mods(env, http_mock,
                                [{'id': moderator.ap_profile_id, 'type': 'Person'}])

        db.session.expire_all()
        membership = CommunityMember.query.filter_by(
            community_id=env.community.id, user_id=moderator.id).one()
        assert membership.is_moderator is True
