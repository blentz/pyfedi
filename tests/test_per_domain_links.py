"""A link to a local account or community names its domain when that is not the storefront's (spec 2026-10-19 §3.2)."""
import pytest

from app.models import User
from app.storefront import canonicalise_links, canonicalise_text, home_storefront, storefront_html
from app.utils import allowlist_html, markdown_to_html
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


def test_a_stored_link_moves_only_its_host_and_is_qualified_on_render(app, two_domains):
    """Important 4 (ruling 2026-10-20): canonicalising never writes `name@<onion>` into stored content, which
    federates; it moves the host alone. A bare path names the default domain's account wherever it was typed."""
    with default_request(app):
        out = canonicalise_links(f'<a href="http://{ONION}/u/bob">b</a><a href="https://{HELL}/u/bob">b</a>')
    assert out == f'<a href="https://{HELL}/u/bob">b</a><a href="https://{HELL}/u/bob">b</a>'


@pytest.mark.parametrize('typed', [f'http://{ONION}/u/bob', f'http://{ONION}/c/art?x=1#y'])
def test_html_and_markdown_typed_on_the_onion_store_the_same_link_and_no_onion_host(app, two_domains, typed):
    canonical = typed.replace(f'http://{ONION}', f'https://{HELL}')
    with onion_request(app):
        html = allowlist_html(f'<p><a href="{typed}">x</a></p>')
        markdown = markdown_to_html(f'[x]({typed})')
        source = canonicalise_text(f'[x]({typed})')
    for stored in (html, markdown, source):
        assert ONION not in stored, stored
    assert f'href="{canonical}"' in html and f'href="{canonical}"' in markdown
    assert source == f'[x]({canonical})'


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
