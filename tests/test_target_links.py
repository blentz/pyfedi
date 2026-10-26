"""A name stored in a notification's or report's `targets` links by id, so it reaches the account it names on whichever
domain the viewer is on (follow-up F1).

`/u/bob` on the onion is the onion's own `bob`, a namesake of the first domain's. A new row stores the id beside the
name and links to `/user/<id>`; a community to `/community/<id>`. A row stored before has the name alone: a bare name
is the first domain's, `name@domain` is that domain's, and a name that finds nothing is shown as text, never as the
ambiguous `/u/<name>`."""
import pathlib
import re
from datetime import datetime
from types import SimpleNamespace

import pytest
from flask import render_template

from app import db
from app.local_names import stored_name_id, target_href, target_link
from app.models import Community, User
from tests.factories import make_instance, make_user
from tests.two_domains import default_request, onion_request, two_domains  # noqa: F401  (the fixture)

pytestmark = pytest.mark.usefixtures('site')

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / 'app' / 'templates'


@pytest.fixture
def bobs(two_domains):
    """`bob` on the first domain and a namesake `bob` on the onion; likewise `alpha`."""
    first, onion = two_domains.domains
    return SimpleNamespace(
        first_bob=two_domains.local_user('bob', first), onion_bob=two_domains.local_user('bob', onion),
        first_alpha=two_domains.local_community('alpha', first), onion_alpha=two_domains.local_community('alpha', onion))


def test_on_the_onion_a_new_row_links_to_the_first_domains_account_by_id(app, bobs):
    targets = {'suspect_user_user_name': 'bob', 'suspect_user_id': bobs.first_bob.id}
    with onion_request(app):
        assert target_href(targets, 'suspect_user_user_name') == f'/user/{bobs.first_bob.id}'


def test_a_report_rendered_on_the_onion_links_to_the_first_domains_account(app, bobs):
    """The report an admin on the onion reads, through the real template."""
    report = SimpleNamespace(
        id=1, reasons='spam', description='', suspect_user_id=bobs.first_bob.id, is_local=lambda *a, **k: True, created_at=datetime(2026, 1, 1), updated=datetime(2026, 1, 1),
        targets={'suspect_user_user_name': 'bob', 'suspect_user_id': bobs.first_bob.id,
                 'reporter_user_name': 'bob@abc.onion', 'reporter_id': bobs.onion_bob.id})
    with onion_request(app):
        html = render_template('admin/reports/user_report.html', report=report)
    assert f'href="/user/{bobs.first_bob.id}"' in html
    assert f'href="/user/{bobs.onion_bob.id}"' in html
    assert 'href="/u/' not in html


def test_a_notification_rendered_on_the_onion_links_to_the_first_domains_account_and_community(app, bobs):
    notification = SimpleNamespace(
        id=1, read=False, created_at=None, subtype=None,
        targets={'author_user_name': 'bob', 'author_id': bobs.first_bob.id, 'community_name': 'alpha',
                 'community_id': bobs.first_alpha.id, 'post_id': 1, 'post_title': 'a post'})
    with onion_request(app):
        html = render_template('user/notifs/0.html', notification=notification, localize_datetime=lambda *a: '',
                               locale='en')
    assert f'href="/user/{bobs.first_bob.id}"' in html
    assert f'href="/community/{bobs.first_alpha.id}"' in html


def test_an_old_row_with_a_bare_name_links_to_the_first_domains_account(app, bobs):
    with onion_request(app):
        assert target_href({'suspect_user_user_name': 'bob'}, 'suspect_user_user_name') == f'/user/{bobs.first_bob.id}'
    with default_request(app):
        assert target_href({'suspect_user_user_name': 'bob'}, 'suspect_user_user_name') == f'/user/{bobs.first_bob.id}'


def test_an_old_row_with_a_domain_links_to_that_domains_account(app, bobs):
    with default_request(app):
        assert target_href({'author_user_name': 'bob@abc.onion'}, 'author_user_name') == f'/user/{bobs.onion_bob.id}'


def test_an_old_row_with_a_bare_community_name_links_to_the_first_domains_community(app, bobs):
    with onion_request(app):
        assert target_href({'community_name': 'alpha'}, 'community_name') == f'/community/{bobs.first_alpha.id}'
        assert target_href({'community_name': 'alpha@abc.onion'}, 'community_name') == \
            f'/community/{bobs.onion_alpha.id}'


def test_an_old_row_with_a_remote_name_links_to_the_remote_account(app, bobs):
    remote = make_user(make_instance('remote.example'), 'carol')
    assert remote.ap_id == 'carol@remote.example'
    with onion_request(app):
        assert target_href({'author_user_name': 'carol@remote.example'}, 'author_user_name') == f'/user/{remote.id}'


def test_an_old_row_whose_name_matches_nothing_is_not_linked(app, bobs):
    with onion_request(app):
        assert target_href({'suspect_user_user_name': 'ghost'}, 'suspect_user_user_name') is None
        assert target_href({'author_user_name': 'ghost@remote.example'}, 'author_user_name') is None
        assert target_href({'community_name': 'ghost'}, 'community_name') is None
        assert str(target_link({'suspect_user_user_name': 'ghost'}, 'suspect_user_user_name', 'x')) == 'ghost'


def test_a_report_rendered_with_an_unknown_name_shows_text_and_no_link(app, bobs):
    report = SimpleNamespace(id=1, reasons='spam', description='', suspect_user_id=None, is_local=lambda *a, **k: True, created_at=datetime(2026, 1, 1), updated=datetime(2026, 1, 1),
                             targets={'suspect_user_user_name': 'ghost', 'reporter_user_name': 'ghost'})
    with onion_request(app):
        html = render_template('admin/reports/user_report.html', report=report)
    assert 'ghost' in html
    assert 'href="/u/' not in html and 'href="/user/' not in html


def test_a_missing_or_empty_name_is_not_linked_and_shows_nothing(app, bobs):
    with onion_request(app):
        assert target_href({}, 'author_user_name') is None
        assert target_href(None, 'author_user_name') is None
        assert target_href({'author_user_name': '', 'author_id': bobs.first_bob.id}, 'author_user_name') is None
        assert str(target_link({}, 'author_user_name')) == ''
        assert str(target_link(None, 'author_user_name')) == ''


def test_an_id_that_is_not_an_integer_falls_back_to_the_name(app, bobs):
    targets = {'author_user_name': 'bob', 'author_id': 'bob; drop table'}
    with onion_request(app):
        assert target_href(targets, 'author_user_name') == f'/user/{bobs.first_bob.id}'


def test_the_link_carries_its_class_and_escapes_the_name(app, bobs):
    targets = {'author_user_name': 'bob', 'author_id': bobs.first_bob.id}
    with onion_request(app):
        assert str(target_link(targets, 'author_user_name', 'border border-0')) == \
            f'<a class="border border-0" href="/user/{bobs.first_bob.id}">bob</a>'
        assert str(target_link(targets, 'author_user_name')) == f'<a href="/user/{bobs.first_bob.id}">bob</a>'
        assert str(target_link({'author_user_name': '<b>x</b>'}, 'author_user_name')) == '&lt;b&gt;x&lt;/b&gt;'


def test_stored_name_id_finds_a_user_or_a_community(app, bobs):
    with default_request(app):
        assert stored_name_id(User, 'bob') == bobs.first_bob.id
        assert stored_name_id(Community, 'alpha@abc.onion') == bobs.onion_alpha.id
        assert stored_name_id(Community, 'nothing') is None


def test_no_template_builds_a_path_from_a_stored_name():
    """Every href made from a `targets` name goes through `target_link`: a relative `/u/` or `/c/` path built from a
    stored name is the namesake bug."""
    pattern = re.compile(r'href="/(?:u|c)/\{\{[^}]*targets')
    offenders = [f'{p.relative_to(TEMPLATES)}:{n}' for p in sorted(TEMPLATES.rglob('*.html'))
                 for n, line in enumerate(p.read_text().splitlines(), 1) if pattern.search(line)]
    assert offenders == []


def test_an_automated_reporter_is_text_not_a_link_to_user_one(app, bobs):
    """The automated reports store `reporter_id: 1` and the name `automated`."""
    targets = {'reporter_user_name': 'automated', 'reporter_id': 1}
    with onion_request(app):
        assert target_href(targets, 'reporter_user_name') is None
        assert str(target_link(targets, 'reporter_user_name')) == 'automated'
