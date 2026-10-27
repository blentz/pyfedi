"""The alpha API's lookups of an id a caller sent: one rule for every endpoint (follow-up F6, ruling 2026-10-26).

A row hidden on this storefront (app.local_only.found_here) is refused with the exception and text a row nobody holds
gets, so an action's answer never tells the two apart, nor carries the hidden row. Every endpoint that reads an id
from its body or query string asks one of these, and none keeps its own copy of the rule."""
from app.local_only import found_here
from app.models import Community, Feed, Post, PostReply, Report, User


def a_row(model, id_, missing: str):
    """The `model` row `id_` names, or `Exception(missing)`, the endpoint's own text for an id nobody holds."""
    row = found_here(model, id_)
    if row is None:
        raise Exception(missing)
    return row


def a_post(post_id) -> Post:
    """The post `post_id` names, or a clean refusal: the alpha API turns every exception into a 400 carrying `str(e)`,
    so reading an attribute off None handed the caller `'NoneType' object has no attribute ...` (D1194, D1202)."""
    return a_row(Post, post_id, 'post not found')


def a_reply(reply_id) -> PostReply:
    return a_row(PostReply, reply_id, 'comment not found')


def a_community(community_id) -> Community:
    return a_row(Community, community_id, 'community not found')


def a_user(user_id) -> User:
    return a_row(User, user_id, 'user not found')


def a_person(person_id) -> User:
    """a_user for the endpoints whose text for an id nobody holds has always been `person not found`."""
    return a_row(User, person_id, 'person not found')


def a_feed(feed_id, missing: str) -> Feed:
    """The feed `feed_id` names, or `Exception(missing)`: the feed endpoints each have their own text for an id nobody
    holds. A feed hidden here (an onion feed off the onion, per-domain feeds Task 7) gets the same."""
    return a_row(Feed, feed_id, missing)


def a_report(report_id) -> Report:
    return a_row(Report, report_id, 'report not found')
