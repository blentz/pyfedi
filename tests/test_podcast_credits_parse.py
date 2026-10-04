"""Interop D24, decision 6: credits come from the podcast's RSS <podcast:person> tags. Channel-level
people are hosts; the matching item's role="guest" people are guests. The feed is untrusted."""
from pathlib import Path

import pytest

from app.discovery.credits import MAX_CREDITS, MAX_FEED_BYTES, parse_feed_credits

FIXTURE = Path(__file__).resolve().parent.parent / 'app' / 'discovery' / 'fixtures' / 'castopod_feed.xml'
EP1 = 'https://pod.example/@mypodcast/episodes/ep-1'
EP2 = 'https://pod.example/@mypodcast/episodes/ep-2'
ANN = {'name': 'Ann Host', 'role': 'host', 'image': 'https://pod.example/media/ann.jpg',
       'profile_url': 'https://social.example/@ann', 'user_id': None}
BEN = {'name': 'Ben Cohost', 'role': 'host', 'image': None, 'profile_url': 'https://ben.example/about', 'user_id': None}


def feed(channel_people='', items=''):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0" xmlns:podcast="https://podcastindex.org/namespace/1.0">'
            f'<channel><title>T</title>{channel_people}{items}</channel></rss>').encode()


def guest_item(link='', guid='', name='Gia Guest'):
    return (f'<item><link>{link}</link><guid>{guid}</guid>'
            f'<podcast:person role="guest">{name}</podcast:person></item>')


def test_channel_people_are_hosts_and_the_items_guests_are_guests(app):
    credits = parse_feed_credits(FIXTURE.read_bytes(), EP1)

    assert credits == [ANN, BEN, {'name': 'Cara Guest', 'role': 'guest', 'image': None,
                                  'profile_url': 'https://social.example/@cara', 'user_id': None}]


def test_another_episode_gets_its_own_guests(app):
    assert [c['name'] for c in parse_feed_credits(FIXTURE.read_bytes(), EP2)] == ['Ann Host', 'Ben Cohost', 'Dan Other']


def test_an_episode_missing_from_the_feed_gets_the_hosts_only(app):
    assert parse_feed_credits(FIXTURE.read_bytes(), 'https://pod.example/@mypodcast/episodes/nope') == [ANN, BEN]


def test_a_person_with_no_role_is_a_host(app):
    credits = parse_feed_credits(feed('<podcast:person>Rolf Noroll</podcast:person>'), EP1)

    assert [(c['name'], c['role']) for c in credits] == [('Rolf Noroll', 'host')]


def test_an_item_matches_by_guid_or_with_a_trailing_slash(app):
    """Review focus 5."""
    by_slash = feed(items=guest_item(link=EP1 + '/'))
    by_guid = feed(items=guest_item(link='https://pod.example/elsewhere', guid=EP1))

    assert [c['name'] for c in parse_feed_credits(by_slash, EP1)] == ['Gia Guest']
    assert [c['name'] for c in parse_feed_credits(by_guid, EP1 + '/')] == ['Gia Guest']


def test_a_broken_or_empty_feed_gives_no_credits(app):
    assert parse_feed_credits(b'', EP1) == []
    assert parse_feed_credits(b'not xml at all', EP1) == []
    assert parse_feed_credits(b'<rss><channel>', EP1) == []
    assert parse_feed_credits(b'<rss></rss>', EP1) == []
    assert parse_feed_credits(b'<html><body/></html>', EP1) == []


def test_a_feed_with_a_doctype_or_entity_is_refused(app):
    laughs = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>'
              b'<rss xmlns:podcast="https://podcastindex.org/namespace/1.0"><channel>'
              b'<podcast:person>&lol2;</podcast:person></channel></rss>')

    assert parse_feed_credits(laughs, EP1) == []


def test_an_encoded_doctype_cannot_slip_past_the_refusal(app):
    """P9: the check must not be a byte scan an encoding can hide from."""
    text = ('<?xml version="1.0" encoding="{enc}"?><!DOCTYPE rss [<!ENTITY who "Eve">]>'
            '<rss xmlns:podcast="https://podcastindex.org/namespace/1.0"><channel>'
            '<podcast:person>&who;</podcast:person></channel></rss>')
    for enc in ('utf-16', 'utf-16-le', 'utf-16-be', 'utf-32', 'utf-32-le', 'utf-32-be'):
        assert parse_feed_credits(text.format(enc=enc).encode(enc), EP1) == [], enc
    clean = feed('<podcast:person>Ann</podcast:person>').decode().replace('UTF-8', 'UTF-16').encode('utf-16')
    assert [c['name'] for c in parse_feed_credits(clean, EP1)] == ['Ann']


def test_a_clean_utf32_feed_gives_no_credits(app):
    """Expat cannot read UTF-32 at all, so even a clean UTF-32 feed is unreadable and credits nobody."""
    clean = feed('<podcast:person>Ann</podcast:person>').decode().replace('UTF-8', 'UTF-32')

    assert parse_feed_credits(clean.encode('utf-32'), EP1) == []


def test_a_feed_in_an_encoding_expat_cannot_read_gives_no_credits(app):
    """pyexpat raises LookupError for an unknown encoding name and ValueError for a multi-byte one."""
    for enc in ('bogus', 'Shift_JIS', 'big5', 'utf-7'):
        text = feed('<podcast:person>Ann</podcast:person>').decode().replace('UTF-8', enc)
        assert parse_feed_credits(text.encode(), EP1) == [], enc


def test_a_feed_over_two_megabytes_is_refused(app):
    padded = FIXTURE.read_bytes().replace(b'</channel>', b'<!--' + b'x' * MAX_FEED_BYTES + b'--></channel>')

    assert parse_feed_credits(padded, EP1) == []


def test_untrusted_values_are_cleaned(app):
    person = ('<podcast:person img="javascript:alert(1)" href="http://plain.example/me">'
              '<![CDATA[<b>Bold</b> Name]]></podcast:person>')

    assert parse_feed_credits(feed(person), EP1) == [{'name': 'Bold Name', 'role': 'host', 'image': None,
                                                      'profile_url': None, 'user_id': None}]


def test_credits_are_capped(app):
    people = ''.join(f'<podcast:person>Host {i}</podcast:person>' for i in range(30))

    assert len(parse_feed_credits(feed(people), EP1)) == MAX_CREDITS == 20


def _declared(enc):
    return feed('<podcast:person>Ann</podcast:person>').decode().replace('UTF-8', enc)


HOSTILE_FEEDS = {
    'empty': b'',
    'junk': b'\xff\xfe\xfd',
    'utf-16 bom': _declared('UTF-16').encode('utf-16'),
    'unknown encoding': _declared('bogus').encode(),
    'utf-7': _declared('utf-7').encode(),
    'shift_jis': _declared('Shift_JIS').encode(),
    'nul byte': feed('<podcast:person>A\x00nn</podcast:person>'),
    'utf-8 body declared iso-8859-1': _declared('iso-8859-1').replace('Ann', 'Änn').encode(),
    'deep nest': b'<rss><channel>' + b'<a>' * 200000 + b'</a>' * 200000 + b'</channel></rss>',
    'utf-32': _declared('UTF-32').encode('utf-32'),
    'utf-32 without a declaration': feed('<podcast:person>Ann</podcast:person>').decode()
                                    .replace('<?xml version="1.0" encoding="UTF-8"?>', '').encode('utf-32'),
    'encoding name with a nul': _declared('utf\x00-8').encode(),
    'empty encoding name': _declared('').encode(),
}


@pytest.mark.parametrize('body', HOSTILE_FEEDS.values(), ids=HOSTILE_FEEDS.keys())
def test_hostile_feed_bytes_never_raise(app, body):
    assert isinstance(parse_feed_credits(body, EP1), list)
