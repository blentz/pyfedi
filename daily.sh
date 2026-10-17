#!/bin/bash

source venv/bin/activate > /dev/null 2>&1
export FLASK_APP=pyfedi.py

# Run every step even if one fails, but exit non-zero if any did, so the
# scheduler sees the failure. Set after the venv line, which fails in the container.
status=0
trap 'status=1' ERR

flask daily-maintenance
flask refresh_discovery
flask sync_discovery

exit $status
