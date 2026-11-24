# Piped into the harness PieFed by interop/run.sh bluesky, after `from pyfedi import app` and an app context.
# STATE is 'on' or 'off'. On: Bluesky switched on, members may create accounts on the PDS, and piefed.test public for
# the run (it is private for the rest of the harness, R219, and nothing from a private instance ever goes to a
# connected account). Off puts all three back and forgets every connection and copy the run made, deleting the
# accounts PieFed made from the PDS.
from app import cache, db
from app.connections import bluesky_hosted, setting_name
from app.models import Site, SocialConnection, SocialCrosspost
from app.utils import get_site_as_dict, set_setting

on = STATE == 'on'   # noqa: F821  (run.sh prepends it)
site = db.session.get(Site, 1)
site.private_instance = not on
SocialCrosspost.query.delete()
for hosted_row in bluesky_hosted.hosted_rows().all():   # the PDS keeps accounts between runs: those PieFed made go
    bluesky_hosted.delete_account(hosted_row)
SocialConnection.query.delete()
db.session.commit()
cache.delete_memoized(get_site_as_dict)   # as the admin pages do: requests read the site through a 60 s cache
set_setting(setting_name('bluesky'), on)
set_setting(setting_name(bluesky_hosted.HOSTING), on)
print(f'piefed_state: bluesky {"on" if on else "off"}, private_instance={site.private_instance}')
