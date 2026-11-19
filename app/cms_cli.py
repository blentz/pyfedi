"""`flask cms export` and `flask cms import`: the CMS pages as JSON, so a site can keep them in files.

The JSON is `{"pages": [{"url", "title", "body", "in_header", "in_footer", "show_sidebar", "members_only"}, ...]}`;
export adds each page's `edited_at`. Import creates or updates pages by URL and never deletes one. It checks every page
before storing any, and stores each as the admin editor does: canonicalised Markdown and the editor's HTML."""
import json
import sys

import click
from flask import current_app

from app import db
from app.cms import cms_nav_links_changed
from app.models import CmsPage, utcnow
from app.storefront import canonicalise_text
from app.utils import markdown_to_html

FLAGS = ('in_header', 'in_footer', 'show_sidebar', 'members_only')
URL_MAX = CmsPage.url.type.length
TITLE_MAX = CmsPage.title.type.length


class PagesError(ValueError):
    """Pages that cannot be imported; the message says which and why."""


def page_json(page: CmsPage) -> dict:
    return {'url': page.url, 'title': page.title, 'body': page.body or '',
            **{flag: bool(getattr(page, flag)) for flag in FLAGS},
            'edited_at': page.edited_at.isoformat() if page.edited_at else None}


def export_pages() -> dict:
    return {'pages': [page_json(page) for page in db.session.query(CmsPage).order_by(CmsPage.url)]}


def check_pages(text: str) -> list[dict]:
    """The pages `text` holds, each checked as the admin form checks a page; PagesError otherwise."""
    try:
        data = json.loads(text)
    except ValueError:
        raise PagesError('The input is not JSON.') from None
    if not isinstance(data, dict) or not isinstance(data.get('pages'), list):
        raise PagesError('The input must be an object with a "pages" list.')
    pages, seen = [], set()
    for number, item in enumerate(data['pages'], 1):
        if not isinstance(item, dict):
            raise PagesError(f'Page {number} is not an object.')
        url = item.get('url')
        if not isinstance(url, str) or not url.startswith('/') or url.startswith('//') or '\\' in url \
                or len(url) > URL_MAX:
            raise PagesError(f'Page {number}: url must be a path on this site, at most {URL_MAX} characters.')
        if url in seen:
            raise PagesError(f'{url}: given twice.')
        seen.add(url)
        title = item.get('title')
        if not isinstance(title, str) or not title.strip() or len(title) > TITLE_MAX:
            raise PagesError(f'{url}: title must be text, 1 to {TITLE_MAX} characters.')
        if not isinstance(item.get('body'), str) or not item['body'].strip():
            raise PagesError(f'{url}: body must be text.')
        for flag in FLAGS:
            if not isinstance(item.get(flag), bool):
                raise PagesError(f'{url}: {flag} must be true or false.')
        pages.append({key: item[key] for key in ('url', 'title', 'body', *FLAGS)})
    return pages


def import_pages(pages: list[dict], editor: str) -> list[str]:
    """Store `pages` (checked); the URLs of those that changed. A page already as given is left alone."""
    changed = []
    with current_app.test_request_context():   # the Markdown renderer reads the request's site
        for item in pages:
            body = canonicalise_text(item['body'])
            row = db.session.query(CmsPage).filter_by(url=item['url']).first()
            if row is not None and (row.title, row.body) == (item['title'], body) and \
                    all(getattr(row, flag) == item[flag] for flag in FLAGS):
                continue
            if row is None:
                row = CmsPage(url=item['url'])
                db.session.add(row)
            row.title, row.body = item['title'], body
            row.body_html = markdown_to_html(body, a_target='')
            for flag in FLAGS:
                setattr(row, flag, item[flag])
            row.last_edited_by, row.edited_at = editor, utcnow()
            changed.append(item['url'])
    db.session.commit()
    if changed:
        cms_nav_links_changed()
    return changed


def register_cms_commands(app) -> None:
    @app.cli.group('cms')
    def cms():
        """CMS pages as JSON, for keeping them in files."""

    @cms.command('export')
    def export():
        """Print every CMS page as JSON."""
        click.echo(json.dumps(export_pages(), indent=1, ensure_ascii=False))

    @cms.command('import')
    @click.option('--editor', default='cms import', show_default=True, help='Recorded as the last editor of a change')
    def import_(editor):
        """Create or update CMS pages from JSON on standard input, then print every page as JSON, with the URLs of
        those that changed as "changed"."""
        try:
            pages = check_pages(sys.stdin.read())
        except PagesError as error:
            click.echo(str(error))
            sys.exit(1)
        changed = import_pages(pages, editor[:50])
        click.echo(json.dumps({**export_pages(), 'changed': changed}, indent=1, ensure_ascii=False))
