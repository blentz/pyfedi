# Piped into the harness PieFed by interop/run.sh bluesky, after `from pyfedi import app` and an app context.
# STATE is 'on' or 'off'. On: Bluesky switched on, and piefed.test public for the run (it is private for the rest of
# the harness, R219, and nothing from a private instance ever goes to a connected account). Off puts both back and
# forgets every connection and copy the run made.
from app import db
from app.connections import setting_name
from app.models import Site, SocialConnection, SocialCrosspost
from app.utils import set_setting

on = STATE == 'on'   # noqa: F821  (run.sh prepends it)
site = db.session.get(Site, 1)
site.private_instance = not on
SocialCrosspost.query.delete()
SocialConnection.query.delete()
db.session.commit()
set_setting(setting_name('bluesky'), on)
print(f'piefed_state: bluesky {"on" if on else "off"}, private_instance={site.private_instance}')
