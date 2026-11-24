#!/usr/bin/env python
import os
from app import celery, create_app

app = create_app()
app.app_context().push()

# Import all task modules to register them with Celery
from app.shared.tasks import maintenance
from app.shared.tasks import follows, likes, notes, deletes, flags, pages, locks, adds, removes, groups, users, blocks, media
# tasks no blueprint imports: Jetstream events and ATProto backfills (spec 2026-11-23), hosted Bluesky accounts
from app.atproto import actors as atproto_actors, identity as atproto_identity, ingest as atproto_ingest, \
    notify as atproto_notify  # noqa: F401
from app.connections import bluesky_hosted  # noqa: F401
