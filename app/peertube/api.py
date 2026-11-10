"""PeerTube's blueprint with every route module registered on it, for app/__init__ to register (as app.mastodon.api)."""
from app.peertube import bp
from app.peertube import actors, auth, links, me, subscriptions, videos  # noqa: F401  each registers its routes on bp

__all__ = ['bp']
