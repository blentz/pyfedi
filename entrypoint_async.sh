#!/usr/bin/env sh
set -e

echo "Starting async server..."

# Only the web container runs `flask db upgrade`, and the autodeploy restarts every unit at once. Wait until the
# schema matches the code's migrations; if it never does, `set -e` stops the script and systemd restarts the unit.
python wait_for_schema.py

if [ "${FLASK_DEBUG:-}" = "1" ] && [ "${FLASK_ENV:-}" = "development" ]; then
  uvicorn fastapi_server:app --reload --host 0.0.0.0 --port 8000
else
  echo "Starting Uvicorn..."
  uvicorn fastapi_server:app --host 0.0.0.0 --port 8000
fi
