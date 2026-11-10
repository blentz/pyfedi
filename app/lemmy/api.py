"""Lemmy's blueprint with every route module registered on it, for app/__init__ to register (as app.peertube.api)."""
from app.lemmy import bp
# each registers its routes on bp
from app.lemmy import errors, site  # noqa: F401

__all__ = ['bp']
