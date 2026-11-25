"""Placeholder values that code renders: the lists in the unread notifications email (spec 2026-11-24-admin-messaging
D8). Called under the recipient's storefront, like the bundled templates they stand in for."""
import html

from flask import url_for

from app.email import mail_name
from app.messaging.render import Value
from app.storefront import storefront_url


def _items(pairs) -> Value:
    """(title, url) pairs as a plain list and an HTML one; no pairs, nothing."""
    pairs = list(pairs)
    if not pairs:
        return Value('', '')
    text = '\n'.join(f'- {title} - {url}' for title, url in pairs)
    items = ''.join(f'<li><a href="{html.escape(url, quote=True)}">{html.escape(title)}</a></li>' for title, url in pairs)
    return Value(text, f'<ul>{items}</ul>')


def _post_url(post) -> str:
    return storefront_url() + post.slug if post.slug else url_for('activitypub.post_ap', post_id=post.id, _external=True)


def digest_values(user, notifications, posts) -> dict:
    return {
        'name': Value(user.display_name()),
        'site_name': Value(mail_name(user)),
        'notifications': _items((n.title, url_for('user.notification_goto', notification_id=n.id, _external=True))
                                for n in notifications),
        'recent_posts': _items((post.title, _post_url(post)) for post in posts),
        'notifications_url': Value(url_for('user.notifications', _external=True)),
    }
