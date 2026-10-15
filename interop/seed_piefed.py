# Seed the harness PieFed: site setup, user alice, the local microblogs community, and alice's video_channel
# community vc1. Idempotent.
from app import db
from app.activitypub.signature import RsaKeys
from app.activitypub.util import find_microblogging_community
from app.constants import COMMUNITY_KIND_VIDEO_CHANNEL
from app.models import Community, User
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
if Community.query.filter_by(name='vc1', ap_id=None).first() is None:
    # kind is set here: no form sets it yet (spec 2026-10-14 §3.2, step 4)
    url = current_app.config['SERVER_URL']
    private_key, public_key = RsaKeys.generate_keypair()
    owner = User.query.filter_by(user_name='alice', ap_id=None).one()
    db.session.add(Community(name='vc1', title='vc1', kind=COMMUNITY_KIND_VIDEO_CHANNEL, user_id=owner.id, instance_id=1,
                             private_key=private_key, public_key=public_key, nsfw=False, local_only=False,
                             ap_profile_id=f'{url}/c/vc1', ap_public_url=f'{url}/c/vc1',
                             ap_followers_url=f'{url}/c/vc1/followers', ap_moderators_url=f'{url}/c/vc1/moderators',
                             ap_domain=current_app.config['SERVER_NAME'], subscriptions_count=0,
                             first_federated_at=utcnow()))
    db.session.commit()
print('seed_piefed: ok')
