# Seed the harness PieFed: site setup, user alice, the local microblogs community. Idempotent.
from app import db
from app.activitypub.signature import RsaKeys
from app.activitypub.util import find_microblogging_community
from app.models import User
from app.utils import gibberish, utcnow
from flask import current_app

if User.query.filter_by(user_name='alice', ap_id=None).first() is None:
    private_key, public_key = RsaKeys.generate_keypair()
    alice = User(user_name='alice', title='alice', email='alice@piefed.test', instance_id=1,
                 private_key=private_key, public_key=public_key, alt_user_name=gibberish(12),
                 email_unread_sent=False, verified=True, last_seen=utcnow())
    alice.set_password('interop-alice-pw')
    url = current_app.config['SERVER_URL']
    alice.ap_profile_id = f'{url}/u/alice'
    alice.ap_public_url = f'{url}/u/alice'
    alice.ap_inbox_url = f'{url}/u/alice/inbox'
    db.session.add(alice)
    db.session.commit()
find_microblogging_community()
print('seed_piefed: ok')
