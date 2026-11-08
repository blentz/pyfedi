"""People: one person as a viewer may read them, and the query behind a list of people.

The alpha API's `get_user` (one person, by id or by name) and `get_user_list` (the person list and search) read
through here (W0.1 step 4), so a later client API finds people by the same rules. Model rows and queries only: views
and paging stay with the caller. A person the viewer may not read is `NoResultFound`, for an id or name nobody holds
and for a person hidden here alike (follow-up F6): the alpha API's error handler answers it `Not found`.

The two lookups disagreed before they were moved here: a lookup by name refuses a deleted person and one by id finds
them. Each is a `Lookup` (`BY_ID`, `BY_NAME`); the plan's W0.1 section lists the disagreements for the owner to rule
on. The person reads apply none of the viewer's blocks (`Restrictions`): a blocked person is found and listed.
"""
from dataclasses import dataclass

from sqlalchemy import and_, asc, desc, exists, or_, select
from sqlalchemy.orm.exc import NoResultFound

from app import db
from app.local_names import local_clause, local_named
from app.local_only import hidden_here, one_here, without_local_only_actors
from app.models import Community, CommunityMember, User
from app.permissions import user_access
from app.reads.viewer import Restrictions
from app.storefront import current_instance_id, current_storefront, domain_for_host, on_domain


@dataclass(frozen=True)
class Lookup:
    """Where the lookup of one person by id and by name disagreed."""
    finds_deleted: bool   # a deleted person is found (their view shows them deleted), else NoResultFound


BY_ID = Lookup(finds_deleted=True)
"""get_user's `person_id`: the row the id names, deleted or not."""

BY_NAME = Lookup(finds_deleted=False)
"""get_user's `username`: a deleted person is nobody."""


def refuse_hidden_person(person: User, signed_in: bool) -> None:
    """NoResultFound when this storefront hides `person` from a viewer who is signed in, or not (`hidden_here`: a
    local-only account from a logged-out viewer, owner ruling 2026-10-21, and any account off its own domain from
    everyone, follow-up F6)."""
    if hidden_here(person, signed_in):
        raise NoResultFound


def person_by_id(person_id: int, signed_in: bool, lookup: Lookup = BY_ID) -> User:
    """The person `person_id` names, as a viewer who is signed in, or not, may read them; NoResultFound for an id
    nobody holds, a person hidden here, and (by `lookup`) a deleted person."""
    person = db.session.get(User, person_id)
    if person is None or (person.deleted and not lookup.finds_deleted):
        raise NoResultFound
    refuse_hidden_person(person, signed_in)
    return person


def person_named(name: str, ap_domain: str | None, remote_named=None, lookup: Lookup = BY_NAME) -> User:
    """The one person `name` names: this storefront's account when `ap_domain` is None (a bare name), the account of
    the hosted domain `ap_domain` is, or else the remote account the caller's condition `remote_named` finds (both
    lowercased, as `normalise_actor_string` answers them). NoResultFound for nobody, more than one, a person hidden on
    this storefront, and (by `lookup`) a deleted person.

    Only the storefront's part of `hidden_here` is asked, as the lookup can come before the caller knows whether the
    viewer is signed in; the caller asks `refuse_hidden_person` once it does. The remote arm is the caller's because
    tests/test_local_name_lookups.py allows that lookup by name in alpha's `get_user` and nowhere else."""
    if ap_domain is None:
        found = User.query.filter(local_clause(User, name))
    else:
        hosted = domain_for_host(ap_domain)
        found = User.query.filter(local_named(User, name, hosted) if hosted is not None else remote_named)
    if not lookup.finds_deleted:
        found = found.filter(User.deleted == False)
    return one_here(found, User)


@dataclass(frozen=True)
class People:
    """A list of people: `scope` 'Local' (this storefront's verified, unbanned accounts) or anything else (every
    account not deleted, banned ones too); `search` a part of the name (or, with an '@' in it, of the remote address);
    `sort` New, Old, Top... (most posts first) or anything else (by id)."""
    scope: str = 'All'
    search: str = ''
    sort: str = 'Hot'


def person_listing(signed_in: bool, people: People):
    """The query listing `people` for a viewer who is signed in, or not, ordered: without the accounts hidden here
    (spec §3.4). The viewer's blocks are not applied."""
    if people.scope == 'Local':
        users = User.query.filter(User.instance_id == current_instance_id(), User.verified.is_(True),
                                  User.banned.is_(False), User.deleted.is_(False)).filter(
            on_domain(current_storefront()))
    else:
        users = User.query.filter(User.deleted == False)
    users = without_local_only_actors(users, User, visible=signed_in)

    if people.search:
        if '@' in people.search:
            users = users.filter(User.ap_id.ilike(f"%{people.search}%"))
        else:
            users = users.filter(User.user_name.ilike(f"%{people.search}%"))

    if people.sort == "New":
        return users.order_by(desc(User.created))
    if people.sort == "Old":
        return users.order_by(asc(User.created))
    if people.sort.startswith('Top'):
        return users.order_by(desc(User.post_count))
    return users.order_by(User.id)


def sees_all_of(person: User, viewer_id: int | None) -> bool:
    """Whether the viewer `viewer_id` (None: nobody signed in) is `person` or administers them ('administer all
    users' on them), the two the web profile shows a person's hidden moderating to (app/user/utils.py:417-419)."""
    return bool(viewer_id) and (viewer_id == person.id or user_access('administer all users', viewer_id, on=person))


def moderates_hidden_from(person: User, viewer_id: int | None, community_ids) -> set[int]:
    """Of `community_ids`, the communities `person` moderates, those the viewer may not be told they moderate: a
    community whose moderators are private (`private_mods`), as the web profile leaves it out
    (app/user/utils.py:417-419), and a private community the viewer is not a member of (membership as the post and
    reply listings ask it: `Restrictions.private_community_ids`, member and not banned), unless the viewer is the
    person or administers them."""
    if not community_ids or sees_all_of(person, viewer_id):
        return set()
    unjoined_private = Community.private == True
    if member_of := Restrictions(viewer_id).private_community_ids:
        unjoined_private = and_(unjoined_private, Community.id.not_in(member_of))
    return set(db.session.scalars(select(Community.id).where(
        Community.id.in_(community_ids), or_(Community.private_mods == True, unjoined_private))))


def moderators_hidden(community_id: int, viewer_id: int | None) -> bool:
    """Whether the moderators of the community `community_id` are hidden from the viewer `viewer_id` (None: nobody
    signed in): when they are private (`private_mods`) the web community page names none to anyone
    (app/community/routes.py:522-523) and only its moderator page names them, to the community's owner and moderators
    and an admin of communities (app/community/routes.py:1838, community_mod_list)."""
    community = db.session.get(Community, community_id)
    if community is None or not community.private_mods:
        return False
    if not viewer_id:
        return True
    if user_access('administer all communities', viewer_id, on=community):
        return False
    return not db.session.scalar(select(exists().where(
        CommunityMember.community_id == community_id, CommunityMember.user_id == viewer_id,
        or_(CommunityMember.is_moderator == True, CommunityMember.is_owner == True))))
