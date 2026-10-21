"""The users a post's body Mentions with @user@instance syntax, and the stored user an address names.

Kept apart from app.user.utils (search_for_user) and app.shared.tasks.pages (mentioned_users) so that
app.activitypub.util can resolve the Mentions of a Note it is asked for at module top: importing either of those
from there closes an import cycle (app.user's package runs app.user.routes, which imports from app.activitypub.util).
"""
import re

from app import db
from app.local_names import local_named
from app.models import BannedInstances, User
from app.storefront import current_storefront, domain_for_host

MENTION_RE = re.compile(r"@([a-zA-Z0-9_.-]*)@([a-zA-Z0-9_.-]*)\b")


def user_address_parts(address: str):
    """(name, server) of `name@server` (lowercased), or (address, '') for a bare local name; None when `address` has
    an `@` and is not exactly that shape. A leading `@` is ignored."""
    if address.startswith('@'):
        address = address[1:]
    if '@' not in address:
        return address, ''
    # `name, server = address.split('@')` unpacked whatever it was given,
    # and this address comes from a URL segment or a search box.
    parts = address.lower().split('@')
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def stored_user(address: str, home=None):
    """The user `address` names among those already stored, as search_for_user(address, allow_fetch=False) finds it.
    A bare name is a local user on `home`, a hosted domain (the current storefront's when None); `name@host` for a
    host this site hosts is that domain's local user. Raises when the address's server is banned here."""
    parts = user_address_parts(address)
    if parts is None:
        return None
    name, server = parts
    if server:
        banned = db.session.query(BannedInstances).filter_by(domain=server).first()
        if banned:
            reason = f" Reason: {banned.reason}" if banned.reason is not None else ''
            raise Exception(f"{server} is blocked.{reason}")
        hosted = domain_for_host(server)
        if hosted:   # one of ours: that domain's own account, or nobody. Never fetched (it would be a self-request)
            return db.session.query(User).filter(local_named(User, name, hosted)).first()
        # `name` and `server` were lowercased above; `address` was not, so
        # this lookup was case-sensitive and a handle typed with any capital
        # letter in it found nobody.
        return db.session.query(User).filter_by(ap_id=f'{name}@{server}').first()
    # The name's case as typed, as this lookup has always matched it
    return db.session.query(User).filter(local_named(User, name, home or current_storefront(), exact_case=True)).first()


def mentioned_users_found_by(post, find_user) -> list:
    """The users a post's body Mentions with @user@instance syntax, once each, leaving out its author. `find_user`
    turns a name on a hosted domain (`@name@host` for any domain this site hosts), or an address, into a user or None;
    one that raises counts as None. A bare name means the author's home domain (`stored_user`'s `home`)."""
    user = post.author
    recipients = []
    if post.body:
        matches = MENTION_RE.finditer(post.body)
        for match in matches:
            recipient = None
            domain = domain_for_host(match.group(2))
            try:
                if domain:   # a hosted domain: the name is that domain's account
                    recipient = find_user(match.group(1), domain)
                else:
                    recipient = find_user(f"{match.group(1)}@{match.group(2)}")
            except:
                pass
            if recipient and recipient.id == user.id:
                recipient = None
            if recipient:
                add_recipient = True
                for existing_recipient in recipients:
                    if ((not recipient.ap_id and recipient.user_name == existing_recipient.user_name and
                         recipient.site_domain_id == existing_recipient.site_domain_id) or
                        (recipient.ap_id and recipient.ap_id == existing_recipient.ap_id)):
                        add_recipient = False
                        break
                if add_recipient:
                    recipients.append(recipient)
    return recipients


def stored_mentioned_users(post) -> list:
    """mentioned_users(post, allow_fetch=False): the Mentioned users among those already stored. Answering a fetch
    never fetches."""
    return mentioned_users_found_by(post, stored_user)
