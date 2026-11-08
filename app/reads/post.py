"""One post as a viewer may read it, and the gate before a community's list of posts.

The refusals are plain `Exception`s with the alpha API's texts: its error handler answers a bare `Exception` with a 400
carrying `str(e)` and anything else (a subclass too) with a 500, and clients match on those texts. A later API maps
each text to its own status.
"""
from app import db
from app.activitypub.util import normalise_actor_string
from app.local_only import hidden_here
from app.models import Community, Post
from app.reads.viewer import Restrictions
from app.storefront import current_storefront, domain_authority
from app.visibility import can_view


def refuse_unreadable_post(post: Post, user_id: int | None, restrictions: Restrictions) -> None:
    """Refuse the post `post` to the viewer `user_id` (None: nobody signed in) when they may not read it on its own.

    The caller found the post first (`app.local_only.found_here`: an id nobody holds and a post hidden on this
    storefront are one answer, `post not found`). A post the viewer may not see (followers-only, unpublished, local-only
    to an anonymous reader: `can_view`) is `post not found` too, so the answer does not confirm it exists (interop D7);
    a post of a private community the viewer does not belong to is refused by name, signed in or not. A deleted or
    removed post is not refused (its view blanks it), and neither is one by a person or instance the viewer blocked."""
    if not can_view(post, user_id):
        raise Exception('post not found')
    if post.community.private and post.community_id not in restrictions.private_community_ids:
        raise Exception('Private community - membership required')


def refuse_private_community(community_id, community_name, user_id, user):
    """Refuse a post list of the private community `community_id` or `community_name` names to a caller who is not a
    member. A community nobody holds, and one hidden here (`hidden_here`), list nothing and are not refused, so the
    answer tells neither from the other (follow-up F6, m2)."""
    if community_id:
        community = db.session.get(Community, community_id)
    else:
        # parse community_name to get the community
        if '@' not in community_name:
            community_name_lookup = f"{community_name}@{domain_authority(current_storefront())}"   # the storefront's
        else:
            community_name_lookup = community_name
        name, ap_domain = normalise_actor_string(community_name_lookup)
        if not name or not ap_domain:
            raise Exception('invalid_request')
        community = Community.query.filter_by(name=name, ap_domain=ap_domain).first()

    if community is None or not community.private or hidden_here(community, user_id is not None):
        return
    # community is private, check if user is a member
    if not user_id:
        raise Exception('Private community - authentication required')
    if not community.is_member(user):
        raise Exception('Private community - membership required')
