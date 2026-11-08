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

from sqlalchemy import asc, desc
from sqlalchemy.orm.exc import NoResultFound

from app import db
from app.local_names import local_clause, local_named
from app.local_only import hidden_here, one_here, without_local_only_actors
from app.models import User
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
