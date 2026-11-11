"""The Mastodon client API's blueprint with every route module registered on it, for app/__init__ to register. Kept
apart from the package's __init__, so a module that needs only a helper of the package (app.activitypub.routes reads
app.mastodon.instance) does not load every route, and through them the read layer, part way through its own import."""
import app.push_delivery  # noqa: F401  registers the listeners that push each new notification
from app.mastodon import bp
from app.mastodon import actions, compose, direct, extras, media, notifications, pixelfed, push, relations, \
    routes, search, stories, timelines  # noqa: F401  each registers its routes on bp

__all__ = ['bp']
