"""The post API's actions: voting, saving, subscribing, writing, editing,
deleting, reporting, moderating, flair and poll votes.

Sub-project 84, slice L -- `app/api/alpha/utils/post.py` without the listing.
Five defects, all measured first:

* FOURTEEN endpoints read attributes off a lookup that answers None, so
  naming a post, community or report that does not exist was
  `'NoneType' object has no attribute 'community'` rather than an answer --
  D1194 and D1202's shape, a third time (D1218);
* `put_post_report_resolve` tested `not report.suspect_post_id and
  report.suspect_post_reply_id`, which refuses a report against a COMMENT
  and lets every other kind through: a report against a person, a community
  or a conversation names neither, so it walked past the guard and died on
  `community.moderators()` with `in_community_id` None (D1217);
* `post_post` and `put_post` both tested `language_id < 2` on a value that
  can be None -- `site_language_id()` answers None on an instance whose
  languages are not seeded, and `Post.language_id` is nullable (D1219);
* `put_post` defaulted the body to `post.body`, which is NULL for every link
  and image post, and `edit_post` runs a regex over it: no bodyless post
  could be edited at all, not even to fix its title (D1220);
* `post_post_feature` bound neither `post` nor `user_id` for a `feature_type`
  that is neither Community nor Local, so the response it then built was an
  UnboundLocalError (D1221).
"""
from unittest.mock import patch

import pytest
from flask import current_app, g

from app.api.alpha.utils.post import (get_post_like_list, get_post_report_list,
                                      post_poll_vote, post_post,
                                      post_post_delete, post_post_feature,
                                      post_post_hide, post_post_like,
                                      post_post_lock, post_post_mark_as_read,
                                      post_post_remove, post_post_report,
                                      put_post, put_post_report_resolve,
                                      put_post_save, put_post_set_flair,
                                      put_post_subscribe)
from app import db
from app.constants import (POST_TYPE_ARTICLE, POST_TYPE_LINK, POST_TYPE_POLL,
                           REPORT_STATE_NEW, REPORT_STATE_RESOLVED)
from app.models import (Community, Language, Post, PostBookmark, PostVote,
                        Report, Role, RolePermission, Site, User,
                        NotificationSubscription, read_posts, user_role)
from tests.factories import (a_keypair, make_community, make_community_ban,
                             make_community_flair, make_community_member,
                             make_poll, make_poll_choice, make_post,
                             make_post_vote, make_user)

MISSING = 999999


def token(user):
    return f'Bearer {user.encode_jwt_token()}'


def with_permission(user, permission):
    role = Role(name=f'role-{user.id}-{permission}', weight=10)
    db.session.add(role)
    db.session.commit()
    db.session.add(RolePermission(role_id=role.id, permission=permission))
    db.session.execute(user_role.insert().values(user_id=user.id,
                                                 role_id=role.id))
    db.session.commit()
    return user


@pytest.fixture
def env(app, api_baseline):
    """`author` wrote `post` in the local community `probeland`, which
    `moderator` moderates and `stranger` has nothing to do with.

    Every account that writes here carries keys: `can_create_post` refuses a
    local account with no `private_key`, which surfaces as "You are not
    permitted to make posts in this community".
    """
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    for code, name in [('en', 'English'), ('und', 'Undetermined'),
                       ('fr', 'French')]:
        db.session.add(Language(code=code, name=name))
    db.session.commit()
    db.session.get(Site, 1).language_id = Language.query.filter_by(
        code='en').one().id
    community = make_community('probeland')
    author = api_baseline.user2
    moderator = api_baseline.user4
    stranger = api_baseline.user3
    for who in (author, moderator, stranger):
        who.private_key, who.public_key = a_keypair()
    db.session.commit()
    make_community_member(author, community)
    make_community_member(moderator, community, is_moderator=True)
    post = make_post(community, author, ap_id='https://test.piefed.local/p/1')
    post.body = 'a body'
    post.body_html = '<p>a body</p>'
    db.session.commit()
    return SimpleNamespace(community=community, author=author, post=post,
                           moderator=moderator, stranger=stranger,
                           admin=api_baseline.user1, baseline=api_baseline)


# ------------------------------------------------ the id nobody holds (D1218)

class TestAPostNobodyHolds:
    """D1218. Every one of these read an attribute off `db.session.get(...)`
    without testing it first."""

    @pytest.mark.parametrize('call, data', [
        (put_post, {'post_id': MISSING, 'title': 'x'}),
        (post_post_report, {'post_id': MISSING, 'reason': 'spam'}),
        (post_post_like, {'post_id': MISSING, 'score': 1}),
        (put_post_save, {'post_id': MISSING, 'save': True}),
        (put_post_subscribe, {'post_id': MISSING, 'subscribe': True}),
        (post_post_delete, {'post_id': MISSING, 'deleted': True}),
        (post_post_lock, {'post_id': MISSING, 'locked': True}),
        (post_post_hide, {'post_id': MISSING, 'hidden': True}),
        (post_post_remove, {'post_id': MISSING, 'removed': True}),
        (get_post_like_list, {'post_id': MISSING}),
        (put_post_set_flair, {'post_id': MISSING, 'flair_id_list': []}),
        (post_poll_vote, {'post_id': MISSING, 'choice_id': 1}),
        (get_post_report_list, {'post_id': MISSING}),
        (post_post_feature, {'post_id': MISSING, 'featured': True,
                             'feature_type': 'Local'}),
    ])
    def test_a_post_nobody_holds_is_refused_by_name(self, env, call, data):
        with pytest.raises(Exception, match='post not found'):
            call(token(env.author), data)

    def test_a_community_nobody_holds_is_refused_by_name(self, env):
        with pytest.raises(Exception, match='community not found'):
            get_post_report_list(token(env.author),
                                 {'community_id': MISSING})

    def test_writing_into_a_community_nobody_holds(self, env):
        with pytest.raises(Exception, match='community not found'):
            post_post(token(env.author),
                      {'title': 'hello', 'community_id': MISSING})

    def test_a_report_nobody_holds_is_refused_by_name(self, env):
        with pytest.raises(Exception, match='report not found'):
            put_post_report_resolve(token(env.admin),
                                    {'report_id': MISSING, 'resolved': True})


# -------------------------------------------------------------------- voting

class TestVoting:
    def test_an_upvote(self, env):
        res = post_post_like(token(env.stranger),
                             {'post_id': env.post.id, 'score': 1})
        assert res['post_view']['my_vote'] == 1
        assert PostVote.query.filter_by(user_id=env.stranger.id,
                                        post_id=env.post.id).one().effect == 1

    def test_a_downvote(self, env):
        post_post_like(token(env.stranger),
                       {'post_id': env.post.id, 'score': -1})
        assert PostVote.query.filter_by(user_id=env.stranger.id,
                                        post_id=env.post.id).one().effect == -1

    def test_a_score_of_anything_else_takes_the_vote_back(self, env):
        post_post_like(token(env.stranger),
                       {'post_id': env.post.id, 'score': 1})
        res = post_post_like(token(env.stranger),
                             {'post_id': env.post.id, 'score': 0})
        assert res['post_view']['my_vote'] == 0
        assert PostVote.query.filter_by(user_id=env.stranger.id,
                                        post_id=env.post.id).count() == 0

    def test_an_account_that_votes_privately_is_followed(self, env):
        env.stranger.vote_privately = True
        db.session.commit()
        post_post_like(token(env.stranger),
                       {'post_id': env.post.id, 'score': 1})
        assert PostVote.query.filter_by(user_id=env.stranger.id,
                                        post_id=env.post.id).one().effect == 1

    def test_the_request_can_override_the_account_setting(self, env):
        """`private` decides the `federate` argument, which is what tells the
        rest of the fediverse the vote happened -- the response looks the same
        either way, so the task's own argument is what has to be read."""
        env.stranger.vote_privately = True
        db.session.commit()
        with patch('app.shared.post.task_selector') as task:
            res = post_post_like(token(env.stranger),
                                 {'post_id': env.post.id, 'score': 1,
                                  'private': False})
        assert res['post_view']['my_vote'] == 1
        assert task.call_args.kwargs['federate'] is True

    def test_a_private_vote_is_not_federated(self, env):
        env.stranger.vote_privately = True
        db.session.commit()
        with patch('app.shared.post.task_selector') as task:
            post_post_like(token(env.stranger),
                           {'post_id': env.post.id, 'score': 1})
        assert task.call_args.kwargs['federate'] is False


class TestSaveAndSubscribe:
    def test_saving_and_unsaving(self, env):
        put_post_save(token(env.stranger),
                      {'post_id': env.post.id, 'save': True})
        assert PostBookmark.query.filter_by(
            user_id=env.stranger.id, post_id=env.post.id).count() == 1
        put_post_save(token(env.stranger),
                      {'post_id': env.post.id, 'save': False})
        assert PostBookmark.query.filter_by(
            user_id=env.stranger.id, post_id=env.post.id).count() == 0

    def test_subscribing_and_unsubscribing(self, env):
        put_post_subscribe(token(env.stranger),
                           {'post_id': env.post.id, 'subscribe': True})
        assert NotificationSubscription.query.filter_by(
            user_id=env.stranger.id, entity_id=env.post.id).count() == 1
        put_post_subscribe(token(env.stranger),
                           {'post_id': env.post.id, 'subscribe': False})
        assert NotificationSubscription.query.filter_by(
            user_id=env.stranger.id, entity_id=env.post.id).count() == 0


# -------------------------------------------------------- writing and editing

class TestWriting:
    def test_a_post_with_nothing_but_a_title(self, env):
        res = post_post(token(env.author),
                        {'title': 'hello', 'community_id': env.community.id})
        post = db.session.get(Post, res['post_view']['post']['id'])
        assert post.title == 'hello'
        assert post.type == POST_TYPE_ARTICLE
        assert post.nsfw is False

    def test_a_before_post_create_plugin_can_rewrite_the_title_and_body(self, env):
        """D811, fixed (owner ruling 2026-09-30). The call site used to discard
        `fire_hook`'s return, so a before-hook could not do what `fire_hook`
        documents. The returned title and content now become the post's; the
        identity and routing fields (user, community, type) are not the
        plugin's to change and still come from the request.
        """
        def rewrite(hook_name, data=None, **kwargs):
            if hook_name != 'before_post_create':
                return data
            return {**data, 'title': 'rewritten title', 'content': 'rewritten body',
                    'community_id': -1, 'user_id': -1}

        with patch('app.plugins.fire_hook', side_effect=rewrite):
            res = post_post(token(env.author),
                            {'title': 'hello', 'body': 'original', 'community_id': env.community.id})

        post = db.session.get(Post, res['post_view']['post']['id'])
        assert post.title == 'rewritten title'
        assert post.body == 'rewritten body'
        assert post.community_id == env.community.id
        assert post.user_id == env.author.id

    def test_a_url_makes_it_a_link_post(self, env):
        # A link post asks the far end what it is: is_image_url sends a HEAD
        # request and opengraph_parse fetches the page for a thumbnail.
        with patch('app.shared.post.is_image_url', return_value=False), \
                patch('app.shared.post.opengraph_parse', return_value=None):
            res = post_post(token(env.author),
                            {'title': 'hello',
                             'community_id': env.community.id,
                             'url': 'https://example.test/thing'})
        assert db.session.get(
            Post, res['post_view']['post']['id']).type == POST_TYPE_LINK

    def test_a_poll_ends_in_three_days_unless_told_otherwise(self, env):
        from datetime import timedelta

        from app.models import Poll, utcnow
        before = utcnow()
        res = post_post(token(env.author),
                        {'title': 'which', 'community_id': env.community.id,
                         'poll': {'choices': ['a', 'b'], 'mode': 'single',
                                  'local_only': False}})
        post = db.session.get(Post, res['post_view']['post']['id'])
        assert post.type == POST_TYPE_POLL
        poll = db.session.get(Poll, post.id)
        assert timedelta(days=2) < poll.end_poll - before < timedelta(days=4)

    def test_a_poll_can_say_when_it_ends(self, env):
        from app.models import Poll, utcnow
        from datetime import timedelta
        ends = (utcnow() + timedelta(days=9)).isoformat(
            timespec='microseconds')
        res = post_post(token(env.author),
                        {'title': 'which', 'community_id': env.community.id,
                         'poll': {'choices': ['a', 'b'], 'mode': 'single',
                                  'local_only': False, 'end_poll': ends}})
        poll = db.session.get(Poll, res['post_view']['post']['id'])
        assert (poll.end_poll - db.session.get(Post, res['post_view']['post']['id']).posted_at
                > timedelta(days=8))

    def test_an_event_post(self, env):
        res = post_post(token(env.author),
                        {'title': 'a gathering',
                         'community_id': env.community.id,
                         'event': {'start': '2030-01-01T10:00:00.000000Z',
                                   'end': '2030-01-01T12:00:00.000000Z',
                                   'timezone': 'UTC'}})
        from app.constants import POST_TYPE_EVENT
        assert db.session.get(
            Post, res['post_view']['post']['id']).type == POST_TYPE_EVENT

    def test_an_instance_with_no_language_seeded(self, env):
        """D1219. `site_language_id()` answers None there, and `None < 2` is a
        TypeError -- so on such an instance NO post could be written."""
        db.session.get(Site, 1).language_id = None
        Language.query.delete()
        db.session.commit()
        res = post_post(token(env.author),
                        {'title': 'hello', 'community_id': env.community.id})
        assert db.session.get(Post, res['post_view']['post']['id']).language_id is None

    def test_a_language_that_is_named_is_kept(self, env):
        french = Language.query.filter_by(code='fr').one()
        res = post_post(token(env.author),
                        {'title': 'bonjour',
                         'community_id': env.community.id,
                         'language_id': french.id})
        assert db.session.get(
            Post, res['post_view']['post']['id']).language_id == french.id

    def test_a_language_below_the_first_real_one_falls_back(self, env):
        res = post_post(token(env.author),
                        {'title': 'hello', 'community_id': env.community.id,
                         'language_id': 1})
        assert db.session.get(Post, res['post_view']['post']['id']).language_id == \
            db.session.get(Site, 1).language_id


class TestEditing:
    def test_a_post_with_no_body_can_still_be_edited(self, env):
        """D1220. `post.body` is NULL for every link and image post, and
        `edit_post` runs a regex substitution over whatever it is given."""
        env.post.body = None
        env.post.body_html = None
        db.session.commit()
        res = put_post(token(env.author),
                       {'post_id': env.post.id, 'title': 'a new title'})
        assert res['post_view']['post']['title'] == 'a new title'

    def test_a_post_whose_language_is_null_can_still_be_edited(self, env):
        """D1219, on the editing side: `Post.language_id` is nullable."""
        env.post.language_id = None
        db.session.commit()
        put_post(token(env.author),
                 {'post_id': env.post.id, 'title': 'a new title'})
        assert db.session.get(Post, env.post.id).title == 'a new title'

    def test_naming_no_field_keeps_what_is_there(self, env):
        put_post(token(env.author), {'post_id': env.post.id})
        post = db.session.get(Post, env.post.id)
        assert post.title == 'a post'
        assert post.body == 'a body'

    def test_every_field_at_once(self, env):
        put_post(token(env.author),
                 {'post_id': env.post.id, 'title': 'new', 'body': 'new body',
                  'nsfw': True, 'ai_generated': True, 'language_id': 1,
                  'tags': '', 'flair': '', 'alt_text': 'a description'})
        post = db.session.get(Post, env.post.id)
        assert post.title == 'new'
        assert post.body == 'new body'
        assert post.nsfw is True
        assert post.ai_generated is True

    def test_a_language_already_set_is_kept(self, env):
        # `fr`, not `en`: language ids below 2 are the "undetermined" range
        # this endpoint replaces with the site's own, so a test using the
        # first seeded language would exercise the fallback instead.
        french = Language.query.filter_by(code='fr').one()
        assert french.id >= 2
        env.post.language_id = french.id
        db.session.commit()
        put_post(token(env.author),
                 {'post_id': env.post.id, 'title': 'a new title'})
        assert db.session.get(Post, env.post.id).language_id == french.id

    def test_the_alt_text_already_on_the_picture_is_kept(self, env):
        # The post needs a url: `edit_post` only writes the alt text back when
        # `url and post.image`, and it writes whatever it was handed -- so a
        # bodyless default of '' would WIPE the description that is there.
        from tests.factories import make_file
        picture = make_file(file_path='/static/p.png')
        picture.alt_text = 'a picture of something'
        env.post.image = picture
        env.post.url = 'https://example.test/p.png'
        db.session.commit()
        with patch('app.shared.post.is_image_url', return_value=False), \
                patch('app.shared.post.opengraph_parse', return_value=None):
            put_post(token(env.author),
                     {'post_id': env.post.id, 'title': 'a new title'})
        assert db.session.get(
            Post, env.post.id).image.alt_text == 'a picture of something'

    def test_an_event_can_be_edited(self, env):
        res = post_post(token(env.author),
                        {'title': 'a gathering',
                         'community_id': env.community.id,
                         'event': {'start': '2030-01-01T10:00:00.000000Z',
                                   'end': '2030-01-01T12:00:00.000000Z',
                                   'timezone': 'UTC'}})
        post_id = res['post_view']['post']['id']
        put_post(token(env.author),
                 {'post_id': post_id, 'title': 'a later gathering',
                  'event': {'start': '2030-02-01T10:00:00.000000Z',
                            'end': '2030-02-01T12:00:00.000000Z',
                            'timezone': 'UTC'}})
        post = db.session.get(Post, post_id)
        assert post.title == 'a later gathering'
        assert post.event.start.month == 2

    def test_a_poll_can_be_edited(self, env):
        res = post_post(token(env.author),
                        {'title': 'which', 'community_id': env.community.id,
                         'poll': {'choices': ['a', 'b'], 'mode': 'single',
                                  'local_only': False}})
        post_id = res['post_view']['post']['id']
        put_post(token(env.author),
                 {'post_id': post_id, 'title': 'which one',
                  'poll': {'choices': ['a', 'b', 'c'], 'mode': 'single',
                           'local_only': False}})
        assert db.session.get(Post, post_id).title == 'which one'

    def test_adding_a_url_makes_it_a_link_post(self, env):
        with patch('app.shared.post.is_image_url', return_value=False), \
                patch('app.shared.post.opengraph_parse', return_value=None):
            put_post(token(env.author),
                     {'post_id': env.post.id,
                      'url': 'https://example.test/x'})
        assert db.session.get(Post, env.post.id).type == POST_TYPE_LINK

    def test_taking_the_url_away_makes_it_an_article_again(self, env):
        env.post.url = 'https://example.test/x'
        env.post.type = POST_TYPE_LINK
        db.session.commit()
        with patch('app.shared.post.is_image_url', return_value=False), \
                patch('app.shared.post.opengraph_parse', return_value=None):
            put_post(token(env.author),
                     {'post_id': env.post.id, 'url': None})
        assert db.session.get(Post, env.post.id).type == POST_TYPE_ARTICLE


class TestDeleting:
    def test_deleting_and_restoring(self, env):
        post_post_delete(token(env.author),
                         {'post_id': env.post.id, 'deleted': True})
        assert db.session.get(Post, env.post.id).deleted is True
        post_post_delete(token(env.author),
                         {'post_id': env.post.id, 'deleted': False})
        assert db.session.get(Post, env.post.id).deleted is False


# ------------------------------------------------------------------ reporting

class TestReporting:
    def test_a_report_is_written(self, env):
        res = post_post_report(token(env.stranger),
                               {'post_id': env.post.id, 'reason': 'spam',
                                'description': 'it is spam'})
        report = Report.query.filter_by(suspect_post_id=env.post.id).one()
        assert report.reasons == 'spam'
        assert report.description == 'it is spam'
        assert res['post_report_view']['post_report']['reason'] == 'spam'


class TestRemoteReporting:
    @pytest.fixture
    def remote_author(self, env, api_baseline):
        """A post written by an account on another instance: `report_remote`
        decides whether that instance is told."""
        elsewhere = make_user(api_baseline.instance_remote, 'faraway')
        post = make_post(env.community, elsewhere,
                         ap_id='https://remote.test/p/1')
        db.session.commit()
        return post

    def test_a_report_reaches_the_suspect_instance_by_default(self, env,
                                                              remote_author,
                                                              api_baseline):
        with patch('app.shared.post.task_selector') as task:
            post_post_report(token(env.stranger),
                             {'post_id': remote_author.id, 'reason': 'spam'})
        assert task.call_args.kwargs['instance_ids'] == \
            [api_baseline.instance_remote.id]

    def test_a_report_can_be_kept_local(self, env, remote_author):
        with patch('app.shared.post.task_selector') as task:
            post_post_report(token(env.stranger),
                             {'post_id': remote_author.id, 'reason': 'spam',
                              'report_remote': False})
        task.assert_not_called()


class TestReportListing:
    @pytest.fixture
    def report(self, env):
        post_post_report(token(env.stranger),
                         {'post_id': env.post.id, 'reason': 'spam'})
        return Report.query.filter_by(suspect_post_id=env.post.id).one()

    def test_an_account_with_no_standing_cannot_read_one_posts_reports(
            self, env, report):
        with pytest.raises(Exception, match='incorrect login'):
            get_post_report_list(token(env.stranger),
                                 {'post_id': env.post.id})

    def test_a_moderator_reads_one_posts_reports(self, env, report):
        res = get_post_report_list(token(env.moderator),
                                   {'post_id': env.post.id})
        assert [item['post_report']['id'] for item in res['post_reports']] == \
            [report.id]

    def test_an_administrator_reads_one_posts_reports(self, env, report):
        with_permission(env.stranger, 'administer all communities')
        res = get_post_report_list(token(env.stranger),
                                   {'post_id': env.post.id})
        assert len(res['post_reports']) == 1

    def test_an_account_with_no_standing_cannot_read_a_communitys_reports(
            self, env, report):
        with pytest.raises(Exception, match='incorrect login'):
            get_post_report_list(token(env.stranger),
                                 {'community_id': env.community.id})

    def test_a_moderator_reads_a_communitys_reports(self, env, report):
        res = get_post_report_list(token(env.moderator),
                                   {'community_id': env.community.id})
        assert len(res['post_reports']) == 1

    def test_an_administrator_reads_a_communitys_reports(self, env, report):
        with_permission(env.stranger, 'administer all communities')
        res = get_post_report_list(token(env.stranger),
                                   {'community_id': env.community.id})
        assert len(res['post_reports']) == 1

    def test_an_administrator_reads_every_report(self, env, report):
        with_permission(env.stranger, 'administer all communities')
        res = get_post_report_list(token(env.stranger), {})
        assert len(res['post_reports']) == 1

    def test_a_moderator_reads_only_what_they_moderate(self, env, report):
        elsewhere = make_community('elsewhere')
        other_post = make_post(elsewhere, env.author,
                               ap_id='https://test.piefed.local/p/2')
        db.session.add(Report(reporter_id=env.stranger.id, reasons='spam',
                              type=1, suspect_post_id=other_post.id,
                              suspect_user_id=env.author.id,
                              in_community_id=elsewhere.id))
        db.session.commit()
        res = get_post_report_list(token(env.moderator), {})
        assert [item['post_report']['id']
                for item in res['post_reports']] == [report.id]

    def test_a_resolved_report_is_left_out_by_default(self, env, report):
        report.status = REPORT_STATE_RESOLVED
        db.session.commit()
        res = get_post_report_list(token(env.moderator),
                                   {'post_id': env.post.id})
        assert res['post_reports'] == []

    def test_a_resolved_report_can_be_asked_for(self, env, report):
        report.status = REPORT_STATE_RESOLVED
        db.session.commit()
        res = get_post_report_list(token(env.moderator),
                                   {'post_id': env.post.id,
                                    'unresolved_only': False})
        assert len(res['post_reports']) == 1

    def test_the_listing_pages(self, env, report):
        for index in range(2, 4):
            other = make_post(env.community, env.author,
                              ap_id=f'https://test.piefed.local/p/{index}')
            db.session.add(Report(reporter_id=env.stranger.id, reasons='spam',
                                  type=1, suspect_post_id=other.id,
                                  suspect_user_id=env.author.id,
                                  in_community_id=env.community.id))
        db.session.commit()
        first = get_post_report_list(token(env.moderator), {'limit': 2})
        assert len(first['post_reports']) == 2
        assert first['next_page'] == '2'
        second = get_post_report_list(token(env.moderator),
                                      {'limit': 2, 'page': 2})
        assert len(second['post_reports']) == 1
        assert second['next_page'] is None


class TestReportResolution:
    @pytest.fixture
    def report(self, env):
        post_post_report(token(env.stranger),
                         {'post_id': env.post.id, 'reason': 'spam'})
        return Report.query.filter_by(suspect_post_id=env.post.id).one()

    def test_a_report_against_a_person_is_not_this_endpoints_business(self, env):
        """D1217. The guard read `not suspect_post_id AND
        suspect_post_reply_id`, so it refused a report against a comment and
        let a report against a person, a community or a conversation straight
        through -- to die on `community.moderators()` with `in_community_id`
        None."""
        against_a_person = Report(reporter_id=env.stranger.id, reasons='spam',
                                  type=0, suspect_user_id=env.author.id)
        db.session.add(against_a_person)
        db.session.commit()
        with pytest.raises(Exception, match='invalid target of resolution'):
            put_post_report_resolve(token(env.admin),
                                    {'report_id': against_a_person.id,
                                     'resolved': True})

    def test_a_report_against_a_comment_is_not_this_endpoints_business(
            self, env):
        from tests.factories import make_post_reply
        reply = make_post_reply(env.post, env.author, body='a reply')
        against_a_comment = Report(reporter_id=env.stranger.id, reasons='spam',
                                   type=2, suspect_post_reply_id=reply.id,
                                   in_community_id=env.community.id)
        db.session.add(against_a_comment)
        db.session.commit()
        with pytest.raises(Exception, match='invalid target of resolution'):
            put_post_report_resolve(token(env.admin),
                                    {'report_id': against_a_comment.id,
                                     'resolved': True})

    def test_a_moderator_resolves_and_reopens(self, env, report):
        put_post_report_resolve(token(env.moderator),
                                {'report_id': report.id, 'resolved': True})
        assert db.session.get(Report, report.id).status == REPORT_STATE_RESOLVED
        put_post_report_resolve(token(env.moderator),
                                {'report_id': report.id, 'resolved': False})
        assert db.session.get(Report, report.id).status == REPORT_STATE_NEW

    def test_an_administrator_resolves(self, env, report):
        with_permission(env.stranger, 'administer all communities')
        put_post_report_resolve(token(env.stranger),
                                {'report_id': report.id, 'resolved': True})
        assert db.session.get(Report, report.id).status == REPORT_STATE_RESOLVED

    def test_an_account_with_no_standing_cannot_resolve(self, env, report):
        with pytest.raises(Exception, match='incorrect login'):
            put_post_report_resolve(token(env.stranger),
                                    {'report_id': report.id, 'resolved': True})


# ----------------------------------------------------------------- moderating

class TestModerating:
    def test_locking_and_unlocking(self, env):
        post_post_lock(token(env.moderator),
                       {'post_id': env.post.id, 'locked': True})
        assert db.session.get(Post, env.post.id).comments_enabled is False
        post_post_lock(token(env.moderator),
                       {'post_id': env.post.id, 'locked': False})
        assert db.session.get(Post, env.post.id).comments_enabled is True

    def test_hiding_and_unhiding(self, env):
        post_post_hide(token(env.stranger),
                       {'post_id': env.post.id, 'hidden': True})
        post_post_hide(token(env.stranger),
                       {'post_id': env.post.id, 'hidden': False})

    def test_removing_and_restoring(self, env):
        from app.models import ModLog
        post_post_remove(token(env.moderator),
                         {'post_id': env.post.id, 'removed': True})
        assert db.session.get(Post, env.post.id).deleted is True
        # The reason is what the modlog shows the community afterwards.
        assert ModLog.query.filter_by(action='delete_post').one().reason == \
            'Removed by mod'
        post_post_remove(token(env.moderator),
                         {'post_id': env.post.id, 'removed': False})
        assert db.session.get(Post, env.post.id).deleted is False
        assert ModLog.query.filter_by(action='restore_post').one().reason == \
            'Restored by mod'

    def test_a_removal_reason_can_be_given(self, env):
        from app.models import ModLog
        post_post_remove(token(env.moderator),
                         {'post_id': env.post.id, 'removed': True,
                          'reason': 'off topic'})
        assert ModLog.query.filter_by(action='delete_post').one().reason == \
            'off topic'

    def test_featuring_in_the_community(self, env):
        post_post_feature(token(env.moderator),
                          {'post_id': env.post.id, 'featured': True})
        assert db.session.get(Post, env.post.id).sticky is True

    def test_featuring_on_the_instance(self, env):
        post_post_feature(token(env.admin),
                          {'post_id': env.post.id, 'featured': True,
                           'feature_type': 'Local'})
        assert db.session.get(Post, env.post.id).instance_sticky is True

    def test_only_an_administrator_features_on_the_instance(self, env):
        with pytest.raises(Exception, match='Only admin users'):
            post_post_feature(token(env.moderator),
                              {'post_id': env.post.id, 'featured': True,
                               'feature_type': 'Local'})

    def test_a_feature_type_that_is_neither_is_refused_by_name(self, env):
        """D1221. Neither arm bound `post` or `user_id`, so the response built
        below them was `UnboundLocalError: cannot access local variable
        'post'`."""
        with pytest.raises(Exception, match='feature_type must be'):
            post_post_feature(token(env.admin),
                              {'post_id': env.post.id, 'featured': True,
                               'feature_type': 'Global'})


class TestMarkAsRead:
    def test_naming_no_post_at_all_is_refused(self, env):
        with pytest.raises(Exception, match='post_id or post_ids required'):
            post_post_mark_as_read(token(env.stranger), {'read': True})

    def test_one_post(self, env):
        assert post_post_mark_as_read(
            token(env.stranger),
            {'post_id': env.post.id, 'read': True}) == {'success': True}
        assert db.session.execute(
            db.select(read_posts).where(
                read_posts.c.read_post_id == env.post.id)).first() is not None

    def test_several_posts(self, env):
        second = make_post(env.community, env.author,
                           ap_id='https://test.piefed.local/p/2')
        assert post_post_mark_as_read(
            token(env.stranger),
            {'post_ids': [env.post.id, second.id],
             'read': True}) == {'success': True}

    def test_a_row_that_is_already_there_is_reported_as_a_failure(self, env):
        from sqlalchemy.exc import IntegrityError
        with patch('app.api.alpha.utils.post.mark_post_read',
                   side_effect=IntegrityError('x', 'y', Exception('z'))):
            assert post_post_mark_as_read(
                token(env.stranger),
                {'post_id': env.post.id, 'read': True}) == {'success': False}

    def test_a_post_that_is_already_read(self, env):
        post_post_mark_as_read(token(env.stranger),
                               {'post_id': env.post.id, 'read': True})
        assert post_post_mark_as_read(
            token(env.stranger),
            {'post_id': env.post.id, 'read': True}) == {'success': True}


class TestLikeListing:
    def test_an_account_with_no_standing_cannot_read_the_votes(self, env):
        with pytest.raises(Exception, match='Not a moderator'):
            get_post_like_list(token(env.stranger), {'post_id': env.post.id})

    def test_a_moderator_reads_the_votes(self, env):
        make_post_vote(env.stranger, env.post, 1)
        res = get_post_like_list(token(env.moderator),
                                 {'post_id': env.post.id})
        assert [like['score'] for like in res['post_likes']] == [1]
        assert res['post_likes'][0]['creator']['id'] == env.stranger.id
        assert res['post_likes'][0]['creator_banned'] is False
        assert res['post_likes'][0]['creator_banned_from_community'] is False

    def test_a_voter_banned_from_the_site_is_marked(self, env):
        make_post_vote(env.stranger, env.post, 1)
        env.stranger.banned = True
        db.session.commit()
        res = get_post_like_list(token(env.moderator),
                                 {'post_id': env.post.id})
        assert res['post_likes'][0]['creator_banned'] is True

    def test_a_voter_banned_from_the_community_is_marked(self, env):
        make_post_vote(env.stranger, env.post, 1)
        make_community_ban(env.stranger, env.community,
                           banned_by=env.moderator)
        res = get_post_like_list(token(env.moderator),
                                 {'post_id': env.post.id})
        assert res['post_likes'][0]['creator_banned_from_community'] is True

    def test_a_vote_that_was_taken_back_is_left_out(self, env):
        make_post_vote(env.stranger, env.post, 0)
        res = get_post_like_list(token(env.moderator),
                                 {'post_id': env.post.id})
        assert res['post_likes'] == []

    def test_the_listing_pages(self, env):
        for name in ('one', 'two', 'three'):
            make_post_vote(make_user(env.baseline.instance_local, name,
                                     local=True), env.post, 1)
        first = get_post_like_list(token(env.moderator),
                                   {'post_id': env.post.id, 'limit': 2})
        assert len(first['post_likes']) == 2
        assert first['next_page'] == '2'
        second = get_post_like_list(token(env.moderator),
                                    {'post_id': env.post.id, 'limit': 2,
                                     'page': 2})
        assert len(second['post_likes']) == 1
        assert second['next_page'] is None

    def test_a_limit_beyond_the_configured_page_length_is_clamped(
            self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'PAGE_LENGTH', 2)
        for name in ('one', 'two', 'three'):
            make_post_vote(make_user(env.baseline.instance_local, name,
                                     local=True), env.post, 1)
        res = get_post_like_list(token(env.moderator),
                                 {'post_id': env.post.id, 'limit': 500})
        assert len(res['post_likes']) == 2

    def test_an_administrator_reads_the_votes(self, env):
        make_post_vote(env.stranger, env.post, 1)
        res = get_post_like_list(token(env.admin), {'post_id': env.post.id})
        assert len(res['post_likes']) == 1


class TestFlair:
    def test_an_account_with_no_standing_cannot_set_flair(self, env):
        flair = make_community_flair(env.community, 'news')
        with pytest.raises(Exception, match='Insufficient permissions'):
            put_post_set_flair(token(env.stranger),
                               {'post_id': env.post.id,
                                'flair_id_list': [flair.id]})

    def test_the_author_sets_flair(self, env):
        flair = make_community_flair(env.community, 'news')
        put_post_set_flair(token(env.author),
                           {'post_id': env.post.id,
                            'flair_id_list': [flair.id]})
        assert [f.id for f in db.session.get(Post, env.post.id).flair] == \
            [flair.id]

    def test_a_moderator_sets_flair(self, env):
        flair = make_community_flair(env.community, 'news')
        put_post_set_flair(token(env.moderator),
                           {'post_id': env.post.id,
                            'flair_id_list': [flair.id]})
        assert len(db.session.get(Post, env.post.id).flair) == 1

    def test_naming_no_flair_clears_what_is_there(self, env):
        flair = make_community_flair(env.community, 'news')
        put_post_set_flair(token(env.author),
                           {'post_id': env.post.id,
                            'flair_id_list': [flair.id]})
        put_post_set_flair(token(env.author), {'post_id': env.post.id})
        assert db.session.get(Post, env.post.id).flair == []

    def test_flair_belonging_to_another_community_is_ignored(self, env):
        elsewhere = make_community('elsewhere')
        theirs = make_community_flair(elsewhere, 'theirs')
        put_post_set_flair(token(env.author),
                           {'post_id': env.post.id,
                            'flair_id_list': [theirs.id]})
        assert db.session.get(Post, env.post.id).flair == []

    def test_a_post_still_in_review_is_not_federated(self, env):
        from app.constants import POST_STATUS_REVIEWING
        flair = make_community_flair(env.community, 'news')
        env.post.status = POST_STATUS_REVIEWING
        db.session.commit()
        with patch('app.api.alpha.utils.post.task_selector') as task:
            put_post_set_flair(token(env.author),
                               {'post_id': env.post.id,
                                'flair_id_list': [flair.id]})
        task.assert_not_called()
        assert len(db.session.get(Post, env.post.id).flair) == 1

    def test_a_published_post_is_federated(self, env):
        flair = make_community_flair(env.community, 'news')
        with patch('app.api.alpha.utils.post.task_selector') as task:
            put_post_set_flair(token(env.author),
                               {'post_id': env.post.id,
                                'flair_id_list': [flair.id]})
        task.assert_called_once_with('edit_post', post_id=env.post.id)

    def test_a_flair_nobody_holds_is_ignored(self, env):
        put_post_set_flair(token(env.author),
                           {'post_id': env.post.id, 'flair_id_list': [MISSING]})
        assert db.session.get(Post, env.post.id).flair == []


class TestPollVote:
    def test_a_vote_is_counted(self, env):
        from app.models import PollChoice, PollChoiceVote
        post = make_post(env.community, env.author,
                         ap_id='https://test.piefed.local/p/poll')
        post.type = POST_TYPE_POLL
        make_poll(post)
        choice = make_poll_choice(post, 'a')
        db.session.commit()
        post_poll_vote(token(env.stranger),
                       {'post_id': post.id, 'choice_id': choice.id})
        assert PollChoiceVote.query.filter_by(
            user_id=env.stranger.id, post_id=post.id).count() == 1
