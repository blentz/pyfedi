"""`flask cms export` and `flask cms import`: CMS pages as JSON, for keeping them in files outside the database.

Import stores each page as the admin editor would (canonicalised Markdown, the editor's HTML), creates or updates by
URL, never deletes, and checks every page before storing any. Both print every page as it now stands."""
import json

import pytest

from app import cli, db
from app.cms import cms_nav_links
from app.models import CmsPage
from app.utils import markdown_to_html

FLAGS = {'in_header': False, 'in_footer': False, 'show_sidebar': False, 'members_only': False}


def page(url='/rules', title='Rules', body='Be **kind**.', **flags):
    return {'url': url, 'title': title, 'body': body, **FLAGS, **flags}


@pytest.fixture
def run(app, site):
    cli.register(app)
    runner = app.test_cli_runner()

    def invoke(*args, input=None):
        return runner.invoke(args=['cms', *args], input=input)
    return invoke


def exported(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.output)['pages']


def without_times(pages):
    return [{key: value for key, value in found.items() if key != 'edited_at'} for found in pages]


def stored(url):
    return db.session.query(CmsPage).filter_by(url=url).one()


class TestExport:
    def test_no_pages(self, run):
        assert exported(run('export')) == []

    def test_every_page_sorted_by_url_with_its_flags_and_last_edit(self, run):
        db.session.add_all([CmsPage(url='/b', title='B', body='bee', body_html='<p>bee</p>', in_footer=True),
                            CmsPage(url='/a', title='A', body='ay', body_html='<p>ay</p>', members_only=True)])
        db.session.commit()
        pages = exported(run('export'))
        assert without_times(pages) == [page('/a', 'A', 'ay', members_only=True), page('/b', 'B', 'bee', in_footer=True)]
        assert all(isinstance(found['edited_at'], str) for found in pages)


class TestImport:
    def test_a_new_page_is_stored_as_the_editor_stores_it(self, app, run):
        result = run('import', input=json.dumps({'pages': [page(in_header=True)]}))
        assert without_times(exported(result)) == [page(in_header=True)]
        assert json.loads(result.output)['changed'] == ['/rules']
        row = stored('/rules')
        with app.test_request_context():
            assert row.body_html == markdown_to_html('Be **kind**.', a_target='')
        assert row.last_edited_by == 'cms import'
        assert cms_nav_links() == {'header': [{'url': '/rules', 'title': 'Rules'}], 'footer': []}

    def test_an_existing_page_is_updated_by_its_url(self, run):
        db.session.add(CmsPage(url='/rules', title='Old', body='old', body_html='<p>old</p>', in_footer=True))
        db.session.commit()
        run('import', '--editor', 'files', input=json.dumps({'pages': [page(title='New', body='new')]}))
        row = stored('/rules')
        assert (row.title, row.body, row.in_footer, row.last_edited_by) == ('New', 'new', False, 'files')
        assert '<p>new</p>' in row.body_html

    def test_an_unchanged_page_is_left_as_it_was(self, run):
        run('import', input=json.dumps({'pages': [page()]}))
        before = stored('/rules').edited_at
        result = run('import', '--editor', 'someone else', input=json.dumps({'pages': [page()]}))
        assert json.loads(result.output)['changed'] == []
        row = stored('/rules')
        assert (row.edited_at, row.last_edited_by) == (before, 'cms import')

    def test_pages_it_was_not_given_stay(self, run):
        db.session.add(CmsPage(url='/about', title='About', body='us', body_html='<p>us</p>'))
        db.session.commit()
        pages = exported(run('import', input=json.dumps({'pages': [page()]})))
        assert [found['url'] for found in pages] == ['/about', '/rules']

    @pytest.mark.parametrize('bad, reason', [
        ('not json', 'not JSON'),
        ('[]', '"pages"'),
        (json.dumps({'pages': [page(url='rules')]}), 'url'),
        (json.dumps({'pages': [page(url='//elsewhere.example/x')]}), 'url'),
        (json.dumps({'pages': [page(url='/a\\b')]}), 'url'),
        (json.dumps({'pages': [page(url='/' + 'a' * 100)]}), 'url'),
        (json.dumps({'pages': [page(title='')]}), 'title'),
        (json.dumps({'pages': [page(title='t' * 256)]}), 'title'),
        (json.dumps({'pages': [page(body='')]}), 'body'),
        (json.dumps({'pages': [page(in_header='yes')]}), 'in_header'),
        (json.dumps({'pages': [{'url': '/x', 'title': 'X', 'body': 'x'}]}), 'in_header'),
        (json.dumps({'pages': [page(), page()]}), 'twice'),
        (json.dumps({'pages': ['a page']}), 'object'),
    ])
    def test_anything_wrong_stores_nothing_and_says_what(self, run, bad, reason):
        good_first = bad.replace('{"pages": [', '{"pages": [' + json.dumps(page('/first')) + ', ', 1)
        result = run('import', input=good_first)
        assert result.exit_code == 1 and reason in result.output
        assert db.session.query(CmsPage).count() == 0
