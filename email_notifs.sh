#!/bin/bash

source venv/bin/activate > /dev/null 2>&1
export FLASK_APP=pyfedi.py

# Run every step even if one fails, but exit non-zero if any did, so the
# scheduler sees the failure. Set after the venv line, which fails in the container.
status=0
trap 'status=1' ERR

flask send_missed_notifs
flask process_email_bounces
flask clean_up_old_activities

exit $status
