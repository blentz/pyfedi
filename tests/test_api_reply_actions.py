"""The comment API's actions: voting, saving, subscribing, writing, editing,
deleting, reporting, moderating and reading who voted.

Sub-project 84, slice H -- `app/api/alpha/utils/reply.py` without the list.
Two defects, both measured:

* TEN endpoints read attributes straight off a `db.session.get(...)`, which
  answers None for an id nobody holds, so naming a comment, post or report
  that does not exist was an AttributeError rather than an answer (D1194);
* `post_reply` tested `language_id < 2` on a value that can be None -- its
  own default, `site_language_id()`, answers None on an instance whose
  languages are not seeded -- while `put_reply` one function below already
  writes `is None or ... < 2` (D1195).
"""
from unittest.mock import patch

import pytest
from flask import g

from app.constants import REPORT_STATE_NEW, REPORT_STATE_RESOLVED
from app import db
from app.models import (Community, CommunityMember, Language, Notification,
                        Post, PostReply, PostReplyVote, Report, Role,
                        RolePermission, Site, User, user_role)
from tests.factories import (make_community_member, make_post_reply,
                             make_post_reply_vote, make_user)


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
    """`author` wrote the comment, `moderator` moderates the community it is
    in, and `stranger` is neither."""
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    author = api_baseline.user2
    stranger = api_baseline.user3
    community = api_baseline.community1
    post = api_baseline.post1
    language = Language(code='en', name='English')
    db.session.add(language)
    db.session.commit()
    db.session.get(Site, 1).language_id = language.id
    reply = make_post_reply(post, author, body='a reply')
    reply.body_html = '<p>a reply</p>'
    # `can_create_post_reply` refuses a local account with no keys
    # (app/utils.py:2574), and both actors write comments here.
    for actor in (author, stranger):
        actor.private_key = 'a-key'
        actor.public_key = 'a-key'
        actor.verified = True
    db.session.commit()
    return author, stranger, community, post, reply, api_baseline


def a_moderator(baseline, community):
    moderator = make_user(baseline.instance_local, 'themod', local=True)
    db.session.commit()
    make_community_member(moderator, community, is_moderator=True)
    db.session.commit()
    return moderator


# --------------------------------------------------------------------------
# D1194 -- ten ids that did not resolve
# --------------------------------------------------------------------------


@pytest.mark.parametrize('name,data,message', [
    ('get_reply', {'id': 999999}, 'comment not found'),
    ('put_reply', {'comment_id': 999999, 'body': 'x'}, 'comment not found'),
    ('post_reply', {'post_id': 999999, 'body': 'x'}, 'post not found'),
    ('post_reply_report', {'comment_id': 999999, 'reason': 'spam'},
     'comment not found'),
    ('post_reply_mark_as_read', {'comment_reply_id': 999999, 'read': True},
     'comment not found'),
    ('post_reply_mark_as_answer', {'comment_reply_id': 999999, 'answer': True},
     'comment not found'),
    ('post_reply_distinguish', {'comment_reply_id': 999999,
                                'distinguished': True}, 'comment not found'),
    ('get_reply_like_list', {'comment_id': 999999}, 'comment not found'),
    ('put_reply_report_resolve', {'report_id': 999999, 'resolved': True},
     'invalid target of resolution'),
    ('get_reply_report_list', {'comment_id': 999999}, 'comment not found'),
])
def test_an_id_that_does_not_resolve_is_refused(app, env, name, data, message):
    """D1194. Each of these read an attribute off a `db.session.get` that
    answers None. Measured, one per endpoint, e.g. `PROBE bm get_reply:
    AttributeError: 'NoneType' object has no attribute 'community_id'`."""
    import app.api.alpha.utils.reply as reply_utils

    author, stranger, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        getattr(reply_utils, name)(token(author), data)

    assert str(refused.value) == message


def test_a_report_about_a_community_that_is_gone(app, env):
    """D1194's other half in the resolve endpoint: the report resolves, its
    community does not."""
    from app.api.alpha.utils.reply import put_reply_report_resolve

    author, stranger, community, post, reply, baseline = env
    # `report_in_community_id_fkey` forbids a dangling id, so the reachable
    # shape is a report that names no community at all.
    report = Report(reasons='spam', type=0, reporter_id=stranger.id,
                    suspect_post_reply_id=reply.id, in_community_id=None,
                    source_instance_id=1)
    db.session.add(report)
    db.session.commit()

    with pytest.raises(Exception) as refused:
        put_reply_report_resolve(token(author), {'report_id': report.id,
                                                 'resolved': True})

    assert str(refused.value) == 'invalid target of resolution'


def test_a_report_list_for_a_community_that_is_gone(app, env):
    from app.api.alpha.utils.reply import get_reply_report_list

    author, stranger, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        get_reply_report_list(token(author), {'community_id': 999999})

    assert str(refused.value) == 'community not found'


# --------------------------------------------------------------------------
# D1195 -- a language that is not set
# --------------------------------------------------------------------------


def test_a_comment_is_written_on_an_instance_with_no_languages(app, env):
    """D1195. `site_language_id()` answers None when the Site carries no
    language and there is no 'en' row -- a fresh instance -- and
    `language_id < 2` on None is a TypeError. Measured: `PROBE bm post_reply:
    TypeError: '<' not supported between instances of 'NoneType' and
    'int'`."""
    from app.api.alpha.utils.reply import post_reply

    author, stranger, community, post, reply, baseline = env
    Language.query.delete()
    db.session.get(Site, 1).language_id = None
    db.session.commit()

    answer = post_reply(token(author), {'post_id': post.id,
                                        'body': 'hello there'})

    assert answer['comment_view']['comment']['body'] == 'hello there'


def test_a_comment_takes_the_language_it_was_given(app, env):
    from app.api.alpha.utils.reply import post_reply

    author, stranger, community, post, reply, baseline = env
    french = Language(code='fr', name='French')
    db.session.add(french)
    db.session.commit()

    answer = post_reply(token(author), {'post_id': post.id, 'body': 'bonjour',
                                        'language_id': french.id})

    assert db.session.get(PostReply,
                          answer['comment_view']['comment']['id']).language_id == french.id


def test_a_language_below_two_falls_back_to_the_instances(app, env):
    """1 is 'undetermined' -- a language nobody chose."""
    from app.api.alpha.utils.reply import post_reply

    author, stranger, community, post, reply, baseline = env
    site_language = db.session.get(Site, 1).language_id

    answer = post_reply(token(author), {'post_id': post.id, 'body': 'hello',
                                        'language_id': 1})

    assert db.session.get(PostReply,
                          answer['comment_view']['comment']['id']).language_id == site_language


# --------------------------------------------------------------------------
# Writing, editing, deleting
# --------------------------------------------------------------------------


def test_a_comment_is_written(app, env):
    from app.api.alpha.utils.reply import post_reply

    author, stranger, community, post, reply, baseline = env

    answer = post_reply(token(stranger), {'post_id': post.id,
                                          'body': 'hello there'})

    assert answer['comment_view']['comment']['body'] == 'hello there'
    assert answer['comment_view']['creator']['id'] == stranger.id


def test_a_comment_can_answer_another(app, env):
    from app.api.alpha.utils.reply import post_reply

    author, stranger, community, post, reply, baseline = env

    answer = post_reply(token(stranger), {'post_id': post.id,
                                          'parent_id': reply.id,
                                          'body': 'I disagree'})

    assert db.session.get(PostReply,
                          answer['comment_view']['comment']['id']).parent_id == reply.id


def test_a_comment_is_edited_by_its_author(app, env):
    from app.api.alpha.utils.reply import put_reply

    author, stranger, community, post, reply, baseline = env

    answer = put_reply(token(author), {'comment_id': reply.id,
                                       'body': 'second thoughts'})

    assert answer['comment_view']['comment']['body'] == 'second thoughts'


def test_a_comment_is_not_edited_by_anybody_else(app, env):
    from app.api.alpha.utils.reply import put_reply

    author, stranger, community, post, reply, baseline = env

    with pytest.raises(Exception):
        put_reply(token(stranger), {'comment_id': reply.id, 'body': 'mine now'})

    assert reply.body == 'a reply'


def test_an_edit_keeps_what_it_was_not_given(app, env):
    from app.api.alpha.utils.reply import put_reply

    author, stranger, community, post, reply, baseline = env
    reply.language_id = db.session.get(Site, 1).language_id
    db.session.commit()

    put_reply(token(author), {'comment_id': reply.id, 'body': 'changed'})

    assert reply.language_id == db.session.get(Site, 1).language_id


def test_a_comment_is_deleted_and_restored_by_its_author(app, env):
    from app.api.alpha.utils.reply import post_reply_delete

    author, stranger, community, post, reply, baseline = env

    deleted = post_reply_delete(token(author), {'comment_id': reply.id,
                                                'deleted': True})
    assert deleted['comment_view']['comment']['deleted'] is True

    restored = post_reply_delete(token(author), {'comment_id': reply.id,
                                                 'deleted': False})
    assert restored['comment_view']['comment']['deleted'] is False


def test_a_comment_is_not_deleted_by_anybody_else(app, env):
    from app.api.alpha.utils.reply import post_reply_delete

    author, stranger, community, post, reply, baseline = env

    with pytest.raises(Exception):
        post_reply_delete(token(stranger), {'comment_id': reply.id,
                                            'deleted': True})

    assert reply.deleted is False


# --------------------------------------------------------------------------
# Voting, saving, subscribing
# --------------------------------------------------------------------------


@pytest.mark.parametrize('score,effect', [(1, 1), (-1, -1)])
def test_a_vote_is_recorded(app, env, score, effect):
    from app.api.alpha.utils.reply import post_reply_like

    author, stranger, community, post, reply, baseline = env

    post_reply_like(token(stranger), {'comment_id': reply.id, 'score': score})

    vote = PostReplyVote.query.filter_by(user_id=stranger.id,
                                         post_reply_id=reply.id).first()
    assert (vote.effect if vote else 0) == effect


def test_a_vote_is_taken_back(app, env):
    """Score 0 is the API's spelling of "remove my vote"."""
    from app.api.alpha.utils.reply import post_reply_like

    author, stranger, community, post, reply, baseline = env
    post_reply_like(token(stranger), {'comment_id': reply.id, 'score': 1})

    post_reply_like(token(stranger), {'comment_id': reply.id, 'score': 0})

    vote = PostReplyVote.query.filter_by(user_id=stranger.id,
                                         post_reply_id=reply.id).first()
    assert vote is None


def test_taking_back_a_vote_nobody_cast_is_not_an_error(app, env):
    """D1196. `PostReply.vote` remapped 'reversal' only when an existing vote
    was found, so a reversal with nothing to reverse fell through to
    `ValueError: unresolvable vote direction: 'reversal'` -- while `Post.vote`
    answers None for the same request. The same call was a 500 for a comment
    and a no-op for a post. Measured:

        PROBE bn1 comment: ValueError: unresolvable vote direction: 'reversal'
        PROBE bn2 post: accepted
    """
    from app.api.alpha.utils.reply import post_reply_like

    author, stranger, community, post, reply, baseline = env

    post_reply_like(token(stranger), {'comment_id': reply.id, 'score': 0})

    assert PostReplyVote.query.count() == 0


def test_a_private_vote_is_not_federated(app, env):
    """`private` defaults to the account's own setting, and the federation
    flag is its opposite."""
    from app.api.alpha.utils.reply import post_reply_like

    author, stranger, community, post, reply, baseline = env
    stranger.vote_privately = True
    db.session.commit()

    with patch('app.api.alpha.utils.reply.vote_for_reply',
               return_value=stranger.id) as voted:
        post_reply_like(token(stranger), {'comment_id': reply.id, 'score': 1})

    assert voted.call_args.args[2] is False


def test_a_vote_can_be_asked_to_federate(app, env):
    from app.api.alpha.utils.reply import post_reply_like

    author, stranger, community, post, reply, baseline = env
    stranger.vote_privately = True
    db.session.commit()

    with patch('app.api.alpha.utils.reply.vote_for_reply',
               return_value=stranger.id) as voted:
        post_reply_like(token(stranger), {'comment_id': reply.id, 'score': 1,
                                          'private': False})

    assert voted.call_args.args[2] is True


def test_a_comment_is_saved_and_unsaved(app, env):
    from app.api.alpha.utils.reply import put_reply_save

    author, stranger, community, post, reply, baseline = env

    saved = put_reply_save(token(stranger), {'comment_id': reply.id,
                                             'save': True})
    assert saved['comment_view']['saved'] is True

    unsaved = put_reply_save(token(stranger), {'comment_id': reply.id,
                                               'save': False})
    assert unsaved['comment_view']['saved'] is False


def test_a_comment_is_subscribed_to_and_away_from(app, env):
    from app.api.alpha.utils.reply import put_reply_subscribe

    author, stranger, community, post, reply, baseline = env

    subscribed = put_reply_subscribe(token(stranger), {'comment_id': reply.id,
                                                       'subscribe': True})
    assert subscribed['comment_view']['activity_alert'] is True

    unsubscribed = put_reply_subscribe(token(stranger),
                                       {'comment_id': reply.id,
                                        'subscribe': False})
    assert unsubscribed['comment_view']['activity_alert'] is False


def test_one_comment_is_read_by_its_id(app, env):
    from app.api.alpha.utils.reply import get_reply

    author, stranger, community, post, reply, baseline = env

    answer = get_reply(token(stranger), {'id': reply.id})

    assert answer['comment_view']['comment']['id'] == reply.id


def test_one_comment_can_be_read_without_logging_in(app, env):
    from app.api.alpha.utils.reply import get_reply

    author, stranger, community, post, reply, baseline = env

    assert get_reply(None, {'id': reply.id})['comment_view']['comment']['id'] == reply.id


# --------------------------------------------------------------------------
# Reporting and resolving
# --------------------------------------------------------------------------


def test_a_comment_is_reported(app, env):
    from app.api.alpha.utils.reply import post_reply_report

    author, stranger, community, post, reply, baseline = env

    answer = post_reply_report(token(stranger), {'comment_id': reply.id,
                                                 'reason': 'spam'})

    assert answer['comment_report_view']['comment_report']['reason'] == 'spam'
    assert Report.query.one().suspect_post_reply_id == reply.id


def test_the_report_list_needs_a_moderator(app, env):
    from app.api.alpha.utils.reply import get_reply_report_list

    author, stranger, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        get_reply_report_list(token(stranger), {'comment_id': reply.id})

    assert str(refused.value) == 'incorrect login'


def a_report(reporter, reply, community):
    """`reply_report_view` renders the reported account as well, so a report
    built by hand has to name it -- `report_reply` does."""
    report = Report(reasons='spam', type=0, reporter_id=reporter.id,
                    suspect_post_reply_id=reply.id,
                    suspect_user_id=reply.user_id,
                    in_community_id=community.id, source_instance_id=1)
    db.session.add(report)
    db.session.commit()
    return report


def test_a_moderator_reads_the_reports_for_their_comment(app, env):
    from app.api.alpha.utils.reply import get_reply_report_list

    author, stranger, community, post, reply, baseline = env
    moderator = a_moderator(baseline, community)
    report = a_report(stranger, reply, community)

    answer = get_reply_report_list(token(moderator), {'comment_id': reply.id})

    assert [r['comment_report']['id']
            for r in answer['comment_reports']] == [report.id]


def test_a_moderator_reads_the_reports_for_their_community(app, env):
    from app.api.alpha.utils.reply import get_reply_report_list

    author, stranger, community, post, reply, baseline = env
    moderator = a_moderator(baseline, community)
    report = a_report(stranger, reply, community)

    answer = get_reply_report_list(token(moderator),
                                   {'community_id': community.id})

    assert len(answer['comment_reports']) == 1


def test_somebody_who_moderates_nothing_reads_no_reports(app, env):
    from app.api.alpha.utils.reply import get_reply_report_list

    author, stranger, community, post, reply, baseline = env
    a_report(stranger, reply, community)

    answer = get_reply_report_list(token(stranger), {})

    assert answer['comment_reports'] == []


def test_a_moderator_reads_the_reports_of_what_they_moderate(app, env):
    from app.api.alpha.utils.reply import get_reply_report_list

    author, stranger, community, post, reply, baseline = env
    moderator = a_moderator(baseline, community)
    report = a_report(stranger, reply, community)

    answer = get_reply_report_list(token(moderator), {})

    assert [r['comment_report']['id']
            for r in answer['comment_reports']] == [report.id]


def test_somebody_who_administers_communities_reads_them_all(app, env):
    from app.api.alpha.utils.reply import get_reply_report_list

    author, stranger, community, post, reply, baseline = env
    admin = with_permission(make_user(baseline.instance_local, 'theadmin',
                                      local=True),
                            'administer all communities')
    report = a_report(stranger, reply, community)

    for data in ({}, {'comment_id': reply.id}, {'community_id': community.id}):
        answer = get_reply_report_list(token(admin), data)
        assert len(answer['comment_reports']) == 1


def test_resolved_reports_are_left_out_by_default(app, env):
    from app.api.alpha.utils.reply import get_reply_report_list

    author, stranger, community, post, reply, baseline = env
    moderator = a_moderator(baseline, community)
    report = a_report(stranger, reply, community)
    report.status = REPORT_STATE_RESOLVED
    db.session.commit()

    assert get_reply_report_list(token(moderator), {})['comment_reports'] == []
    assert len(get_reply_report_list(
        token(moderator), {'unresolved_only': False})['comment_reports']) == 1


def test_the_report_list_is_paged(app, env):
    from app.api.alpha.utils.reply import get_reply_report_list

    author, stranger, community, post, reply, baseline = env
    moderator = a_moderator(baseline, community)
    for _ in range(3):
        a_report(stranger, reply, community)

    answer = get_reply_report_list(token(moderator), {'limit': 2})

    assert len(answer['comment_reports']) == 2
    assert answer['next_page'] == '2'


def test_a_report_is_resolved_and_reopened_by_a_moderator(app, env):
    from app.api.alpha.utils.reply import put_reply_report_resolve

    author, stranger, community, post, reply, baseline = env
    moderator = a_moderator(baseline, community)
    report = a_report(stranger, reply, community)

    put_reply_report_resolve(token(moderator), {'report_id': report.id,
                                                'resolved': True})
    assert report.status == REPORT_STATE_RESOLVED

    put_reply_report_resolve(token(moderator), {'report_id': report.id,
                                                'resolved': False})
    assert report.status == REPORT_STATE_NEW


def test_a_report_is_not_resolved_by_a_stranger(app, env):
    from app.api.alpha.utils.reply import put_reply_report_resolve

    author, stranger, community, post, reply, baseline = env
    report = a_report(stranger, reply, community)

    with pytest.raises(Exception) as refused:
        put_reply_report_resolve(token(stranger), {'report_id': report.id,
                                                   'resolved': True})

    assert str(refused.value) == 'incorrect login'
    assert report.status != REPORT_STATE_RESOLVED


def test_a_report_about_a_post_is_not_a_comment_report(app, env):
    from app.api.alpha.utils.reply import put_reply_report_resolve

    author, stranger, community, post, reply, baseline = env
    moderator = a_moderator(baseline, community)
    report = Report(reasons='spam', type=0, reporter_id=stranger.id,
                    suspect_post_id=post.id, in_community_id=community.id,
                    source_instance_id=1)
    db.session.add(report)
    db.session.commit()

    with pytest.raises(Exception) as refused:
        put_reply_report_resolve(token(moderator), {'report_id': report.id,
                                                    'resolved': True})

    assert str(refused.value) == 'invalid target of resolution'


# --------------------------------------------------------------------------
# Moderating a comment
# --------------------------------------------------------------------------


def test_a_moderator_removes_and_restores_a_comment(app, env):
    from app.api.alpha.utils.reply import post_reply_remove

    author, stranger, community, post, reply, baseline = env
    moderator = a_moderator(baseline, community)

    removed = post_reply_remove(token(moderator), {'comment_id': reply.id,
                                                   'removed': True})
    assert removed['comment_view']['comment']['removed'] is True

    restored = post_reply_remove(token(moderator), {'comment_id': reply.id,
                                                    'removed': False,
                                                    'reason': 'on reflection'})
    assert restored['comment_view']['comment']['removed'] is False


def test_a_stranger_removes_nothing(app, env):
    from app.api.alpha.utils.reply import post_reply_remove

    author, stranger, community, post, reply, baseline = env

    with pytest.raises(Exception):
        post_reply_remove(token(stranger), {'comment_id': reply.id,
                                            'removed': True})


def test_a_comment_is_locked_and_unlocked(app, env):
    from app.api.alpha.utils.reply import post_reply_lock

    author, stranger, community, post, reply, baseline = env
    moderator = a_moderator(baseline, community)

    post_reply_lock(token(moderator), {'comment_id': reply.id,
                                       'locked': True})
    assert db.session.get(PostReply, reply.id).replies_enabled is False

    post_reply_lock(token(moderator), {'comment_id': reply.id,
                                       'locked': False})
    assert db.session.get(PostReply, reply.id).replies_enabled is True


def test_only_the_author_distinguishes_their_own_comment(app, env):
    """And only if they moderate the community it is in."""
    from app.api.alpha.utils.reply import post_reply_distinguish

    author, stranger, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        post_reply_distinguish(token(stranger),
                               {'comment_reply_id': reply.id,
                                'distinguished': True})

    assert str(refused.value) == 'incorrect login'
    assert reply.distinguished is False


def test_an_author_who_moderates_nothing_cannot_distinguish(app, env):
    from app.api.alpha.utils.reply import post_reply_distinguish

    author, stranger, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        post_reply_distinguish(token(author), {'comment_reply_id': reply.id,
                                               'distinguished': True})

    assert str(refused.value) == 'insufficient permission'


def test_a_moderator_distinguishes_their_own_comment(app, env):
    from app.api.alpha.utils.reply import post_reply_distinguish

    author, stranger, community, post, reply, baseline = env
    membership = CommunityMember.query.filter_by(user_id=author.id,
                                                 community_id=community.id).first()
    if membership:
        membership.is_moderator = True
    else:
        make_community_member(author, community, is_moderator=True)
    db.session.commit()

    answer = post_reply_distinguish(token(author),
                                    {'comment_reply_id': reply.id,
                                     'distinguished': True})

    assert answer['comment_view']['comment']['distinguished'] is True
    assert reply.distinguished is True


def test_somebody_who_administers_communities_may_distinguish_their_own(app,
                                                                        env):
    from app.api.alpha.utils.reply import post_reply_distinguish

    author, stranger, community, post, reply, baseline = env
    with_permission(author, 'administer all communities')

    post_reply_distinguish(token(author), {'comment_reply_id': reply.id,
                                           'distinguished': True})

    assert reply.distinguished is True


# --------------------------------------------------------------------------
# Answers and read markers
# --------------------------------------------------------------------------


def test_the_author_of_a_comment_marks_it_as_the_answer(app, env):
    from app.api.alpha.utils.reply import post_reply_mark_as_answer

    author, stranger, community, post, reply, baseline = env

    answer = post_reply_mark_as_answer(token(author),
                                       {'comment_reply_id': reply.id,
                                        'answer': True})

    assert 'comment_reply_view' in answer
    assert db.session.get(PostReply, reply.id).answer is True


def test_the_answer_can_be_unchosen(app, env):
    from app.api.alpha.utils.reply import post_reply_mark_as_answer

    author, stranger, community, post, reply, baseline = env
    post_reply_mark_as_answer(token(author), {'comment_reply_id': reply.id,
                                              'answer': True})

    post_reply_mark_as_answer(token(author), {'comment_reply_id': reply.id,
                                              'answer': False})

    assert db.session.get(PostReply, reply.id).answer is False


def test_a_stranger_does_not_choose_the_answer(app, env):
    from app.api.alpha.utils.reply import post_reply_mark_as_answer

    author, stranger, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        post_reply_mark_as_answer(token(stranger),
                                  {'comment_reply_id': reply.id,
                                   'answer': True})

    assert str(refused.value) == 'Does not have permission'


def test_a_moderator_chooses_the_answer(app, env):
    from app.api.alpha.utils.reply import post_reply_mark_as_answer

    author, stranger, community, post, reply, baseline = env
    moderator = a_moderator(baseline, community)

    post_reply_mark_as_answer(token(moderator), {'comment_reply_id': reply.id,
                                                 'answer': True})

    assert db.session.get(PostReply, reply.id).answer is True


def test_marking_a_comment_read_clears_its_notification(app, env):
    from app.api.alpha.utils.reply import post_reply_mark_as_read

    author, stranger, community, post, reply, baseline = env
    notification = Notification(title='a reply',
                                url=f'/post/{post.id}#comment_{reply.id}',
                                user_id=stranger.id, author_id=author.id,
                                notif_type=0, subtype='x', read=False)
    db.session.add(notification)
    stranger.unread_notifications = 1
    db.session.commit()

    post_reply_mark_as_read(token(stranger), {'comment_reply_id': reply.id,
                                              'read': True})

    assert notification.read is True
    assert db.session.get(User, stranger.id).unread_notifications == 0


def test_marking_a_comment_unread_brings_it_back(app, env):
    from app.api.alpha.utils.reply import post_reply_mark_as_read

    author, stranger, community, post, reply, baseline = env
    notification = Notification(title='a reply',
                                url=f'/comment/{reply.id}',
                                user_id=stranger.id, author_id=author.id,
                                notif_type=0, subtype='x', read=True)
    db.session.add(notification)
    db.session.commit()

    post_reply_mark_as_read(token(stranger), {'comment_reply_id': reply.id,
                                              'read': False})

    assert notification.read is False


def test_marking_a_comment_with_no_notification_is_not_an_error(app, env):
    from app.api.alpha.utils.reply import post_reply_mark_as_read

    author, stranger, community, post, reply, baseline = env

    answer = post_reply_mark_as_read(token(stranger),
                                     {'comment_reply_id': reply.id,
                                      'read': True})

    assert answer['comment_reply_view']['comment']['id'] == reply.id


# --------------------------------------------------------------------------
# Who voted
# --------------------------------------------------------------------------


def test_a_moderator_sees_who_voted(app, env):
    from app.api.alpha.utils.reply import get_reply_like_list

    author, stranger, community, post, reply, baseline = env
    moderator = a_moderator(baseline, community)
    make_post_reply_vote(stranger, reply, 1)
    db.session.commit()

    answer = get_reply_like_list(token(moderator), {'comment_id': reply.id})

    assert [like['creator']['id']
            for like in answer['comment_likes']] == [stranger.id]
    assert answer['comment_likes'][0]['score'] == 1


def test_a_stranger_does_not_see_who_voted(app, env):
    from app.api.alpha.utils.reply import get_reply_like_list

    author, stranger, community, post, reply, baseline = env
    make_post_reply_vote(stranger, reply, 1)
    db.session.commit()

    with pytest.raises(Exception) as refused:
        get_reply_like_list(token(stranger), {'comment_id': reply.id})

    assert str(refused.value) == 'Not a moderator'


def test_a_banned_voter_is_named_as_banned(app, env):
    from app.api.alpha.utils.reply import get_reply_like_list

    author, stranger, community, post, reply, baseline = env
    moderator = a_moderator(baseline, community)
    make_post_reply_vote(stranger, reply, 1)
    stranger.banned = True
    db.session.commit()

    answer = get_reply_like_list(token(moderator), {'comment_id': reply.id})

    assert answer['comment_likes'][0]['creator_banned'] is True


def test_a_voter_banned_from_the_community_is_named_too(app, env):
    from app.api.alpha.utils.reply import get_reply_like_list
    from tests.factories import make_community_ban

    author, stranger, community, post, reply, baseline = env
    moderator = a_moderator(baseline, community)
    make_post_reply_vote(stranger, reply, 1)
    make_community_ban(stranger, community)
    db.session.commit()

    answer = get_reply_like_list(token(moderator), {'comment_id': reply.id})

    assert answer['comment_likes'][0]['creator_banned_from_community'] is True


def test_the_vote_list_is_paged_and_capped(app, env):
    from app.api.alpha.utils.reply import get_reply_like_list

    author, stranger, community, post, reply, baseline = env
    moderator = a_moderator(baseline, community)
    for number in range(3):
        somebody = make_user(baseline.instance_local, f'voter{number}',
                             local=True)
        make_post_reply_vote(somebody, reply, 1)
    db.session.commit()

    with pytest.MonkeyPatch.context() as patched:
        patched.setitem(app.config, 'PAGE_LENGTH', 2)
        answer = get_reply_like_list(token(moderator), {'comment_id': reply.id,
                                                        'limit': 100})

    assert len(answer['comment_likes']) == 2
    assert answer['next_page'] == '2'


def test_an_edit_of_a_comment_whose_distinguished_flag_is_null(app, env):
    """The column is nullable, and an edit that does not mention it must not
    hand None to the writer below."""
    from app.api.alpha.utils.reply import put_reply

    author, stranger, community, post, reply, baseline = env
    reply.distinguished = None
    db.session.commit()

    # What `edit_reply` is handed: the view renders a null flag as False
    # either way, and the column is only written for a moderator, so neither
    # can tell the coercion from its absence.
    with patch('app.api.alpha.utils.reply.edit_reply',
               return_value=(author.id, reply)) as edited:
        put_reply(token(author), {'comment_id': reply.id, 'body': 'changed'})

    assert edited.call_args.args[0]['distinguished'] is False


@pytest.mark.parametrize('effect,expected', [(1, 1), (-1, -1)])
def test_the_answer_view_carries_how_the_caller_voted(app, env, effect,
                                                      expected):
    from app.api.alpha.utils.reply import post_reply_mark_as_answer

    author, stranger, community, post, reply, baseline = env
    make_post_reply_vote(author, reply, effect)
    db.session.commit()

    answer = post_reply_mark_as_answer(token(author),
                                       {'comment_reply_id': reply.id,
                                        'answer': True})

    assert answer['comment_reply_view']['my_vote'] == expected


@pytest.mark.parametrize('effect,expected', [(1, 1), (-1, -1)])
def test_the_read_marker_view_carries_it_too(app, env, effect, expected):
    from app.api.alpha.utils.reply import post_reply_mark_as_read

    author, stranger, community, post, reply, baseline = env
    make_post_reply_vote(stranger, reply, effect)
    db.session.commit()

    answer = post_reply_mark_as_read(token(stranger),
                                     {'comment_reply_id': reply.id,
                                      'read': True})

    assert answer['comment_reply_view']['my_vote'] == expected


def test_a_stranger_reads_no_reports_for_a_community_they_do_not_moderate(app,
                                                                          env):
    """The community arm's refusal, which is separate from the comment
    arm's."""
    from app.api.alpha.utils.reply import get_reply_report_list

    author, stranger, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        get_reply_report_list(token(stranger), {'community_id': community.id})

    assert str(refused.value) == 'incorrect login'
