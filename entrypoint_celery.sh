#!/bin/bash

# The queues and concurrency come from the environment so one image runs two workers: the default worker
# (celery,send: the inbox and outbound delivery) and a separate background worker (backfills, discovery
# sync, maintenance), which can then never hold the slots the inbox needs (tests/test_compose_celery_workers.py).
exec celery -A celery_worker_docker.celery worker --concurrency="${CELERY_CONCURRENCY:-4}" --queues="${CELERY_QUEUES:-celery,send}"
