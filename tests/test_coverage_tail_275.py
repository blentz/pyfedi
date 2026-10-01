"""Round 275: the last coverable lines.

What is left in `app/` after this round is a short list that the findings ledger accounts for
individually: the deferred `/admin/perf_test`, two arms of a WTForms `errors` tuple that need a
ten-megabyte upload, and lines the schema or the control flow makes unreachable. These are the ones
that remain reachable:

    a cross-post appended        `calculate_cross_posts`' `elif len(ncp.cross_posts) < limit` body.
                                 Round 265 covered the refusal at the limit; this is the arm that
                                 does the work.
    a detached actor             `user_view`'s `except DetachedInstanceError`, which exists because
                                 `convert_archived_replies_to_tree` builds temporary User objects
                                 that are not in a session.
    an unknown icon format       `make_image_sizes_async`'s `else: PNG` for a community or user image
                                 whose extension has no rule.
    a refused downvote           `process_downvote`'s two IGNORED arms, which are what stop a vote
                                 quota or a defederated instance from moving a score.
    a search that fills in       `search_for_community`'s debug dispatch of the backfill.
"""
from io import BytesIO
from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.models import Community, File, Post, PostReply, Site, User, UserExtraField
from tests.factories import (make_community, make_community_member, make_instance, make_post,
                             make_post_reply, make_user)


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('lastlines')
    author = make_user(api_baseline.instance_local, 'lastlinesauthor', local=True)
    db.session.commit()
    make_community_member(author, community)
    db.session.commit()
    return SimpleNamespace(app=app, community=community, author=author,
                           baseline=api_baseline)


class TestACrossPostAppendedToAnExistingList:

    def test_a_post_below_the_limit_gains_the_newcomer(self, env):
        """`elif len(ncp.cross_posts) < limit: ncp.cross_posts.append(self.id)`.

        Round 265 covered the refusal for a post already at nine; this is the arm that does the work,
        and the two together are what the limit MEANS. The list is rendered as "also posted in", so
        this is the line that makes a cross-post mutual.
        """
        first = make_post(env.community, env.author, ap_id='https://test.piefed.local/xp/1')
        first.url = 'https://news.example/shared-story'
        first.cross_posts = [9001]
        db.session.commit()
        second = make_post(env.community, env.author, ap_id='https://test.piefed.local/xp/2')
        second.url = 'https://news.example/shared-story'
        db.session.commit()

        second.calculate_cross_posts()
        db.session.commit()

        db.session.expire_all()
        assert db.session.get(Post, first.id).cross_posts == [9001, second.id]

    def test_a_post_with_no_list_yet_is_given_one(self, env):
        """The `if ncp.cross_posts is None` arm beside it: the column starts NULL, so the first
        cross-post has to create the list rather than append to it."""
        first = make_post(env.community, env.author, ap_id='https://test.piefed.local/xp/3')
        first.url = 'https://news.example/another-story'
        first.cross_posts = None
        db.session.commit()
        second = make_post(env.community, env.author, ap_id='https://test.piefed.local/xp/4')
        second.url = 'https://news.example/another-story'
        db.session.commit()

        second.calculate_cross_posts()
        db.session.commit()

        db.session.expire_all()
        assert db.session.get(Post, first.id).cross_posts == [second.id]


class TestAnActorThatIsNotInASession:
    """D1437, REPORTED AND NOT FIXED. `user_view`'s

        if user.extra_fields:
            try:
                extra_fields = user.extra_fields
            except DetachedInstanceError:
                extra_fields = db.session.get(User, user.id).extra_fields

    cannot reach its handler on the installed SQLAlchemy (2.0.52). `User.extra_fields` is a DYNAMIC
    relationship, and a detached instance does not raise for one -- measured:

        SAWarning: Instance <User at 0x...> is detached, dynamic relationship cannot return a
        correct result. This warning will become a DetachedInstanceError in a future release.

    So the collection comes back EMPTY, the `if` above the try is falsy, and the try is never entered.
    The comment names the caller -- `convert_archived_replies_to_tree` builds temporary `User`
    objects -- which means an archived thread's authors silently lose their profile fields today, with
    a warning in the log rather than an error.

    Two ways to fix it, both a maintainer's call: test `inspect(user).detached` and re-read by id
    before the `if`, or have the archived-thread path build attached users. Until then the handler is
    future-proofing for the release that makes it an exception.

    No row here drives the detached case, because doing so puts that SAWarning into the suite -- whose
    warning count this campaign ratchets. The row below covers the arm that does run.
    """

    def test_extra_fields_are_read_for_an_attached_user(self, env):
        from app.api.alpha.views import user_view

        env.author.extra_fields.append(UserExtraField(label='Pronouns', text='they/them'))
        db.session.commit()

        view = user_view(env.author, variant=1)

        assert [field['label'] for field in view['extra_fields']] == ['Pronouns']

    def test_an_account_with_no_extra_fields_publishes_an_empty_list(self, env):
        """The `if user.extra_fields:` guard's other side. Recorded as OBSERVED: the key is present
        and empty rather than absent, because a default is set before this block -- so a client
        reading it needs no `.get()`, and the guard only decides whether the loop runs."""
        from app.api.alpha.views import user_view

        view = user_view(env.author, variant=1)

        assert view['extra_fields'] == []


class TestAnIconFormatWithNoRule:

    def _run(self, env, filename, content, content_type='image/png'):
        """Resize one stored image, with the fetch answered locally.

        Mirrors `tests/test_ap_make_image_sizes.py`'s own helper: the response carries a
        content-type header, and whatever is written under `app/static` is removed afterwards.
        """
        import os

        import httpx

        from app.activitypub.util import make_image_sizes_async

        image = File(source_url=f'https://peer.example/{filename}')
        db.session.add(image)
        db.session.commit()
        with patch('app.activitypub.util.get_request',
                   return_value=httpx.Response(200, content=content,
                                               headers={'content-type': content_type})):
            make_image_sizes_async(image.id, 40, 250, 'users', False)
        db.session.expire_all()
        stored = db.session.get(File, image.id)
        for path in (stored.file_path, stored.thumbnail_path):
            if path and not path.startswith('http') and os.path.exists(path):
                os.unlink(path)
        return stored

    def _png_bytes(self):
        from PIL import Image as PILImage

        buffer = BytesIO()
        PILImage.new('RGB', (400, 300), (10, 120, 200)).save(buffer, format='PNG')
        return buffer.getvalue()

    def test_an_extension_with_no_rule_is_written_as_png(self, env):
        """`else: medium_image_format = thumbnail_image_format = 'PNG'`.

        For the `communities` and `users` directories the format is preserved rather than converted,
        because an avatar is re-federated and peers cache it -- and the map beside this line names
        four extensions. PNG is the fallback for the rest, and it is the right one: it is lossless,
        so a format this instance has no rule for is not degraded on the way through.
        """
        # `file_ext` is derived from the RESPONSE's content type, not from the url -- so the
        # content type is what selects the arm, and `image/tiff` is the one with no rule.
        stored = self._run(env, 'avatar.tiff', self._png_bytes(),
                           content_type='image/tiff')

        assert stored.file_path is not None
        assert stored.file_path.endswith('.png')
        assert stored.thumbnail_path.endswith('.png')

    def test_a_png_stays_a_png(self, env):
        """The arm above it, so the row above is not passing merely because everything becomes
        PNG."""
        stored = self._run(env, 'avatar.png', self._png_bytes())

        assert stored.file_path.endswith('.png')

    def test_a_webp_is_preserved(self, env):
        """The arm that says the map is read at all: a `.webp` avatar stays webp rather than
        becoming the fallback."""
        from PIL import Image as PILImage

        buffer = BytesIO()
        PILImage.new('RGB', (400, 300), (10, 120, 200)).save(buffer, format='WEBP')
        stored = self._run(env, 'avatar.webp', buffer.getvalue(),
                           content_type='image/webp')

        assert stored.file_path.endswith('.webp')


class TestADownvoteThisInstanceWillNotApply:
    """`process_downvote`'s two IGNORED arms. A downvote moves a score that decides what every
    reader sees first, so each refusal is a moderation or anti-abuse rule: a community that does not
    allow downvotes, an account over its daily quota, an author who blocked the voter, or a
    defederated instance.
    """

    @pytest.fixture
    def voted(self, env):
        voter = make_user(env.baseline.instance_remote, 'downvoter')
        voter.ap_profile_id = 'https://remote.piefed.test/u/downvoter'
        db.session.commit()
        post = make_post(env.community, env.author, ap_id='https://test.piefed.local/dv/1')
        db.session.commit()
        env.voter = voter
        env.post = post
        return env

    def _dislike(self, voted, monkeypatch):
        from app.activitypub import routes as ap_routes

        logged = []
        monkeypatch.setattr(ap_routes, 'log_incoming_ap',
                            lambda id, t, r, saved, message=None, session=None:
                            logged.append((r, message)))
        ap_routes.process_downvote(voted.voter, False,
                                   {'id': 'https://remote.piefed.test/activities/dislike/1',
                                    'type': 'Dislike',
                                    'object': voted.post.ap_id}, False)
        return logged

    def test_a_community_that_forbids_downvotes_is_refused(self, voted, monkeypatch):
        """The outer `else`: `can_downvote` said no, so the score does not move and the reason is
        logged rather than the vote silently vanishing."""
        voted.community.downvote_accept_mode = 4  # nobody
        db.session.commit()
        before = voted.post.down_votes

        logged = self._dislike(voted, monkeypatch)

        db.session.expire_all()
        assert db.session.get(Post, voted.post.id).down_votes == before
        assert logged[-1][1] == 'Cannot downvote this'

    def test_a_voter_over_the_daily_quota_is_refused(self, voted, monkeypatch):
        """The INNER `else`, which is a different line with the same message -- the quota is what
        stops one account burying a community's posts in an afternoon."""
        # `votes_cast_today(...) < VOTE_QUOTA` (D501), so a quota of -1 refuses the first vote.
        monkeypatch.setitem(current_app.config, 'VOTE_QUOTA', -1)
        before = voted.post.down_votes

        logged = self._dislike(voted, monkeypatch)

        db.session.expire_all()
        assert db.session.get(Post, voted.post.id).down_votes == before
        assert logged[-1][1] == 'Cannot downvote this'

    def test_an_allowed_downvote_moves_the_score(self, voted, monkeypatch):
        """The control for both, without which either row would pass against a function that
        refuses everything."""
        logged = self._dislike(voted, monkeypatch)

        db.session.expire_all()
        assert db.session.get(Post, voted.post.id).down_votes == 1
        assert logged[-1][1] is None


# `search_for_community`'s own `if current_app.debug:` dispatch of the backfill (line 111) has no row
# here: the handle form webfingers before reaching it, and the url form takes a different branch, so
# covering it needs a webfinger fixture rather than a patch. The same dispatch pattern is covered in
# three other places this session (rounds 266 and 270), and the line is listed in the findings entry
# as outstanding rather than left unexplained.
