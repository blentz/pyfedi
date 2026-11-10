"""PeerTube's blueprint with every route module registered on it, for app/__init__ to register (as app.mastodon.api)."""
from app.peertube import bp
# each registers its routes on bp
from app.peertube import actions, actors, auth, history, links, me, subscriptions, videos  # noqa: F401

__all__ = ['bp']
