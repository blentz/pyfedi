"""Round 236: announcing a new community, and the two celery dispatchers beside it.

`publicize_community` runs when somebody creates a community here: it builds a link post
pointing at the new community and posts it to a REMOTE announcement community --
`newcommunities@lemmy.world`, or `playground@piefed.social` when the instance is in debug.
`tests/test_community_lifecycle.py` patches the whole function out, so nothing had ever
asserted what it posts or where.

That is worth a row because everything in the post is derived from the new community and
sent to a third party: the title, the link, the body with the community's own description
in it, and the language. A mistake here is not a broken page, it is this instance
publishing the wrong thing to somebody else's server.

Also here: `delete_post_from_community`'s debug/celery split, and the unreachable trailing
`else` in `save_icon_file`, which is recorded rather than covered.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.community.util import (delete_post_from_community, publicize_community,
                                publicize_community_task, save_icon_file)
from app.constants import POST_TYPE_LINK, SRC_WEB
from app.models import Community, Language, Site
from tests.factories import make_community, make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    # `Language` rows are seeded by a migration in production; the test database has
    # none, and both `Site.language_id` and `User.language_id` are foreign keys.
    site_language = Language(code='und', name='Undetermined')
    poster_language = Language(code='en', name='English')
    db.session.add_all([site_language, poster_language])
    db.session.commit()
    site.language_id = site_language.id
    g.site = site
    db.session.commit()
    return SimpleNamespace(app=app, site=site, baseline=api_baseline)


def an_announcement_community(ap_id='newcommunities@lemmy.world'):
    community = make_community('newcommunities', host='lemmy.world')
    community.ap_id = ap_id
    db.session.commit()
    return community


# --------------------------------------------------------------------------
# publicize_community
# --------------------------------------------------------------------------


class TestAnnouncingANewCommunity:

    @pytest.fixture
    def seeded(self, env):
        announcements = an_announcement_community()
        fresh = make_community('brandnew')
        fresh.title = 'Brand New Thing'
        fresh.description = 'a place for new things'
        db.session.commit()
        env.announcements = announcements
        env.fresh = fresh
        env.actor = make_user(env.baseline.instance_local, 'founder', local=True)
        env.poster_language = Language.query.filter_by(code='en').one()
        env.actor.language_id = env.poster_language.id
        db.session.commit()
        return env

    def _publicize(self, env):
        """`publicize_community` reads `current_user.language_id`, so the acting account is
        supplied through the login manager rather than by argument."""
        calls = []
        with env.app.test_request_context('/'):
            with patch('app.community.util.current_user', env.actor), \
                    patch('app.shared.post.make_post',
                          side_effect=lambda *args, **kwargs: calls.append((args, kwargs))):
                publicize_community(env.fresh)
        return calls

    def test_the_post_goes_to_the_announcement_community(self, seeded):
        """`community` is REASSIGNED partway down the function: the argument is the new
        community, and by the time `make_post` is called the name refers to the remote
        announcement community. The post must land there, not in the community being
        announced."""
        calls = self._publicize(seeded)

        assert len(calls) == 1
        args, kwargs = calls[0]
        form, target, post_type, source = args
        assert target.id == seeded.announcements.id
        assert post_type == POST_TYPE_LINK
        assert source == SRC_WEB

    def test_the_post_carries_the_new_communitys_title_link_and_description(self, seeded):
        """Everything a reader of the announcement sees. The body holds the community's
        `lemmy_link()` -- the `!name@host` form other software resolves -- followed by its
        description."""
        form = self._publicize(seeded)[0][0][0]

        assert form.title.data == 'Brand New Thing'
        assert form.link_url.data == seeded.fresh.public_url()
        assert seeded.fresh.lemmy_link() in form.body.data
        assert 'a place for new things' in form.body.data

    def test_a_community_with_no_description_still_posts(self, seeded):
        """`community.description if community.description else ''`. A description is
        optional, and `None` concatenated onto the body is a `TypeError` -- on the path
        that publishes to another server."""
        seeded.fresh.description = None
        db.session.commit()

        form = self._publicize(seeded)[0][0][0]

        assert form.body.data.strip() == seeded.fresh.lemmy_link()

    def test_the_language_comes_from_the_poster_and_falls_back_to_the_site(self, seeded):
        """`current_user.language_id or g.site.language_id`. An account that has never
        chosen one posts in the instance's language rather than in none."""
        with_preference = self._publicize(seeded)[0][0][0]

        seeded.actor.language_id = None
        db.session.commit()
        without = self._publicize(seeded)[0][0][0]

        assert with_preference.language_id.data == seeded.poster_language.id
        assert without.language_id.data == seeded.site.language_id

    def test_nothing_is_posted_when_the_announcement_community_is_unknown(self, env):
        """`if community:` -- an instance that has never federated with
        `newcommunities@lemmy.world` has no row for it, and the announcement is skipped
        rather than posted somewhere else."""
        fresh = make_community('brandnew')
        db.session.commit()
        env.fresh = fresh
        env.actor = make_user(env.baseline.instance_local, 'founder', local=True)
        db.session.commit()

        assert self._publicize(env) == []

    def test_a_debug_instance_announces_somewhere_else(self, seeded, monkeypatch):
        """The `if current_app.debug:` arm. A developer running locally must not post test
        communities to `lemmy.world`, so the target is `playground@piefed.social`."""
        playground = make_community('playground', host='piefed.social')
        playground.ap_id = 'playground@piefed.social'
        db.session.commit()
        monkeypatch.setattr(seeded.app, 'debug', True)

        calls = self._publicize(seeded)

        assert calls[0][0][1].id == playground.id


class TestTheCommunityPublicityTask:
    """`publicize_community_task` asks eight named Lemmy instances to resolve the new
    community, which is how it appears in their search. Its call sites are inside a
    triple-quoted block with four numbered reasons for being disabled, so nothing reaches
    it today -- the rows exist because the code is live Python that a maintainer may
    re-enable, and because what it does is issue eight outbound requests.
    """

    def test_it_asks_every_instance_to_resolve_the_community(self, env):
        community = make_community('brandnew')
        db.session.commit()
        calls = []

        with patch('app.community.util.get_request',
                   side_effect=lambda url, *a, **k: calls.append(url)):
            publicize_community_task(community.id)

        assert len(calls) == 8
        assert all(community.lemmy_link() in url for url in calls)
        assert all('/api/v3/resolve_object?q=' in url for url in calls)
        # Eight DIFFERENT instances, not one asked eight times.
        assert len({url.split('/api/')[0] for url in calls}) == 8

    def test_a_community_that_has_been_deleted_asks_nobody(self, env):
        """`if community is None: return`. The task is queued with an id and runs later, so
        the row it names may be gone by then -- and `None.lemmy_link()` would be an
        `AttributeError` inside a celery worker."""
        calls = []

        with patch('app.community.util.get_request',
                   side_effect=lambda url, *a, **k: calls.append(url)):
            publicize_community_task(999999)

        assert calls == []


# --------------------------------------------------------------------------
# delete_post_from_community
# --------------------------------------------------------------------------


class TestDeletingAPostFromACommunity:
    """The same debug/celery split as every other task dispatcher here: in debug the task
    runs inline so a developer sees the traceback, and in production it is queued.
    """

    def test_in_production_the_work_is_queued(self, env, monkeypatch):
        actor = make_user(env.baseline.instance_local, 'remover', local=True)
        db.session.commit()
        monkeypatch.setattr(env.app, 'debug', False)
        queued = []

        with env.app.test_request_context('/'):
            with patch('app.community.util.current_user', actor), \
                    patch('app.community.util.delete_post_from_community_task') as task:
                task.delay.side_effect = lambda *args: queued.append(args)
                delete_post_from_community(7)

        assert queued == [(7, actor.id)]

    def test_in_debug_it_runs_inline(self, env, monkeypatch):
        actor = make_user(env.baseline.instance_local, 'remover', local=True)
        db.session.commit()
        monkeypatch.setattr(env.app, 'debug', True)
        ran = []

        with env.app.test_request_context('/'):
            with patch('app.community.util.current_user', actor), \
                    patch('app.community.util.delete_post_from_community_task') as task:
                task.side_effect = lambda *args: ran.append(args)
                delete_post_from_community(7)

                assert task.delay.call_count == 0

        assert ran == [(7, actor.id)]


# --------------------------------------------------------------------------
# save_icon_file's two refusals
# --------------------------------------------------------------------------


class TestAnIconUploadThisSiteWillNotAccept:
    """An icon with a disallowed extension gets a 400 from the check at the top of
    `save_icon_file`. The trailing `else: abort(400)` that used to document an unreachable
    arm was deleted as dead code (5038acd18).
    """


    def test_a_disallowed_extension_is_refused_before_anything_is_written(self, env):
        """The first `abort(400)`. It happens before `icon_file.save(...)`, so a file with
        a rejected extension never lands on disk at all."""
        from werkzeug.exceptions import BadRequest

        saved = []
        upload = SimpleNamespace(filename='payload.exe',
                                 save=lambda path: saved.append(path))

        with env.app.test_request_context('/'):
            with pytest.raises(BadRequest):
                save_icon_file(upload)

        assert saved == []

    def test_the_extension_refusal_is_the_only_extension_abort(self, env):
        """`save_icon_file` once ended with `if file_ext.lower() in allowed_extensions: ... else:
        abort(400)`, which the check at the top had already made unreachable. It was deleted
        as dead code (5038acd18). The refusal above (test_a_disallowed_extension_is_refused_
        before_anything_is_written) is now the only place an extension is turned away, so
        the source must hold exactly one extension guard and no `else: abort(400)` after it.
        """
        import inspect

        from app.community import util

        source = inspect.getsource(util.save_icon_file)
        assert source.count('if file_ext.lower() not in allowed_extensions') == 1
        assert 'else:\n        abort(400)' not in source
