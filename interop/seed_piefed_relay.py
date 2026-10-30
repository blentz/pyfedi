# Subscribe the harness PieFed to the stand-in for Relay by Loops (interop/relay_standin.py), as an admin's relay page
# does. run.sh runs it once relay.test answers, with the harness CA bundle (an exec has no SSL_CERT_FILE). Idempotent.
from app import db
from app.models import Relay
from app.relays.subscribe import add_relay

RELAY = 'https://relay.test/actor'
if db.session.query(Relay).filter_by(url=RELAY).first() is None:
    add_relay(RELAY)
print('seed_piefed_relay: ok')
