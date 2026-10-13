"""Security track S0: who signed a GET, and may that signer see an audience.

Signatures are made by production's signer (tests/factories.py:
signed_get_headers) and checked by production's verifier; neither is patched.
"""
import time
from unittest.mock import patch

import pytest

from app import db
from app.activitypub.entitlement import (PRIVATE_HEADERS, _owner_candidates, addressees_of, audience_entitles,
                                         signed_requester_actor)
from app.activitypub.util import host_of
from app.activitypub.signature import http_date
from app.models import Instance
from tests.test_ap_content_objects import seed_local_post
from tests.factories import (cavage_signed_headers, make_community, make_community_member, make_follow,
                             make_instance, make_site, make_user, seed_community_owner, signed_get_headers)

pytestmark = pytest.mark.usefixtures('redis_double')

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


def _remote(domain, name, with_keys=True):
    instance = db.session.query(Instance).filter_by(domain=domain).first() or make_instance(domain)
    return make_user(instance, name, with_keys=with_keys)


def _local(app, name):
    instance = db.session.query(Instance).filter_by(domain=app.config['SERVER_NAME']).first() \
        or make_instance(app.config['SERVER_NAME'])
    user = make_user(instance, name, local=True)
    user.ap_profile_id = f"{app.config['SERVER_URL']}/u/{name}".lower()
    db.session.commit()
    return user


def _requester_for(app, path, headers):
    with app.test_request_context(path, method='GET', headers=headers):
        return signed_requester_actor()


# ---- signed_requester_actor -------------------------------------------------

def test_a_validly_signed_get_names_its_signer(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')
    assert _requester_for(app, '/post/1', signed_get_headers('/post/1', signer)) == signer


def test_a_signed_get_with_a_stale_date_names_nobody(app, db_session):
    """A captured signed GET must not be replayable forever: a Date 2 h old is anonymous."""
    make_site()
    signer = _remote('peer.example', 'alice')
    with patch('app.activitypub.signature.http_date', lambda: http_date(time.time() - 7200)):
        headers = signed_get_headers('/post/1', signer)
    assert _requester_for(app, '/post/1', headers) is None


def test_a_signed_get_without_a_date_names_nobody(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')
    headers = signed_get_headers('/post/1', signer)
    del headers['Date']
    assert _requester_for(app, '/post/1', headers) is None


def test_an_unsigned_get_names_nobody(app, db_session):
    make_site()
    assert _requester_for(app, '/post/1', {}) is None


def test_a_signature_header_without_a_keyid_names_nobody(app, db_session):
    make_site()
    assert _requester_for(app, '/post/1', {'Signature': 'headers="date",signature="AAAA"'}) is None


def test_an_unknown_signer_names_nobody_and_fetches_nothing(app, db_session, http_mock):
    """Never fetches: http_mock has no routes registered, so any GET would raise."""
    make_site()
    signer = _remote('peer.example', 'alice')
    headers = signed_get_headers('/post/1', signer, key_id='https://stranger.example/users/bob#main-key')
    assert _requester_for(app, '/post/1', headers) is None


def test_a_signature_that_does_not_verify_names_nobody(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')
    other = _remote('peer.example', 'bob')
    headers = signed_get_headers('/post/1', other, key_id=f'{signer.ap_profile_id}#main-key')
    assert _requester_for(app, '/post/1', headers) is None


def test_a_signed_path_other_than_the_requested_one_names_nobody(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')
    assert _requester_for(app, '/post/2', signed_get_headers('/post/1', signer)) is None


def test_a_gotosocial_style_main_key_path_names_its_owner(app, db_session):
    """Review Focus 1: GoToSocial keyIds are '<actor>/main-key', no fragment."""
    make_site()
    signer = _remote('peer.example', 'alice')
    headers = signed_get_headers('/post/1', signer, key_id=f'{signer.ap_profile_id}/main-key')
    assert _requester_for(app, '/post/1', headers) == signer


@pytest.mark.parametrize('key_id', [
    'https://Peer.Example/users/alice#main-key',
    'https://peer.example:443/users/alice#main-key',
    'HTTPS://PEER.EXAMPLE/users/alice#main-key',
])
def test_a_keyid_host_differing_only_by_case_or_default_port_names_its_owner(app, db_session, key_id):
    """Review Focus 3."""
    make_site()
    signer = _remote('peer.example', 'alice')
    headers = signed_get_headers('/post/1', signer, key_id=key_id)
    assert _requester_for(app, '/post/1', headers) == signer


def test_a_keyid_on_a_non_default_port_is_looked_up_with_that_port(app, db_session, http_mock):
    """The port is part of the candidate id, so a peer on :8443 is not mistaken for the stored :443 actor."""
    make_site()
    signer = _remote('peer.example', 'alice')
    headers = signed_get_headers('/post/1', signer, key_id='https://peer.example:8443/users/alice#main-key')
    assert _requester_for(app, '/post/1', headers) is None


BAD_SIGNATURES = [
    'keyId="https://peer.example/users/alice#main-key",headers="date",signature="A",algorithm="rsa-sha256"',
    'keyId="https://peer.example:99999/users/alice#main-key",headers="date",signature="AAAA",algorithm="rsa-sha256"',
    'keyId="https://peer.example:abc/users/alice#main-key",headers="date",signature="AAAA",algorithm="rsa-sha256"',
]


@pytest.mark.parametrize('signature', BAD_SIGNATURES)
def test_a_malformed_signature_header_names_nobody(app, db_session, signature):
    make_site()
    _remote('peer.example', 'alice')
    # A fresh Date, so the request gets past check_date to the keyId and signature parsing
    assert _requester_for(app, '/post/1', {'Date': http_date(), 'Signature': signature}) is None


@pytest.mark.parametrize('signature', BAD_SIGNATURES)
def test_a_malformed_signature_header_does_not_break_a_public_get(app, db_session, signature):
    make_site()
    _community, _author, post = seed_local_post()
    with app.test_client() as client:
        response = client.get(f'/post/{post.id}', headers={'Accept': 'application/activity+json',
                                                           'Signature': signature})
    assert response.status_code < 500


@pytest.mark.parametrize('enforce', [False, True])
@pytest.mark.parametrize('left_out', ['date', '(request-target)'])
def test_a_signed_get_leaving_a_required_header_unsigned_names_nobody(app, db_session, monkeypatch, enforce,
                                                                      left_out):
    """Authorized fetch is a new privilege, so GET_REQUIRED is enforced whatever SIG_REQUIRED_HEADERS_ENFORCE
    says: with `date` unsigned the checked Date could be swapped, and a replay could never expire."""
    monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', enforce)
    make_site()
    signer = _remote('peer.example', 'alice')
    signed = tuple(name for name in ('(request-target)', 'host', 'date') if name != left_out)
    headers = cavage_signed_headers(signer.private_key, f'{signer.ap_profile_id}#main-key', method='get',
                                    path='/post/1', signed=signed)
    assert _requester_for(app, '/post/1', headers) is None


def test_a_signed_get_covering_every_required_header_names_its_signer_with_the_flag_off(app, db_session,
                                                                                         monkeypatch):
    monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', False)
    make_site()
    signer = _remote('peer.example', 'alice')
    headers = cavage_signed_headers(signer.private_key, f'{signer.ap_profile_id}#main-key', method='get',
                                    path='/post/1', signed=('(request-target)', 'host', 'date'))
    assert _requester_for(app, '/post/1', headers) == signer


def test_a_keyid_with_no_host_names_nobody(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')
    headers = signed_get_headers('/post/1', signer, key_id='main-key')
    assert _requester_for(app, '/post/1', headers) is None


def test_a_banned_signer_names_nobody(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')
    signer.banned = True
    db.session.commit()
    assert _requester_for(app, '/post/1', signed_get_headers('/post/1', signer)) is None


# ---- addressees_of ----------------------------------------------------------

def test_addressees_are_the_union_of_every_addressing_field():
    doc = {'to': 'https://a.example/u/x', 'cc': ['https://b.example/u/y', {'id': 'https://c.example/u/z'}],
           'audience': 'https://d.example/c/g', 'bto': ['https://e.example/u/w'], 'bcc': 'https://f.example/u/v'}
    assert addressees_of(doc) == ['https://a.example/u/x', 'https://b.example/u/y', 'https://c.example/u/z',
                                  'https://d.example/c/g', 'https://e.example/u/w', 'https://f.example/u/v']


def test_non_string_and_empty_addressees_are_dropped():
    assert addressees_of({'to': [None, 3, '', {'id': 7}, {}], 'cc': None}) == []


def test_a_document_that_is_not_a_dict_has_no_addressees():
    assert addressees_of(['https://a.example/u/x']) == []


# ---- audience_entitles ------------------------------------------------------

@pytest.mark.parametrize('public', [PUBLIC, 'as:Public', 'Public'])
def test_rule_1_a_public_audience_entitles_anyone(public):
    assert audience_entitles([public], None) is True


def test_a_non_public_audience_entitles_no_anonymous_requester():
    assert audience_entitles(['https://peer.example/users/alice'], None) is False


def test_rule_2_an_addressed_requester_is_entitled(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')
    assert audience_entitles([signer.ap_profile_id.upper()], signer) is True


def test_rule_3_a_requester_on_an_addressees_host_is_entitled(app, db_session):
    make_site()
    signer = _remote('peer.example', 'instanceactor')
    assert audience_entitles(['https://PEER.example:443/users/bob'], signer) is True


def test_rule_3_does_not_count_a_followers_collection_as_an_addressed_actor(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')
    assert audience_entitles(['https://peer.example/users/bob/followers'], signer) is False


def test_a_requester_on_an_unrelated_host_is_not_entitled(app, db_session):
    make_site()
    signer = _remote('other.example', 'mallory')
    assert audience_entitles(['https://peer.example/users/bob'], signer) is False


def test_an_addressee_with_no_host_never_matches(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')
    assert audience_entitles(['not a url', ''], signer) is False


def test_a_requester_with_no_host_is_not_entitled(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')
    signer.ap_profile_id = 'garbage'
    signer.ap_public_url = 'garbage'
    db.session.commit()
    assert audience_entitles(['https://peer.example/users/bob'], signer) is False


def test_rule_4_an_accepted_follower_of_a_local_user_is_entitled(app, db_session):
    make_site()
    author = _local(app, 'author')
    follower = _remote('peer.example', 'alice')
    make_follow(author, follower, is_accepted=True, is_inward=True)
    followers = f'{author.public_url()}/followers'
    assert audience_entitles([followers], follower, local_actor=author) is True


def test_rule_4_another_account_on_a_followers_host_is_entitled(app, db_session):
    make_site()
    author = _local(app, 'author')
    follower = _remote('peer.example', 'alice')
    make_follow(author, follower, is_accepted=True, is_inward=True)
    instance_actor = _remote('peer.example', 'instanceactor')
    assert audience_entitles([f'{author.public_url()}/followers'], instance_actor, local_actor=author) is True


def test_rule_4_a_community_requester_on_a_followers_host_is_entitled(app, db_session):
    make_site()
    seed_community_owner('peer.example')
    author = _local(app, 'author')
    follower = _remote('peer.example', 'alice')
    make_follow(author, follower, is_accepted=True, is_inward=True)
    requester = make_community('remotegroup', host='peer.example')
    assert audience_entitles([f'{author.public_url()}/followers'], requester, local_actor=author) is True


@pytest.mark.parametrize('is_accepted', [None, False])
def test_rule_4_a_pending_or_rejected_follower_is_not_entitled(app, db_session, is_accepted):
    make_site()
    author = _local(app, 'author')
    follower = _remote('peer.example', 'alice')
    make_follow(author, follower, is_accepted=is_accepted, is_inward=True)
    assert audience_entitles([f'{author.public_url()}/followers'], follower, local_actor=author) is False


def test_rule_4_an_outward_follow_does_not_count(app, db_session):
    """is_inward=False is the local author following the remote user, not the reverse."""
    make_site()
    author = _local(app, 'author')
    followed = _remote('peer.example', 'alice')
    make_follow(author, followed, is_accepted=True, is_inward=False)
    assert audience_entitles([f'{author.public_url()}/followers'], followed, local_actor=author) is False


def test_rule_4_needs_the_followers_collection_to_be_addressed(app, db_session):
    make_site()
    author = _local(app, 'author')
    follower = _remote('peer.example', 'alice')
    make_follow(author, follower, is_accepted=True, is_inward=True)
    assert audience_entitles(['https://elsewhere.example/users/z'], follower, local_actor=author) is False


def test_rule_4_a_member_host_of_a_local_community_is_entitled(app, db_session):
    make_site()
    seed_community_owner('peer.example')
    community = make_community('localgroup', host=app.config['SERVER_NAME'])
    member = _remote('peer.example', 'alice')
    make_community_member(member, community)
    instance_actor = _remote('peer.example', 'instanceactor')
    assert audience_entitles([f'{community.public_url()}/followers'], instance_actor,
                             local_actor=community) is True


def test_rule_4_a_banned_member_does_not_entitle_their_host(app, db_session):
    make_site()
    seed_community_owner('peer.example')
    community = make_community('localgroup', host=app.config['SERVER_NAME'])
    member = _remote('peer.example', 'alice')
    membership = make_community_member(member, community)
    membership.is_banned = True
    db.session.commit()
    assert audience_entitles([f'{community.public_url()}/followers'], member,
                             local_actor=community) is False


def test_rule_4_any_other_local_actor_type_entitles_nobody(app, db_session):
    make_site()
    signer = _remote('peer.example', 'alice')

    class NotAnActor:
        ap_followers_url = 'https://test.piefed.local/f/feed/followers'

        def public_url(self):
            return 'https://test.piefed.local/f/feed'

    assert audience_entitles(['https://test.piefed.local/f/feed/followers'], signer,
                             local_actor=NotAnActor()) is False


def test_private_headers_are_the_contract_values():
    assert PRIVATE_HEADERS == {'Cache-Control': 'private, no-store', 'Vary': 'Accept, Signature'}


# ---- signed_requestor_domain keeps its behaviour on the new helper ----------

def test_signed_requestor_domain_reports_the_signers_host(app, db_session):
    from app.activitypub.routes import signed_requestor_domain
    make_site()
    signer = _remote('peer.example', 'alice')
    with app.test_request_context('/post/1', method='GET', headers=signed_get_headers('/post/1', signer)):
        assert signed_requestor_domain() == 'peer.example'


def test_signed_requestor_domain_falls_back_to_the_user_agent(app, db_session):
    from app.activitypub.routes import signed_requestor_domain
    make_site()
    with app.test_request_context('/post/1', method='GET',
                                  headers={'User-Agent': 'Test (+https://agent.example)'}):
        assert signed_requestor_domain() == 'agent.example'


# ---- R-SECUREACTORS (c): keyIds whose actor id is a root URL or carries a query ----

@pytest.mark.parametrize('actor_id, key_id', [
    ('https://lemmy.example/', 'https://lemmy.example/#main-key'),            # Lemmy's site actor
    ('https://blog.example/?author=1', 'https://blog.example/?author=1#main-key'),   # WordPress ActivityPub
])
def test_a_root_or_query_actor_id_names_its_owner(app, db_session, actor_id, key_id):
    make_site()
    signer = _remote(host_of(actor_id), 'site')
    signer.ap_profile_id = actor_id
    db.session.commit()

    headers = signed_get_headers('/post/1', signer, key_id=key_id)

    assert _requester_for(app, '/post/1', headers) == signer


def test_a_keyid_with_no_scheme_names_no_owner():
    """A network-path keyId ('//host/path') has a host but no scheme, and the candidates built from it began
    '://host', which names no host at all, against the docstring's "all on the keyId's own host". A keyId that
    is not an absolute URL names nobody. Found by the ap_entitlement fuzz target."""
    assert _owner_candidates('//peer.example/users/alice#main-key') == []


def test_owner_candidates_keep_the_root_slash_and_the_query():
    assert _owner_candidates('https://lemmy.example/#main-key') == ['https://lemmy.example/', 'https://lemmy.example']
    assert _owner_candidates('https://blog.example/?author=1#main-key')[0] == 'https://blog.example/?author=1'
    assert _owner_candidates('https://peer.example/users/alice/main-key') == [
        'https://peer.example/users/alice/main-key', 'https://peer.example/users/alice', 'https://peer.example/users']
