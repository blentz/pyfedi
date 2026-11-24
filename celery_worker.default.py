#!/usr/bin/env python
import os
from app import celery, create_app


app = create_app()
if not app.debug:
    os.environ['DATABASE_URL'] = 'postgresql+psycopg2://pyfedi:pyfedi@127.0.0.1/pyfedi'
    os.environ['SERVER_NAME'] = 'piefed.ngrok.app'

app.app_context().push()

from app.shared.tasks import maintenance
from app.shared.tasks import follows, likes, notes, deletes, flags, pages, locks, adds, removes, groups, users, blocks, media
# tasks no blueprint imports: Jetstream events and ATProto backfills (spec 2026-11-23), hosted Bluesky accounts
from app.atproto import actors as atproto_actors, ingest as atproto_ingest  # noqa: F401
from app.connections import bluesky_hosted  # noqa: F401
