"""The CLI fetches whose bodies are legitimately over get_request's default 1 MiB cap pass a bigger one.

`flask populate_community_search` pulls data.lemmyverse.net's community.full.json (4.8 MB today) and `flask
rss-feeds` pulls each feed (and follows one redirect); both would be refused under the default cap.
"""
from datetime import timedelta
from unittest.mock import Mock, mock_open, patch

import pytest

from app import cli, db
from app.models import RssFeed, utcnow
from app.utils import DOMAIN_LIST_MAX_BYTES, DOMAIN_LIST_MAX_SECONDS, LEMMYVERSE_MAX_BYTES, LEMMYVERSE_MAX_SECONDS, MEDIA_MAX_BYTES, MEDIA_MAX_SECONDS
from tests.factories import make_community, make_instance, make_site, make_user


@pytest.fixture
def commands(app, db_session):
    cli.register(app)   # pyfedi.py registers the commands; the test app has none
    return app.test_cli_runner()


def test_the_lemmyverse_list_is_fetched_under_its_own_bigger_cap(commands):
    fetch = Mock(side_effect=[Mock(json=Mock(return_value=[])), Mock(status_code=404)])

    with patch('app.cli.get_request', fetch), patch('app.cli.ensure_directory_exists'), \
            patch('app.cli.open', mock_open(), create=True):
        result = commands.invoke(args=['populate_community_search'])

    assert result.exception is None, result.exception
    assert fetch.call_args_list[0].args == ('https://data.lemmyverse.net/data/community.full.json',)
    assert fetch.call_args_list[0].kwargs == {'max_bytes': LEMMYVERSE_MAX_BYTES,
                                              'max_seconds': LEMMYVERSE_MAX_SECONDS, 'retry': True}
    # The disposable-email domains.txt was 1,504,489 bytes on 2026-10-11.
    assert fetch.call_args_list[1].kwargs == {'max_bytes': DOMAIN_LIST_MAX_BYTES,
                                              'max_seconds': DOMAIN_LIST_MAX_SECONDS, 'retry': True}


def test_a_feed_and_its_redirect_are_fetched_under_the_media_cap(commands):
    instance = make_instance('test.piefed.local', software='piefed')
    make_site()
    make_user(instance, 'feed_bot', local=True, with_keys=True)
    community = make_community('news')
    db.session.add(RssFeed(community_id=community.id, title='feed', url='https://feeds.example/rss',
                           check_frequency=60, next_check=utcnow() - timedelta(minutes=1)))
    db.session.commit()
    fetch = Mock(side_effect=[Mock(status_code=302, headers={'Location': 'https://feeds.example/moved'}),
                              Mock(status_code=404)])

    with patch('app.cli.get_request', fetch):
        result = commands.invoke(args=['rss-feeds'])

    assert result.exception is None, result.exception
    assert [call.args for call in fetch.call_args_list] == [('https://feeds.example/rss',),
                                                            ('https://feeds.example/moved',)]
    assert all(call.kwargs == {'max_bytes': MEDIA_MAX_BYTES, 'max_seconds': MEDIA_MAX_SECONDS, 'retry': True}
               for call in fetch.call_args_list)
    assert (MEDIA_MAX_BYTES, MEDIA_MAX_SECONDS) == (10 * 1024 * 1024, 30)


def test_init_db_stores_the_block_lists_hosts_in_one_spelling(commands):
    """init-db adds the No-QAnon and Peertube Isolation lists as Domain and BannedInstances rows. It stored each
    line as given, so an IPv6 literal written '[0:0::1]' was a row no lookup reads: instance_banned and the Domain
    lookups compare canonical_host's spelling (compressed, unbracketed; owner ruling 2026-10-14). Everything that
    would touch the real schema -- drop_all, create_all, the session -- is a stand-in here; what is checked is what
    the command adds."""
    from unittest.mock import MagicMock

    from app.models import BannedInstances, Domain

    fake_db = MagicMock()
    fake_db.inspect.return_value.get_table_names.return_value = ['alembic_version']
    fake_db.session.execute.return_value.fetchone.return_value = ('UTF8',)
    with patch('app.cli.db', fake_db), \
            patch('app.cli.retrieve_block_list', return_value='[0:0::1]\nbad.example\n'), \
            patch('app.cli.retrieve_peertube_block_list', return_value='[2001:db8:0::5]\ntube.example'):
        result = commands.invoke(args=['init-db', '--interactive', 'no'])

    assert result.exception is None, result.exception
    added = [call.args[0] for call in fake_db.session.add.call_args_list]
    assert [row.name for row in added if isinstance(row, Domain)] == ['::1', 'bad.example', '', '2001:db8::5',
                                                                      'tube.example']
    assert [row.domain for row in added if isinstance(row, BannedInstances)][-2:] == ['2001:db8::5', 'tube.example']
