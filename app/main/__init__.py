from flask import Blueprint

bp = Blueprint('main', __name__)

from app.main import routes
from app.discovery import views as discovery_views  # noqa: E402,F401  fork (interop D24): /discovery/<id>/resolve
