#!/bin/bash

# The Jetstream consumer (ATProto, docs/superpowers/specs/2026-11-23-atproto-federation-design.md §4): one long-lived
# process per deployment, beside the web and Celery containers, that follows Jetstream for the Bluesky accounts
# members follow and hands each event to Celery. Like a worker it waits for the schema first (wait_for_schema.py).
python wait_for_schema.py || exit 1
exec flask --app pyfedi.py atproto-jetstream
