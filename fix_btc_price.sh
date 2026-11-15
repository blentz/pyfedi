#!/bin/bash

source venv/bin/activate
export FLASK_APP=pyfedi.py
flask fix_btc_price
