#!/bin/bash

# The queues and concurrency come from the environment so one image runs two workers: the default worker
# (celery,send: the inbox and outbound delivery) and a separate background worker (backfills, discovery
# sync, maintenance), which can then never hold the slots the inbox needs (tests/test_compose_celery_workers.py).
# Each slot prefetches one task, so a slow task cannot hold a backlog behind it, and a child process is replaced
# after a bounded number of tasks, so a leak cannot grow without limit.
#
# Only the web container runs `flask db upgrade`, and the autodeploy restarts every unit at once. A worker that came up
# first ran queued tasks on a half-migrated schema and they failed. So wait here until the schema matches the code's
# migrations; if it never does, stop, and systemd restarts the unit (wait_for_schema.py, SCHEMA_WAIT_TIMEOUT).
python wait_for_schema.py || exit 1
exec celery -A celery_worker_docker.celery worker --concurrency="${CELERY_CONCURRENCY:-4}" --queues="${CELERY_QUEUES:-celery,send}" \
    --prefetch-multiplier="${CELERY_PREFETCH_MULTIPLIER:-1}" --max-tasks-per-child="${CELERY_MAX_TASKS_PER_CHILD:-1000}"
