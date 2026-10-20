"""A link to a local account or community names its domain when that is not the storefront's (spec 2026-10-19 §3.2)."""
from app.models import User
from app.storefront import canonicalise_links, home_storefront, storefront_html
from tests.two_domains import HELL, ONION, default_request, onion_request, two_domains  # noqa: F401


def test_links_on_the_default_storefront_are_unchanged(app, two_domains):
    hell, _onion = two_domains.domains
    bob = two_domains.local_user('bob', hell)
    art = two_domains.local_community('art', hell)
    with default_request(app):
        assert bob.link() == 'bob'
        assert art.link() == 'art'
        assert bob.lemmy_link() == f'bob@{HELL}'
        assert art.lemmy_link() == f'!art@{HELL}'


def test_on_the_onion_a_hell_account_links_with_its_domain(app, two_domains):
    hell, onion = two_domains.domains
    bob = two_domains.local_user('bob', hell)
    carol = two_domains.local_user('carol', onion)
    with onion_request(app):
        assert bob.link() == f'bob@{HELL}'
        assert bob.lemmy_link() == f'bob@{HELL}'
        assert carol.link() == 'carol'
        assert carol.lemmy_link() == f'carol@{ONION}'


def test_on_hell_an_onion_community_links_with_its_domain(app, two_domains):
    _hell, onion = two_domains.domains
    art = two_domains.local_community('art', onion)
    with default_request(app):
        assert art.link() == f'art@{ONION}'
        assert art.lemmy_link() == f'!art@{ONION}'


def test_a_remote_account_links_as_before(app, two_domains):
    remote = User(user_name='rem', ap_id='rem@far.example', ap_profile_id='https://far.example/u/rem',
                  instance_id=two_domains.instance.id)
    with onion_request(app):
        assert remote.link() == 'rem@far.example'


def test_a_stored_hell_profile_link_on_the_onion_keeps_its_account(app, two_domains):
    html = (f'<a href="https://{HELL}/u/bob">bob</a> <a href="https://{HELL}/c/art?x=1#y">art</a> '
            f'<a href="https://{HELL}/post/5">p</a>')
    with onion_request(app):
        out = storefront_html(html)
    assert f'http://{ONION}/u/bob@{HELL}"' in out and f'http://{ONION}/c/art@{HELL}?x=1#y"' in out
    assert f'http://{ONION}/post/5"' in out


def test_only_an_exact_profile_path_is_qualified(app, two_domains):
    html = ''.join(f'<a href="https://{HELL}{path}">x</a>' for path in
                   ('/u/bob/followers', '/c/art/p/5', '/u/bob@far.example', '/u/', '/u'))
    with onion_request(app):
        out = storefront_html(html)
    for path in ('/u/bob/followers', '/c/art/p/5', '/u/bob@far.example', '/u/', '/u'):
        assert f'href="http://{ONION}{path}"' in out


def test_stored_links_qualify_another_domain_and_leave_the_canonical_one(app, two_domains):
    with default_request(app):
        out = canonicalise_links(f'<a href="http://{ONION}/u/bob">b</a><a href="https://{HELL}/u/bob">b</a>')
    assert f'href="https://{HELL}/u/bob@{ONION}"' in out
    assert f'href="https://{HELL}/u/bob"' in out


def test_an_onion_recipients_email_links_by_their_domain(app, two_domains):
    hell, onion = two_domains.domains
    hell_bob = two_domains.local_user('bob', hell)
    onion_bob = two_domains.local_user('bob', onion)
    with home_storefront(onion_bob):
        assert onion_bob.link() == 'bob'
        assert hell_bob.link() == f'bob@{HELL}'
    with home_storefront(hell_bob):
        assert hell_bob.link() == 'bob'
        assert onion_bob.link() == f'bob@{ONION}'
