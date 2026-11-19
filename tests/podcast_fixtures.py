"""Fixtures for the tests of a local podcast community's episodes: a ready MP3 upload with no cover art, followed
from a Mastodon and a Lemmy instance."""
import uuid
from types import SimpleNamespace

import pytest
from flask import current_app, g

from app import db
from app.constants import COMMUNITY_KIND_PODCAST, POST_TYPE_LINK
from app.models import CommunityMember, Site
from app.shared.media import with_media
from tests.factories import a_keypair, make_community, make_instance, make_post, make_site, make_user
from tests.peertube_video_fixtures import _remote


