"""gpodder's blueprint with every route module registered on it, for app/__init__ to register (as app.lemmy.api)."""
from app.gpodder import bp
# each registers its routes on bp
from app.gpodder import auth  # noqa: F401

__all__ = ['bp']
