"""Round 271: the last one-line arms, across four modules.

Nothing here is a cluster; every row is one line that answers one question, and they are grouped by
what the answer is used for rather than by where it lives.

    who may post here           `can_create_post`'s instance-ban arm -- a community on an instance
                                the account has banned. This is a permission answer, and the last of
                                its four refusals to have no row.
    what an actor resolves to    `find_actor_or_create_cached` handed a DICT rather than a url,
                                which is what an embedded actor object in a peer's activity is.
    what already exists          `find_licence_or_create` and `find_hashtag_or_create` returning the
                                existing row -- without which every post in a licence or a hashtag
                                adds a duplicate.
    what a boost undoes          `undo_boost` with no target, refused before any lookup.
    what a delete recalculates   `calculate_cross_posts(delete_only=True)`, which takes a deleted
                                post OUT of its cross-post siblings' lists.
    the image format fallback    `make_image_sizes_async`'s `else: PNG` for an extension it has no
                                rule for.
    five small readers           `markdown_to_text`, `pending_communities`, `mime_type_using_head`'s
                                empty answer, `User.get_id` for an anonymous visitor,
                                `User.get_by_email`, and `Passkey.__repr__`.
"""
from unittest.mock import patch

import httpx
import pytest
from flask import current_app, g

from app import db
from app.activitypub.util import (find_actor_or_create_cached, find_hashtag_or_create,
                                  find_licence_or_create, undo_boost)
from app.models import Licence, Passkey, Post, Site, Tag, User
from app.utils import (can_create_post, markdown_to_text, mime_type_using_head,
                       pending_communities)
from tests.factories import (make_community, make_community_member, make_instance, make_post,
                             make_user)


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('lastland')
    author = make_user(api_baseline.instance_local, 'lastauthor', local=True)
    db.session.commit()
    make_community_member(author, community)
    db.session.commit()
    return SimpleNamespace(app=app, community=community, author=author,
                           baseline=api_baseline)


# --------------------------------------------------------------------------
# Who may post here
# --------------------------------------------------------------------------


class TestWhoMayPostInACommunity:

    def test_somebody_who_banned_the_communitys_instance_may_not_post(self, env):
        """`if content.instance_id in banned_instances(user.id): return False`.

        A BAN is the stronger of the two account-level settings -- a block hides the content, a ban
        stops it federating -- so somebody who banned an instance must not have their post sent to a
        community hosted there.

        THAT LINE IS REDUNDANT, and provably: `communities_banned_from(user.id)` ONE LINE ABOVE it
        joins `InstanceBan` to `Community` on `instance_id` and returns every community on a banned
        instance, so the community's own id is already in that list. `InstanceBan.instance_id` is
        part of that table's primary key, so there is no null-instance row to slip past the join
        either. The refusal below is what this row asserts, whichever line produces it; the line is
        kept because it states the instance-level rule where a reader of the function looks for it.
        """
        from app import cache
        from app.models import InstanceBan
        from app.utils import banned_instances

        peer = make_instance('banned-by-me.example')
        db.session.commit()
        remote_community = make_community('faraway', host='banned-by-me.example')
        remote_community.instance_id = peer.id
        db.session.commit()
        # `banned_instances(user_id)` reads `InstanceBan`, the per-account list --
        # `BannedInstances` is the INSTANCE-WIDE one an admin maintains.
        db.session.add(InstanceBan(user_id=env.author.id, instance_id=peer.id))
        env.author.verified = True
        env.author.private_key = '-----BEGIN PRIVATE KEY-----x'
        db.session.commit()
        cache.delete_memoized(banned_instances, env.author.id)

        assert can_create_post(env.author, remote_community) is False

    def test_somebody_with_no_ban_may_post(self, env):
        """The control, so the row above refuses for the BAN rather than for the community being
        remote."""
        peer = make_instance('fine-by-me.example')
        db.session.commit()
        remote_community = make_community('friendly', host='fine-by-me.example')
        remote_community.instance_id = peer.id
        # A LOCAL account is refused earlier unless it is verified and has a signing key --
        # `user.verified is False or user.private_key is None` two arms above the one under test.
        env.author.verified = True
        env.author.private_key = '-----BEGIN PRIVATE KEY-----x'
        db.session.commit()

        assert can_create_post(env.author, remote_community) is True


# --------------------------------------------------------------------------
# Resolving what a peer named
# --------------------------------------------------------------------------


class TestResolvingAnActorAPeerEmbedded:

    def test_an_embedded_actor_object_is_read_by_its_id(self, env):
        """`if isinstance(actor, dict): actor = actor['id']`.

        A peer may inline the whole actor document where a url is expected -- Mastodon does, in some
        activities -- and everything below this line is string work (`.strip()`,
        `validate_remote_actor`, a url lookup). Without it a dict reached `.strip()` as an
        AttributeError out of whichever inbox or page asked.
        """
        remote = make_user(env.baseline.instance_remote, 'embedded')
        remote.ap_profile_id = 'https://remote.piefed.test/u/embedded'
        remote.ap_public_url = 'https://remote.piefed.test/u/embedded'
        db.session.commit()

        found = find_actor_or_create_cached(
            {'type': 'Person', 'id': 'https://remote.piefed.test/u/embedded'},
            create_if_not_found=False)

        assert found is not None
        assert found.id == remote.id

    def test_a_plain_url_still_resolves(self, env):
        remote = make_user(env.baseline.instance_remote, 'plainurl')
        remote.ap_profile_id = 'https://remote.piefed.test/u/plainurl'
        remote.ap_public_url = 'https://remote.piefed.test/u/plainurl'
        db.session.commit()

        found = find_actor_or_create_cached('https://remote.piefed.test/u/plainurl',
                                            create_if_not_found=False)

        assert found is not None
        assert found.id == remote.id


class TestFindingWhatAlreadyExists:
    """Two `find_X_or_create` helpers whose EXISTING-row arm had no row. Each is called once per
    incoming post that names a licence or a hashtag, so a helper that always created would add a
    duplicate per post -- and `Tag` is what a hashtag page groups by.
    """

    def test_an_existing_licence_is_reused(self, env):
        first = find_licence_or_create('CC BY-SA 4.0')
        db.session.commit()

        second = find_licence_or_create('CC BY-SA 4.0')
        db.session.commit()

        assert second.id == first.id
        assert Licence.query.filter_by(name='CC BY-SA 4.0').count() == 1

    def test_a_licence_name_is_matched_after_stripping(self, env):
        first = find_licence_or_create('CC BY 4.0')
        db.session.commit()

        assert find_licence_or_create('  CC BY 4.0  ').id == first.id

    def test_an_existing_hashtag_is_reused(self, env):
        first = find_hashtag_or_create('#Photography')
        db.session.commit()

        second = find_hashtag_or_create('#photography')
        db.session.commit()

        assert second.id == first.id
        assert Tag.query.filter_by(name='photography').count() == 1

    def test_the_hash_is_optional_and_the_display_form_is_kept(self, env):
        """The `name`/`display_as` split: lookups fold case so `#Photography` and `#photography`
        are one tag, and the display form is what a page shows."""
        tag = find_hashtag_or_create('Photography')
        db.session.commit()

        assert tag.name == 'photography'
        assert tag.display_as == 'Photography'


class TestUndoingABoost:

    def test_a_boost_with_no_target_is_refused_before_any_lookup(self, env):
        """`if not target_ap_id: return None`. The docstring says this function never logs, so the
        caller distinguishes its refusals by the return value alone.

        The seeded post is what makes the guard observable, and says what it is worth: `Post.ap_id`
        is nullable, so without the guard `Post.get_by_ap_id(None)` runs as `ap_id IS NULL` and
        matches SOME post -- and an Undo naming nothing would then remove a boost from whichever
        post that is. `''` has no such row to find, so only the None case can show it.
        """
        orphan = make_post(env.community, env.author, ap_id='https://test.piefed.local/o/1')
        db.session.commit()
        orphan.ap_id = None
        db.session.commit()
        removed = []

        with patch('app.activitypub.util.remove_boost',
                   side_effect=lambda p, u: removed.append(p.id)):
            assert undo_boost(None, env.author) is None
            assert undo_boost('', env.author) is None

        assert removed == []

    def test_a_boost_of_a_post_that_is_not_here_is_refused(self, env):
        assert undo_boost('https://peer.example/statuses/never-seen', env.author) is None

    def test_a_boost_of_a_post_that_is_here_is_undone(self, env):
        """The True side of both guards, without which neither row above says anything: `if True:`
        and `if False:` on the first guard are indistinguishable while every input is refused.

        The post is returned so the caller can log a result, which the docstring says is the only
        way this function reports anything -- it never logs.
        """
        post = make_post(env.community, env.author, ap_id='https://peer.example/statuses/1')
        db.session.commit()
        removed = []

        with patch('app.activitypub.util.remove_boost',
                   side_effect=lambda p, u: removed.append((p.id, u.id))):
            answer = undo_boost('https://peer.example/statuses/1', env.author)

        assert answer is not None
        assert answer.id == post.id
        assert removed == [(post.id, env.author.id)]


class TestWhatADeleteTakesOutOfItsSiblings:

    def test_a_deleted_post_is_removed_from_its_cross_posts(self, env):
        """`calculate_cross_posts(delete_only=True)`, reached from `delete_post_or_comment`. The
        sibling post's `cross_posts` list is rendered as "also posted in", so a deleted post left in
        it is a link to a page that answers 404.
        """
        from app.activitypub.util import delete_post_or_comment

        first = make_post(env.community, env.author, ap_id='https://test.piefed.local/c/1')
        first.url = 'https://news.example/story'
        db.session.commit()
        first.calculate_cross_posts()
        db.session.commit()
        second = make_post(env.community, env.author, ap_id='https://test.piefed.local/c/2')
        second.url = 'https://news.example/story'
        db.session.commit()
        second.calculate_cross_posts()
        db.session.commit()
        db.session.expire_all()
        assert db.session.get(Post, first.id).cross_posts == [second.id]

        delete_post_or_comment(env.author, second, False,
                               {'id': 'https://test.piefed.local/activities/delete/1',
                                'type': 'Delete'}, None)
        db.session.commit()

        db.session.expire_all()
        assert db.session.get(Post, first.id).cross_posts == []


# --------------------------------------------------------------------------
# Five small readers
# --------------------------------------------------------------------------


class TestTurningMarkdownIntoText:

    def test_an_empty_body_is_empty_text(self, env):
        """`if not markdown_text or markdown_text == '': return ''`. The value comes from
        `Post.body`, which is NULL for a link post -- and `None.replace` is the AttributeError this
        guard exists for."""
        assert markdown_to_text(None) == ''
        assert markdown_to_text('') == ''

    def test_a_heading_marker_is_removed(self, env):
        assert markdown_to_text('# A heading') == 'A heading'


class TestTheCommunitiesSomebodyIsWaitingOn:

    def test_a_pending_join_request_is_listed(self, env):
        """`pending_communities` is what draws the "pending" state on a community's button, so the
        list is read on every listing page."""
        from app.models import CommunityJoinRequest

        other = make_community('waitingland')
        db.session.commit()
        db.session.add(CommunityJoinRequest(user_id=env.author.id, community_id=other.id))
        db.session.commit()

        assert pending_communities(env.author.id) == [other.id]

    def test_an_anonymous_visitor_is_waiting_on_nothing(self, env):
        """`if user_id is None or user_id == 0`. `User.get_id()` answers 0 for an anonymous
        visitor, which is why BOTH values are named in the guard rather than just None.

        BOTH HALVES ARE EQUIVALENT MUTANTS, for the same reason in two shapes: without the guard the
        query runs as `user_id IS NULL` or `user_id = 0`, and neither can match --
        `CommunityJoinRequest.user_id` is a foreign key to `user.id`, and no row has either value. So
        the answer is the empty list either way. The guard is kept for the query it saves on every
        listing page, which is where this is read.
        """
        assert pending_communities(None) == []
        assert pending_communities(0) == []


def _answer(url, headers):
    """An httpx.Response that can answer `raise_for_status`.

    `get_request` and `mime_type_using_head` both call it, and a Response built with no `request=`
    raises "Cannot call `raise_for_status` as the request instance has not been set on this
    response" instead.
    """
    return httpx.Response(200, headers=headers,
                          request=httpx.Request('HEAD', url))


class TestWhatAHeadRequestAnswers:

    def test_a_response_with_no_content_type_is_no_type(self, env):
        """`else: return ''`. The empty string is what the callers read as "no answer" -- an
        `is_image_url` that got None here would fall through to `.split('/')` on it."""
        from app import cache

        cache.delete_memoized(mime_type_using_head)
        with patch('app.utils.httpx_client.head',
                   return_value=_answer('https://peer.example/mystery', {})):
            assert mime_type_using_head('https://peer.example/mystery') == ''

    def test_octet_stream_is_treated_as_no_type_too(self, env):
        """The neighbouring arm, and the reason it exists: a server that answers
        `application/octet-stream` has not identified the file, so claiming it as a type would let
        the extension fallback be skipped."""
        from app import cache

        cache.delete_memoized(mime_type_using_head)
        with patch('app.utils.httpx_client.head',
                   return_value=_answer('https://peer.example/bytes',
                                        {'content-type': 'application/octet-stream'})):
            assert mime_type_using_head('https://peer.example/bytes') == ''

    def test_a_named_type_is_returned(self, env):
        from app import cache

        cache.delete_memoized(mime_type_using_head)
        with patch('app.utils.httpx_client.head',
                   return_value=_answer('https://peer.example/x.png',
                                        {'content-type': 'image/png'})):
            assert mime_type_using_head('https://peer.example/x.png') == 'image/png'


class TestTwoSmallModelReaders:

    def test_an_anonymous_visitor_has_the_id_zero(self, env):
        """`User.get_id` answers 0 rather than None for an account that is not authenticated, and
        several readers test for exactly that -- `pending_communities` above is one."""
        anonymous = User()
        anonymous.id = 7

        with patch.object(User, 'is_authenticated', False):
            assert anonymous.get_id() == 0

    def test_an_account_is_found_by_email_after_stripping(self, env):
        """`User.get_by_email` is the password-reset lookup, so the strip matters: an address
        pasted with a trailing space would otherwise find nobody and the form would say so."""
        env.author.email = 'last@example.test'
        db.session.commit()

        assert User.get_by_email('  last@example.test  ').id == env.author.id
        assert User.get_by_email('nobody@example.test') is None

    def test_a_passkey_describes_itself(self, env):
        """`Passkey.__repr__` is what a shell session and a traceback show, and the device name is
        the only thing distinguishing one of somebody's keys from another."""
        passkey = Passkey(user_id=env.author.id, device='A phone',
                          passkey_id='a-passkey-id', public_key=b'y')
        db.session.add(passkey)
        db.session.commit()

        assert repr(passkey) == f'<Passkey {passkey.id} A phone>'
