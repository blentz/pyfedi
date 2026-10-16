# Seed the harness PieFed: site setup, user alice, the local microblogs community, alice's video_channel
# community vc1, and the media upload communities vc_media (video_channel) and pod_media (podcast), which alice
# moderates, with video and audio uploads open to users. Idempotent.
from app import db
from app.activitypub.signature import RsaKeys
from app.activitypub.util import find_microblogging_community
from app.constants import COMMUNITY_KIND_PODCAST, COMMUNITY_KIND_VIDEO_CHANNEL
from app.models import Community, CommunityMember, User
from app.utils import gibberish, set_setting, utcnow
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
for name, kind in (('vc_media', COMMUNITY_KIND_VIDEO_CHANNEL), ('pod_media', COMMUNITY_KIND_PODCAST)):
    if Community.query.filter_by(name=name, ap_id=None).first() is None:
        # kind by the DB, as for vc1; alice moderates it, since only a moderator may upload (A13.6)
        url = current_app.config['SERVER_URL']
        private_key, public_key = RsaKeys.generate_keypair()
        owner = User.query.filter_by(user_name='alice', ap_id=None).one()
        community = Community(name=name, title=name, kind=kind, user_id=owner.id, instance_id=1,
                              private_key=private_key, public_key=public_key, nsfw=False, local_only=False,
                              ap_profile_id=f'{url}/c/{name}', ap_public_url=f'{url}/c/{name}',
                              ap_followers_url=f'{url}/c/{name}/followers',
                              ap_moderators_url=f'{url}/c/{name}/moderators',
                              ap_domain=current_app.config['SERVER_NAME'], subscriptions_count=1,
                              first_federated_at=utcnow())
        db.session.add(community)
        db.session.flush()
        db.session.add(CommunityMember(user_id=owner.id, community_id=community.id, is_moderator=True, is_owner=True))
        db.session.commit()
set_setting('allow_video_file_uploads', 'users')
set_setting('allow_audio_file_uploads', 'users')
print('seed_piefed: ok')
