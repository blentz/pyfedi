"""The users a post's body Mentions with @user@instance syntax, and the stored user an address names.

Kept apart from app.user.utils (search_for_user) and app.shared.tasks.pages (mentioned_users) so that
app.activitypub.util can resolve the Mentions of a Note it is asked for at module top: importing either of those
from there closes an import cycle (app.user's package runs app.user.routes, which imports from app.activitypub.util).
"""
import re

from flask import current_app

from app import db
from app.models import BannedInstances, User

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


def stored_user(address: str):
    """The user `address` names among those already stored, as search_for_user(address, allow_fetch=False) finds it.
    Raises when the address's server is banned here."""
    parts = user_address_parts(address)
    if parts is None:
        return None
    name, server = parts
    if server:
        banned = db.session.query(BannedInstances).filter_by(domain=server).first()
        if banned:
            reason = f" Reason: {banned.reason}" if banned.reason is not None else ''
            raise Exception(f"{server} is blocked.{reason}")
        # `name` and `server` were lowercased above; `address` was not, so
        # this lookup was case-sensitive and a handle typed with any capital
        # letter in it found nobody.
        return db.session.query(User).filter_by(ap_id=f'{name}@{server}').first()
    return db.session.query(User).filter_by(user_name=name, ap_id=None).first()


def mentioned_users_found_by(post, find_user) -> list:
    """The users a post's body Mentions with @user@instance syntax, once each, leaving out its author. `find_user`
    turns an address into a user or None; one that raises counts as None."""
    user = post.author
    recipients = []
    if post.body:
        matches = MENTION_RE.finditer(post.body)
        for match in matches:
            recipient = None
            if match.group(2) == current_app.config['SERVER_NAME']:
                user_name = match.group(1)
                if user_name != user.user_name:
                    try:
                        recipient = find_user(user_name)
                    except:
                        pass
            else:
                ap_id = f"{match.group(1)}@{match.group(2)}"
                try:
                    recipient = find_user(ap_id)
                except:
                    pass
            if recipient:
                add_recipient = True
                for existing_recipient in recipients:
                    if ((not recipient.ap_id and recipient.user_name == existing_recipient.user_name) or
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
