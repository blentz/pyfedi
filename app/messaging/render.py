"""An admin's text for an email, filled in for one recipient (spec 2026-11-24-admin-messaging D5-D9)."""
import html
import re
from typing import NamedTuple

from flask import render_template

from app import db
from app.messaging.catalog import BY_KEY, Placeholder
from app.models import EmailTemplate
from app.storefront import storefront_html
from app.utils import markdown_to_html

# `{{` and `}}` are literal braces (D6); `{name}` is a placeholder. One pattern, so a scan left to right sees `{{name}}`
# as two literal braces round a word, not as a placeholder.
_TOKEN = re.compile(r'\{\{|\}\}|\{([a-z_]+)\}')
# What Markdown leaves of a placeholder it percent-encoded inside a link (D7).
_ENCODED = re.compile(r'%7B([a-z_]+)%7D', re.IGNORECASE)
# HTML as tags and the text between them, to tell an attribute's value from text (D7).
_TAG = re.compile(r'(<[^>]*>)')
_ANCHOR_OPEN = re.compile(r'<a[\s>]', re.IGNORECASE)
_ANCHOR_CLOSE = re.compile(r'</a\s*>', re.IGNORECASE)


class Mail(NamedTuple):
    subject: str
    text_body: str
    html_body: str


class Value(NamedTuple):
    """A placeholder's value: `text` for the text part and the subject, `html` (trusted, made by code) for a list's
    HTML part. Anything else is escaped into the HTML part."""
    text: str
    html: str | None = None


def placeholders_in(text: str) -> set:
    return {match.group(1) for match in _TOKEN.finditer(text or '') if match.group(1)}


def problems(placeholders: tuple, subject: str, body: str) -> list:
    """Why this text cannot be saved (D6): placeholders it may not use, and required ones it leaves out."""
    allowed = {p.name for p in placeholders}
    used = placeholders_in(subject) | placeholders_in(body)
    found = [f'{{{name}}} is not a placeholder this email has' for name in sorted(used - allowed)]
    found += [f'{{{p.name}}} has to be in the text' for p in placeholders if p.required and p.name not in used]
    found += [f'{{{p.name}}} is a list, which cannot be in the subject'
              for p in placeholders if p.kind == 'list' and p.name in placeholders_in(subject)]
    return found


def fill_text(text: str, values: dict) -> str:
    """The subject or the text part: values as they are."""
    def one(match):
        if match.group(1) is None:
            return match.group(0)[0]
        value = values.get(match.group(1))
        return value.text if value is not None else match.group(0)
    return _TOKEN.sub(one, text)


def _escaped(values: dict, name: str, original: str) -> str:
    value = values.get(name)
    if value is None:
        return original
    return value.html if value.html is not None else html.escape(value.text, quote=True)


def fill_html(rendered: str, values: dict, kinds: dict) -> str:
    """Rendered HTML with values put in (D7): escaped in a tag's attributes and in text; a url on its own in text
    becomes a link to itself, one already inside a link stays text."""
    out, in_anchor = [], False
    for chunk in _TAG.split(rendered):
        if chunk.startswith('<') and chunk.endswith('>'):
            if _ANCHOR_OPEN.match(chunk):
                in_anchor = True
            elif _ANCHOR_CLOSE.match(chunk):
                in_anchor = False
            chunk = _ENCODED.sub(lambda m: _escaped(values, m.group(1), m.group(0)), chunk)
            out.append(_TOKEN.sub(lambda m: m.group(0)[0] if m.group(1) is None
                                  else _escaped(values, m.group(1), m.group(0)), chunk))
            continue

        def one(match, in_anchor=in_anchor):
            if match.group(1) is None:
                return match.group(0)[0]
            name = match.group(1)
            escaped = _escaped(values, name, match.group(0))
            if kinds.get(name) == 'url' and not in_anchor and name in values:
                return f'<a href="{escaped}">{escaped}</a>'
            return escaped
        out.append(_TOKEN.sub(one, chunk))
    return ''.join(out)


def compose(subject: str, body: str, placeholders: tuple, values: dict, unsubscribe_url: str | None = None,
            unsubscribe_label: str | None = None) -> Mail:
    """An admin's subject and Markdown body for one recipient, in the layout round every such email (D9). Call it
    under the recipient's storefront: links move onto their domain."""
    kinds = {p.name: p.kind for p in placeholders}
    allowed = {p.name for p in placeholders}
    values = {name: value for name, value in values.items() if name in allowed}
    # The HTML's own braces are rendered first and filled after, so a value is never read as Markdown (D7).
    content_html = fill_html(storefront_html(markdown_to_html(body)) or '', values, kinds)
    content_text = fill_text(body, values)
    text_body = render_template('email/override.txt', content=content_text, unsubscribe_url=unsubscribe_url,
                                unsubscribe_label=unsubscribe_label)
    html_body = render_template('email/override.html', content=content_html, unsubscribe_url=unsubscribe_url,
                                unsubscribe_label=unsubscribe_label)
    return Mail(fill_text(subject, values).replace('\n', ' ').strip(), text_body, html_body)


def override(key: str) -> EmailTemplate | None:
    return db.session.get(EmailTemplate, key)


def overridden(key: str, values: dict, unsubscribe_url: str | None = None,
               unsubscribe_label: str | None = None) -> Mail | None:
    """The email `key` in the admin's words for one recipient, or None to send the bundled one (D3)."""
    row = override(key)
    if row is None:
        return None
    return compose(row.subject, row.body, BY_KEY[key].placeholders, values, unsubscribe_url, unsubscribe_label)


def sample_values(placeholders: tuple) -> dict:
    """Values for a preview: each placeholder's sample, lists as a short list."""
    def value(p: Placeholder) -> Value:
        if p.kind == 'list':
            items = [line.lstrip('- ') for line in p.sample.splitlines()]
            return Value(p.sample, '<ul>' + ''.join(f'<li>{html.escape(i)}</li>' for i in items) + '</ul>')
        return Value(p.sample)
    return {p.name: value(p) for p in placeholders}
