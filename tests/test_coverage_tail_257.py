"""Round 257: deleting an account without deleting somebody else's images.

`User.delete_dependencies` removes an account's content. The part with no rows is the
reference-counting in front of each file deletion: an image may be referenced by another
account's avatar, or still be attached to a post through `user_file`, and deleting the bytes
would break those. Three `continue`s guard it.

Also here: the account's own attitude calculation, which has a minimum-activity floor before it
means anything; the registration row a pending application leaves behind; and four identity
helpers (`public_url`, `followers_url`, `instance_domain`, `email_domain`) whose fallbacks are what
every federated mention and every outbound delivery is built from.
"""
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.models import (File, Post, PostReply, Site, User, UserRegistration, user_file,
                        utcnow)
from tests.factories import (make_community, make_community_member, make_instance,
                             make_post, make_post_reply, make_post_reply_vote,
                             make_post_vote, make_user)


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('deleteland')
    author = make_user(api_baseline.instance_local, 'leaving', local=True)
    other = make_user(api_baseline.instance_local, 'staying', local=True)
    db.session.commit()
    make_community_member(author, community)
    return SimpleNamespace(app=app, community=community, author=author, other=other,
                           baseline=api_baseline)


@pytest.fixture
def no_disk(monkeypatch):
    """Record which files would have been unlinked rather than touching the filesystem or the
    CDN. Which File is deleted is the whole question here."""
    deleted = []
    monkeypatch.setattr(File, 'delete_from_disk',
                        lambda self, *args, **kwargs: deleted.append(self.source_url))
    return deleted


def an_upload(env, user, url):
    """A File the account uploaded, linked through the `user_file` association table."""
    row = File(source_url=url)
    db.session.add(row)
    db.session.commit()
    db.session.execute(user_file.insert().values(user_id=user.id, file_id=row.id))
    db.session.commit()
    return row


# --------------------------------------------------------------------------
# Whose images survive an account deletion
# --------------------------------------------------------------------------


class TestWhichFilesSurviveAnAccountDeletion:

    def test_an_image_only_this_account_uploaded_is_deleted(self, env, no_disk):
        """The baseline: nothing else references it, so the bytes go."""
        upload = an_upload(env, env.author, 'https://test.piefed.local/mine.png')

        env.author.delete_dependencies()
        db.session.commit()

        assert no_disk == [upload.source_url]

    def test_an_image_another_account_uses_as_an_avatar_is_kept(self, env, no_disk):
        """D1426. Two accounts can point at one File, and deleting the bytes would blank
        somebody else's profile -- but the failure was harder than that: `avatar_id` is a
        foreign key, so the `db.session.delete(file)` beside it raised
        `ForeignKeyViolation ... user_avatar_id_fkey` and aborted the whole
        `delete_dependencies` call, leaving the account half-deleted.

        The loop below this one already checked `User.cover_id` / `User.avatar_id`; this one
        checked only other accounts' `user_file` rows.
        """
        shared = an_upload(env, env.author, 'https://test.piefed.local/shared.png')
        env.other.avatar_id = shared.id
        db.session.commit()

        env.author.delete_dependencies()
        db.session.commit()

        assert no_disk == []

    def test_an_image_another_account_uses_as_a_cover_is_kept_too(self, env, no_disk):
        """The same query covers `cover_id`, which is a separate column -- so a row for each,
        because the `or_` has two halves and one of them could go."""
        shared = an_upload(env, env.author, 'https://test.piefed.local/shared.png')
        env.other.cover_id = shared.id
        db.session.commit()

        env.author.delete_dependencies()
        db.session.commit()

        assert no_disk == []

    def test_an_image_another_account_uploaded_too_is_kept(self, env, no_disk):
        """`if db.session.query(user_file).filter(file_id == ...).count() > 0: continue`. The
        association rows for the leaving account are gone by this point, so a remaining row
        means somebody ELSE still has the image in a post."""
        shared = an_upload(env, env.author, 'https://test.piefed.local/shared.png')
        db.session.execute(user_file.insert().values(user_id=env.other.id,
                                                    file_id=shared.id))
        db.session.commit()

        env.author.delete_dependencies()
        db.session.commit()

        assert no_disk == []

    def test_an_association_cannot_outlive_its_file(self, env, no_disk):
        """`file = db.session.get(File, file_id); if file is None: continue` guards the
        cover/avatar loop, and the equivalent state is NOT reachable for `user_file`: the
        association's `file_id` is a foreign key, so a row pointing at a deleted File cannot
        be inserted (fact 781's shape).

        Recorded rather than covered, because the guard is real for the cover/avatar ids --
        those are read off the User row BEFORE the flush that nulls them, so the File may have
        been deleted by the loop above in between.
        """
        upload = an_upload(env, env.author, 'https://test.piefed.local/gone.png')
        file_id = upload.id
        db.session.execute(
            db.text('DELETE FROM user_file WHERE file_id = :id'), {'id': file_id})
        db.session.delete(upload)
        db.session.commit()

        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            db.session.execute(user_file.insert().values(user_id=env.author.id,
                                                        file_id=file_id))
            db.session.commit()
        db.session.rollback()

    def test_a_profile_image_with_no_upload_row_is_still_reference_counted(self, env,
                                                                          no_disk):
        """The SECOND loop, which handles the account's own `cover_id` and `avatar_id`. Those
        ids are read off the User row before the flush, so a File referenced only as a profile
        image -- with no `user_file` association at all -- never reaches the first loop, and
        this is the only guard standing between it and another account's profile.
        """
        shared = File(source_url='https://test.piefed.local/profile-only.png')
        db.session.add(shared)
        db.session.commit()
        env.author.avatar_id = shared.id
        env.other.avatar_id = shared.id
        db.session.commit()

        env.author.delete_dependencies()
        db.session.commit()

        assert no_disk == []

    def test_a_profile_image_somebody_else_uploaded_is_kept(self, env, no_disk):
        """The second loop's other guard, on `user_file`. The account used somebody else's
        upload as its avatar, so the bytes belong to that upload and not to this profile."""
        shared = File(source_url='https://test.piefed.local/theirs.png')
        db.session.add(shared)
        db.session.commit()
        db.session.execute(user_file.insert().values(user_id=env.other.id,
                                                    file_id=shared.id))
        env.author.avatar_id = shared.id
        db.session.commit()

        env.author.delete_dependencies()
        db.session.commit()

        assert no_disk == []

    def test_the_accounts_own_avatar_is_unlinked_first(self, env, no_disk):
        """`self.avatar_id = None; db.session.flush()` happens BEFORE the reference count, or
        the account's own avatar would count as a live reference to itself and never be
        deleted."""
        avatar = an_upload(env, env.author, 'https://test.piefed.local/avatar.png')
        env.author.avatar_id = avatar.id
        db.session.commit()

        env.author.delete_dependencies()
        db.session.commit()

        assert avatar.source_url in no_disk


class TestAPendingApplication:

    def test_a_registration_awaiting_review_is_removed_with_the_account(self, env):
        """`if self.waiting_for_approval()`. An application row left behind is an entry in the
        admin's queue for an account that no longer exists."""
        db.session.add(UserRegistration(user_id=env.author.id, answer='because',
                                        status=0))
        db.session.commit()

        env.author.delete_dependencies()
        db.session.commit()

        assert UserRegistration.query.filter_by(user_id=env.author.id).count() == 0

    def test_an_approved_registration_is_left_alone(self, env):
        """`waiting_for_approval()` is `status == 0`. An approved application is a record of a
        decision an admin made, and the row asserts deletion does not take it."""
        db.session.add(UserRegistration(user_id=env.author.id, answer='because',
                                        status=1, approved_at=utcnow()))
        db.session.commit()

        env.author.delete_dependencies()
        db.session.commit()

        assert UserRegistration.query.filter_by(user_id=env.author.id).count() == 1


# --------------------------------------------------------------------------
# The attitude score
# --------------------------------------------------------------------------


class TestAnAccountsAttitude:
    """`recalculate_attitude` is upvotes minus downvotes over the total, and it is shown to
    moderators as a hint about whether somebody mostly downvotes. Under ten votes it is None
    rather than a number, because a score computed from three votes says nothing.
    """

    @pytest.fixture
    def seeded(self, env):
        post = make_post(env.community, env.other,
                         ap_id='https://test.piefed.local/a/1')
        db.session.commit()
        env.post = post
        env.replies = []
        for n in range(12):
            reply = make_post_reply(post, env.other, body=f'reply {n}')
            db.session.commit()
            env.replies.append(reply)
        return env

    def test_an_account_with_too_few_votes_has_no_attitude(self, seeded):
        """Nine votes is not ten. The floor is why a new account is not judged on its first
        few clicks."""
        for reply in seeded.replies[:9]:
            make_post_reply_vote(seeded.author, reply, -1.0)
        db.session.commit()

        seeded.author.recalculate_attitude()
        db.session.commit()

        assert seeded.author.attitude is None

    def test_an_account_that_only_downvotes_scores_minus_one(self, seeded):
        for reply in seeded.replies[:10]:
            make_post_reply_vote(seeded.author, reply, -1.0)
        db.session.commit()

        seeded.author.recalculate_attitude()
        db.session.commit()

        assert seeded.author.attitude == pytest.approx(-1.0)

    def test_an_account_that_only_upvotes_scores_one(self, seeded):
        for reply in seeded.replies[:10]:
            make_post_reply_vote(seeded.author, reply, 1.0)
        db.session.commit()

        seeded.author.recalculate_attitude()
        db.session.commit()

        assert seeded.author.attitude == pytest.approx(1.0)

    def test_an_even_split_scores_zero(self, seeded):
        """The arithmetic, at the one value where a sign error is invisible in the other two
        rows."""
        for reply in seeded.replies[:6]:
            make_post_reply_vote(seeded.author, reply, 1.0)
        for reply in seeded.replies[6:12]:
            make_post_reply_vote(seeded.author, reply, -1.0)
        db.session.commit()

        seeded.author.recalculate_attitude()
        db.session.commit()

        assert seeded.author.attitude == pytest.approx(0.0)

    def test_votes_on_posts_count_as_well_as_on_replies(self, seeded):
        """Two queries, one total. An account that only ever votes on posts would otherwise
        never reach the floor."""
        posts = [make_post(seeded.community, seeded.other,
                           ap_id=f'https://test.piefed.local/a/{n + 2}')
                 for n in range(10)]
        db.session.commit()
        for post in posts:
            make_post_vote(seeded.author, post, 1.0)
        db.session.commit()

        seeded.author.recalculate_attitude()
        db.session.commit()

        assert seeded.author.attitude == pytest.approx(1.0)


# --------------------------------------------------------------------------
# How an account names itself
# --------------------------------------------------------------------------


class TestHowAnAccountNamesItself:

    def test_a_local_account_is_named_with_this_instances_domain(self, env):
        """`lemmy_link` is the `name@host` form other software resolves, and for a local
        account the host is this instance -- there is no `ap_id` to read it from."""
        assert env.author.lemmy_link() == \
            f"{env.author.user_name}@{env.app.config['SERVER_NAME']}"

    def test_a_remote_accounts_handle_is_lower_cased(self, env):
        """`self.ap_id.lower()`. The handle is the lookup key for a mention, so
        `@Somebody@Handle.Example` and `@somebody@handle.example` have to name one account."""
        peer = make_instance('handle.example')
        remote = make_user(peer, 'Somebody')
        remote.ap_id = 'Somebody@Handle.Example'
        db.session.commit()

        assert remote.lemmy_link() == 'somebody@handle.example'

    def test_a_remote_accounts_link_keeps_its_case(self, env):
        """`link()` is the other one, and it does NOT lower-case: it is what the UI prints, so
        an account whose name has capitals keeps them. Two helpers, two answers, from the same
        column."""
        peer = make_instance('handle.example')
        remote = make_user(peer, 'Somebody')
        remote.ap_id = 'Somebody@Handle.Example'
        db.session.commit()

        assert remote.link() == 'Somebody@Handle.Example'
        assert env.author.link() == env.author.user_name

    def test_a_followers_url_falls_back_to_the_public_url(self, env):
        """`ap_followers_url` is optional in an actor document, and this instance has to
        deliver to SOMETHING -- so the fallback is the conventional `<actor>/followers`."""
        env.author.ap_followers_url = None
        db.session.commit()

        assert env.author.followers_url() == env.author.public_url() + '/followers'

    def test_a_recorded_followers_url_is_used_as_given(self, env):
        """A peer may put the collection anywhere, so the recorded value wins over the
        convention."""
        env.author.ap_followers_url = 'https://peer.example/u/leaving/subscribers'
        db.session.commit()

        assert env.author.followers_url() == \
            'https://peer.example/u/leaving/subscribers'

    def test_a_local_account_reports_this_instances_domain(self, env):
        """`instance_domain` has three arms in order: the recorded `ap_domain`, then this
        instance's name for a local account, then the Instance row's domain.

        The middle arm is an EQUIVALENT MUTANT and cannot be otherwise: a local account's
        Instance row IS this instance, so its `domain` and `SERVER_NAME` are the same string.
        The arm exists so the answer does not depend on that row being present and correct.
        """
        env.author.ap_domain = None
        db.session.commit()

        assert env.author.instance_domain() == env.app.config['SERVER_NAME']

    def test_a_remote_account_with_no_recorded_domain_uses_its_instance_row(self, env):
        peer = make_instance('domain.example')
        remote = make_user(peer, 'faraway')
        remote.ap_id = 'faraway@domain.example'
        remote.ap_profile_id = 'https://domain.example/u/faraway'
        remote.ap_domain = None
        db.session.commit()

        assert remote.instance_domain() == 'domain.example'

    def test_a_recorded_domain_wins_over_both(self, env):
        peer = make_instance('domain.example')
        remote = make_user(peer, 'faraway')
        remote.ap_id = 'faraway@domain.example'
        remote.ap_domain = 'vanity.example'
        db.session.commit()

        assert remote.instance_domain() == 'vanity.example'

    @pytest.mark.parametrize('email,expected', [
        ('someone@example.com', 'example.com'),
        ('someone@mail.example.com', 'mail.example.com'),
    ])
    def test_the_email_domain_is_the_part_after_the_at(self, env, email, expected):
        env.author.email = email
        db.session.commit()

        assert env.author.email_domain() == expected
