# Seed the harness PieFed: site setup, users alice and dora (dora only for test_loops' first Follow), the local microblogs community, alice's feed conf-feed, alice's video_channel
# community vc1, and the media upload communities vc_media (video_channel) and pod_media (podcast), which alice
# moderates, with video and audio uploads open to users. Also the public hosted domain tube.test (piefed.test is private,
# R219), with tess and her video_channel community tube, for apps that browse with no account (NewPipe). Idempotent.
from app import db
from app.activitypub.signature import RsaKeys
from app.activitypub.util import find_microblogging_community
from app.constants import COMMUNITY_KIND_PODCAST, COMMUNITY_KIND_VIDEO_CHANNEL
from app.models import Community, CommunityMember, Feed, SiteDomain, User
from app.shared.feed import feed_identity
from app.storefront import clear_domain_cache, default_domain, domain_for_host, instance_id_of
from app.utils import gibberish, set_setting, utcnow
from flask import current_app

for name in ('alice', 'dora'):
    if User.query.filter_by(user_name=name, ap_id=None).first() is None:
        private_key, public_key = RsaKeys.generate_keypair()
        user = User(user_name=name, title=name, email=f'{name}@piefed.test', instance_id=1,
                    private_key=private_key, public_key=public_key, alt_user_name=gibberish(12),
                    email_unread_sent=False, verified=True, last_seen=utcnow())
        user.set_password(f'interop-{name}-pw')
        url = current_app.config['SERVER_URL']
        user.ap_profile_id = f'{url}/u/{name}'
        user.ap_public_url = f'{url}/u/{name}'
        user.ap_inbox_url = f'{url}/u/{name}/inbox'
        db.session.add(user)
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
if Feed.query.filter_by(machine_name='conf-feed', ap_id=None).first() is None:
    # a feed belongs to a site domain, and one without a domain is not served: made as feed_new makes it
    owner = User.query.filter_by(user_name='alice', ap_id=None).one()
    private_key, public_key = RsaKeys.generate_keypair()
    db.session.add(Feed(user_id=owner.id, title='conf-feed', name='conf-feed', machine_name='conf-feed',
                        private_key=private_key, public_key=public_key, public=True, nsfw=False, nsfl=False,
                        subscriptions_count=1, **feed_identity(default_domain(), 'conf-feed')))
    db.session.commit()
if SiteDomain.query.filter_by(domain='tube.test').first() is None:
    db.session.add(SiteDomain(domain='tube.test', scheme='https', federates=True, registration='anyone',
                              login_required=False, is_default=False))
    db.session.commit()
    clear_domain_cache()
tube = domain_for_host('tube.test')
if User.query.filter_by(user_name='tess', ap_id=None, site_domain_id=tube.id).first() is None:
    private_key, public_key = RsaKeys.generate_keypair()
    tess = User(user_name='tess', title='tess', email='tess@tube.test', instance_id=instance_id_of(tube),
                site_domain_id=tube.id, private_key=private_key, public_key=public_key, alt_user_name=gibberish(12),
                email_unread_sent=False, verified=True, last_seen=utcnow(), ap_profile_id='https://tube.test/u/tess',
                ap_public_url='https://tube.test/u/tess', ap_inbox_url='https://tube.test/u/tess/inbox')
    tess.set_password('interop-tess-pw')
    db.session.add(tess)
    db.session.commit()
if Community.query.filter_by(name='tube', ap_id=None, site_domain_id=tube.id).first() is None:
    tess = User.query.filter_by(user_name='tess', ap_id=None, site_domain_id=tube.id).one()
    private_key, public_key = RsaKeys.generate_keypair()
    community = Community(name='tube', title='Tube', kind=COMMUNITY_KIND_VIDEO_CHANNEL, user_id=tess.id,
                          instance_id=instance_id_of(tube), site_domain_id=tube.id, private_key=private_key,
                          public_key=public_key, nsfw=False, local_only=False, show_all=True,
                          ap_profile_id='https://tube.test/c/tube', ap_public_url='https://tube.test/c/tube',
                          ap_followers_url='https://tube.test/c/tube/followers',
                          ap_moderators_url='https://tube.test/c/tube/moderators', ap_domain='tube.test',
                          subscriptions_count=1, first_federated_at=utcnow())
    db.session.add(community)
    db.session.flush()
    db.session.add(CommunityMember(user_id=tess.id, community_id=community.id, is_moderator=True, is_owner=True))
    db.session.commit()
set_setting('allow_video_file_uploads', 'users')
set_setting('allow_audio_file_uploads', 'users')
print('seed_piefed: ok')
