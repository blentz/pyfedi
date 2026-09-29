"""Round 241: what an instance reveals, and the helpers that count and measure things.

More of `app/models.py`. Two groups.

`Instance.votes_are_public` decides whether this instance sends a vote to a peer in a form
that names the voter. It is one line of software-string comparisons with a `trusted`
override in front of it, and it had no rows -- which for a privacy decision means nothing
recorded which peers get to see who voted.

The rest are counters and sizers: `Instance`'s four per-instance counts, `User.num_content`
and `User.filesize`, `File.filesize`, `Conversation.instances`, and
`Instance.update_dormant_gone`, which is the state machine that decides when a peer stops
being polled. Each is small, each is hand-written SQL or hand-written arithmetic, and each
is the kind of helper a reader assumes is right.
"""
import os
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.models import (Conversation, File, Instance, Site, User, UserFlair,
                        conversation_member, utcnow)
from tests.factories import (make_community, make_community_flair, make_instance,
                             make_post, make_post_reply, make_user)


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    return SimpleNamespace(app=app, site=site, baseline=api_baseline)


# --------------------------------------------------------------------------
# Instance.votes_are_public
# --------------------------------------------------------------------------


class TestWhetherAPeerSeesWhoVoted:
    """A vote sent to a peer either names the voter or does not. The rule reads
    "only vote privately with untrusted instances": a TRUSTED peer gets private votes, and
    an untrusted one gets public votes if its software is on a list of four -- the
    implementations known to expose voter identities anyway.
    """

    def _instance(self, domain, software, trusted=False):
        instance = make_instance(domain)
        instance.software = software
        instance.trusted = trusted
        db.session.commit()
        return instance

    @pytest.mark.parametrize('software', ['lemmy', 'mbin', 'kbin', 'guppe groups'])
    def test_an_untrusted_peer_running_one_of_the_four_gets_public_votes(self, env,
                                                                        software):
        assert self._instance(f'{software.replace(" ", "")}.example',
                              software).votes_are_public() is True

    @pytest.mark.parametrize('software', ['LEMMY', 'Mbin', 'Guppe Groups'])
    def test_the_software_name_is_compared_in_lower_case(self, env, software):
        """`self.software.lower()`. A peer's nodeinfo is whatever it chose to send, so the
        comparison cannot depend on capitalisation -- and a peer whose name did not match
        would silently get the private treatment instead."""
        assert self._instance('cased.example', software).votes_are_public() is True

    @pytest.mark.parametrize('software', ['piefed', 'mastodon', 'sublinks', ''])
    def test_any_other_software_gets_private_votes(self, env, software):
        """The default is privacy. Anything not on the list -- including another PieFed --
        does not see who voted."""
        assert self._instance('other.example', software).votes_are_public() is False

    @pytest.mark.parametrize('software', ['lemmy', 'piefed'])
    def test_a_trusted_peer_always_gets_private_votes(self, env, software):
        """`if self.trusted is True: return False`, BEFORE the software test. Trust is the
        instance admin's own decision and it overrides the list in the safe direction --
        asserted for a peer that would otherwise get public votes AND one that would not,
        so the row is about the override rather than about the list."""
        assert self._instance('trusted.example', software,
                              trusted=True).votes_are_public() is False

    def test_trust_is_an_identity_test(self, env):
        """`self.trusted is True`, not `if self.trusted`. The column is nullable, and a peer
        whose trust has never been set is not the same as one an admin trusted."""
        instance = self._instance('untouched.example', 'lemmy')
        instance.trusted = None
        db.session.commit()

        assert instance.votes_are_public() is True


# --------------------------------------------------------------------------
# Instance's counts and its dormancy state machine
# --------------------------------------------------------------------------


class TestWhatAnInstancePageCounts:

    @pytest.fixture
    def seeded(self, env):
        peer = make_instance('counted.example')
        other = make_instance('ignored.example')
        author = make_user(peer, 'peerauthor')
        # A SECOND account on the peer, so its user count differs from its community count:
        # with one of each, a query naming the wrong table gives the same answer.
        make_user(peer, 'peerreader')
        make_user(other, 'otherauthor')
        community = make_community('countedland', host='counted.example')
        community.instance_id = peer.id
        elsewhere = make_community('ignoredland', host='ignored.example')
        elsewhere.instance_id = other.id
        db.session.commit()
        post = make_post(community, author, ap_id='https://counted.example/p/1')
        post.instance_id = peer.id
        reply = make_post_reply(post, author, body='a reply')
        reply.instance_id = peer.id
        db.session.commit()
        env.peer = peer
        env.other = other
        env.community = community
        env.post = post
        return env

    def test_each_count_counts_only_this_instances_rows(self, seeded):
        """Four hand-written queries over four tables, each filtered on `instance_id`. They
        are asserted together against one fixture, and the second instance is what makes
        each number a claim about filtering rather than about a table's size."""
        assert seeded.peer.post_count() == 1
        assert seeded.peer.post_replies_count() == 1
        assert seeded.peer.known_communities_count() == 1
        assert seeded.peer.known_users_count() == 2

        assert seeded.other.post_count() == 0
        assert seeded.other.post_replies_count() == 0
        assert seeded.other.known_communities_count() == 1
        assert seeded.other.known_users_count() == 1

    def test_the_repr_names_the_domain(self, seeded):
        """`__repr__` appears in logs and in flask-caching's own cache keys, so it is worth
        one row -- a repr that raised would break the caller rather than the log line."""
        assert repr(seeded.peer) == '<Instance counted.example>'


class TestWhenAPeerStopsBeingPolled:
    """`update_dormant_gone` is a two-step state machine driven by consecutive failures: a
    live peer goes dormant after 2, and a dormant one is given up on entirely after 7. The
    thresholds are what decide how long a peer's posts keep arriving after an outage.
    """

    def _instance(self, failures, dormant):
        instance = make_instance('flaky.example')
        instance.failures = failures
        instance.dormant = dormant
        instance.gone_forever = False
        db.session.commit()
        return instance

    @pytest.mark.parametrize('failures,expected', [(2, False), (3, True)])
    def test_a_live_peer_goes_dormant_after_more_than_two_failures(self, env, failures,
                                                                  expected):
        instance = self._instance(failures, dormant=False)

        instance.update_dormant_gone()

        assert instance.dormant is expected
        assert instance.gone_forever is False

    @pytest.mark.parametrize('failures,expected', [(7, False), (8, True)])
    def test_a_dormant_peer_is_given_up_on_after_more_than_seven(self, env, failures,
                                                                expected):
        """The `elif` ordering matters: a dormant peer with 8 failures must take the FIRST
        arm, not be re-marked dormant. Both thresholds are asserted from either side."""
        instance = self._instance(failures, dormant=True)

        instance.update_dormant_gone()

        assert instance.gone_forever is expected
        assert instance.dormant is True

    def test_a_live_peer_is_never_given_up_on_directly(self, env):
        """The two arms are mutually exclusive on `dormant`, so a peer that has never been
        marked dormant cannot skip that step however many failures it has -- which is what
        keeps a single long outage from permanently discarding a peer."""
        instance = self._instance(100, dormant=False)

        instance.update_dormant_gone()

        assert instance.gone_forever is False
        assert instance.dormant is True

    @pytest.mark.parametrize('method', ['can_poll', 'can_event'])
    def test_lemmy_is_not_polled_or_sent_events(self, env, method):
        """Both answer `software != 'lemmy'`. They are separate methods because they are
        separate decisions that happen to agree today, so each gets its own row."""
        lemmy = make_instance('lemmy.example')
        lemmy.software = 'lemmy'
        piefed = make_instance('piefed.example')
        piefed.software = 'piefed'
        db.session.commit()

        assert getattr(lemmy, method)() is False
        assert getattr(piefed, method)() is True


# --------------------------------------------------------------------------
# Counting and measuring a user's things
# --------------------------------------------------------------------------


class TestHowMuchAUserHasPosted:

    def test_posts_and_replies_are_added_together(self, env):
        """Two queries, one sum. A helper that returned only the posts would look right on
        any account that has never replied, which is most new accounts."""
        community = make_community('contentland')
        author = make_user(env.baseline.instance_local, 'prolific', local=True)
        quiet = make_user(env.baseline.instance_local, 'quiet', local=True)
        db.session.commit()
        first = make_post(community, author, ap_id='https://test.piefed.local/c/1')
        make_post(community, author, ap_id='https://test.piefed.local/c/2')
        make_post_reply(first, author, body='mine')
        make_post_reply(first, quiet, body='theirs')
        db.session.commit()

        assert author.num_content() == 3
        assert quiet.num_content() == 1


class TestMeasuringStoredFiles:

    def _file(self, tmp_path, name, size, thumbnail_size=None):
        path = tmp_path / name
        path.write_bytes(b'x' * size)
        thumbnail = None
        if thumbnail_size is not None:
            thumbnail = tmp_path / f'thumb_{name}'
            thumbnail.write_bytes(b'x' * thumbnail_size)
        row = File(file_path=str(path),
                   thumbnail_path=str(thumbnail) if thumbnail else None,
                   source_url=f'https://test.piefed.local/{name}')
        db.session.add(row)
        db.session.commit()
        return row

    def test_a_files_size_is_itself_plus_its_thumbnail(self, env, tmp_path):
        """`File.filesize` adds the two paths, because a stored image costs both. The two
        sizes differ so that a helper counting one of them twice is visible."""
        row = self._file(tmp_path, 'image.png', 100, thumbnail_size=7)

        assert row.filesize() == 107

    def test_a_file_with_no_thumbnail_is_just_itself(self, env, tmp_path):
        assert self._file(tmp_path, 'plain.png', 40).filesize() == 40

    def test_a_path_that_is_not_on_disk_counts_as_nothing(self, env):
        """`os.path.exists` guards both additions. A File row whose bytes have already been
        deleted is a normal state -- `delete_from_disk` leaves the row -- and
        `os.path.getsize` on a missing path is `FileNotFoundError`."""
        row = File(file_path='app/static/media/nothing-here.png',
                   thumbnail_path='app/static/media/nothing-here_thumb.png',
                   source_url='https://test.piefed.local/gone.png')
        db.session.add(row)
        db.session.commit()

        assert row.filesize() == 0

    def test_a_row_with_no_paths_at_all_counts_as_nothing(self, env):
        """The `self.file_path and` half: `os.path.exists(None)` is a `TypeError`, and a
        File whose bytes live only on a CDN has no local path."""
        row = File(source_url='https://test.piefed.local/remote-only.png')
        db.session.add(row)
        db.session.commit()

        assert row.filesize() == 0

    def test_a_users_size_is_their_avatar_plus_their_cover(self, env, tmp_path):
        """`User.filesize` sums the two images an account can have, each through
        `File.filesize`, so the total includes their thumbnails as well."""
        avatar = self._file(tmp_path, 'avatar.png', 10, thumbnail_size=1)
        cover = self._file(tmp_path, 'cover.png', 100, thumbnail_size=2)
        user = make_user(env.baseline.instance_local, 'decorated', local=True)
        user.avatar_id = avatar.id
        user.cover_id = cover.id
        db.session.commit()

        assert user.filesize() == 113

    def test_an_account_with_no_images_measures_zero(self, env):
        """Both `if`s. `self.avatar.filesize()` on an account with no avatar is an
        `AttributeError` on None, and most accounts have neither image."""
        user = make_user(env.baseline.instance_local, 'plainuser', local=True)
        db.session.commit()

        assert user.filesize() == 0

    def test_an_account_with_only_an_avatar_counts_only_that(self, env, tmp_path):
        avatar = self._file(tmp_path, 'avatar.png', 10)
        user = make_user(env.baseline.instance_local, 'halfdecorated', local=True)
        user.avatar_id = avatar.id
        db.session.commit()

        assert user.filesize() == 10


# --------------------------------------------------------------------------
# A user's flair, and a conversation's instances
# --------------------------------------------------------------------------


class TestAUsersFlairInACommunity:

    def test_the_flair_is_scoped_to_the_community(self, env):
        """`UserFlair` is per community per user, so the same account can carry different
        flair in two communities -- and a lookup missing either condition would show one
        community's flair in another."""
        first = make_community('flairland')
        second = make_community('otherland')
        user = make_user(env.baseline.instance_local, 'flaired', local=True)
        db.session.commit()
        db.session.add_all([
            UserFlair(user_id=user.id, community_id=first.id, flair='  a regular  '),
            UserFlair(user_id=user.id, community_id=second.id, flair='newcomer'),
        ])
        db.session.commit()

        assert user.community_flair(first.id) == 'a regular'
        assert user.community_flair(second.id) == 'newcomer'

    def test_no_flair_is_an_empty_string(self, env):
        """`if user_flair else ''`. The value is rendered beside the username, so None would
        print as the word None."""
        community = make_community('flairland')
        user = make_user(env.baseline.instance_local, 'unflaired', local=True)
        db.session.commit()

        assert user.community_flair(community.id) == ''


class TestWhichInstancesAConversationTouches:
    """`Conversation.instances` is what decides where a private message is delivered. It
    excludes instance 1 -- this one, since local members need no delivery -- and
    de-duplicates, since two members on the same peer are one delivery.
    """

    @pytest.fixture
    def seeded(self, env):
        peer = make_instance('dm.example')
        another = make_instance('dm2.example')
        local = make_user(env.baseline.instance_local, 'localtalker', local=True)
        first = make_user(peer, 'peertalker')
        second = make_user(peer, 'peertalker2')
        third = make_user(another, 'othertalker')
        conversation = Conversation(user_id=local.id)
        db.session.add(conversation)
        db.session.commit()
        # `members` is a backref over the `conversation_member` association TABLE -- there
        # is no model -- so the rows are inserted through the table itself.
        for user in (local, first, second, third):
            db.session.execute(conversation_member.insert().values(
                conversation_id=conversation.id, user_id=user.id))
        db.session.commit()
        env.conversation = conversation
        env.peer = peer
        env.another = another
        return env

    def test_each_remote_instance_appears_once(self, seeded):
        domains = sorted(instance.domain for instance in seeded.conversation.instances())

        assert domains == ['dm.example', 'dm2.example']

    def test_this_instance_is_never_in_the_list(self, seeded):
        """`member.instance.id != 1`. Delivering a local member's copy over ActivityPub
        would be this instance posting to its own inbox."""
        assert all(instance.id != 1
                   for instance in seeded.conversation.instances())

    def test_a_conversation_with_only_local_members_touches_nothing(self, env):
        local = make_user(env.baseline.instance_local, 'localtalker', local=True)
        other = make_user(env.baseline.instance_local, 'localtalker2', local=True)
        conversation = Conversation(user_id=local.id)
        db.session.add(conversation)
        db.session.commit()
        for user in (local, other):
            db.session.execute(conversation_member.insert().values(
                conversation_id=conversation.id, user_id=user.id))
        db.session.commit()

        assert conversation.instances() == []
