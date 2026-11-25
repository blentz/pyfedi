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
# What may follow a link that stands on its own in the text: a space, the end, or a full stop and the like before one
_AFTER_ALONE = re.compile(r'(\s|$|[.,;:!?](\s|$))')
LINKS = ('url', 'secret')   # placeholder kinds that are links


def secret_fits(text: str, start: int, end: int) -> bool:
    """Whether a secret link (one that carries a token, D23) at text[start:end] stands as a link of its own: on its own
    between spaces, or the whole target of a Markdown link that is not an image. Anywhere else it could be sent to
    somebody else's server, inside their URL or in an image's address."""
    before, after = text[:start], text[end:]
    if before.endswith('](') and after.startswith(')'):
        opening = before[:-2].rfind('[')
        return opening >= 0 and not before[:opening].endswith('!')
    return (not before or before[-1].isspace()) and _AFTER_ALONE.match(after) is not None


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
    secrets = {p.name for p in placeholders if p.kind == 'secret'}
    found += [f'{{{name}}} carries a sign-in token, so it cannot be in the subject'
              for name in sorted(secrets & placeholders_in(subject))]
    misplaced = sorted({match.group(1) for match in _TOKEN.finditer(body or '') if match.group(1) in secrets
                        and not secret_fits(body, match.start(), match.end())})
    found += [f'{{{name}}} carries a sign-in token, so it can only be a link of its own: on its own, or as '
              f'[some words]({{{name}}})' for name in misplaced]
    return found


def fill_text(text: str, values: dict, kinds: dict | None = None) -> str:
    """The subject or the text part: values as they are. A secret link anywhere but as a link of its own is left out
    (D23): what saving refuses, kept out of a row written some other way too."""
    kinds = kinds or {}

    def one(match):
        if match.group(1) is None:
            return match.group(0)[0]
        value = values.get(match.group(1))
        if value is None:
            return match.group(0)
        if kinds.get(match.group(1)) == 'secret' and not secret_fits(text, match.start(), match.end()):
            return ''
        return value.text
    return _TOKEN.sub(one, text)


def _escaped(values: dict, name: str, original: str) -> str:
    value = values.get(name)
    if value is None:
        return original
    return value.html if value.html is not None else html.escape(value.text, quote=True)


def _in_tag(chunk: str, values: dict, kinds: dict) -> str:
    """A tag with values put in its attributes. A secret link only as the whole href of a link, and nowhere else in a
    tag: not in an image's address, not inside another URL (D23)."""
    anchor = _ANCHOR_OPEN.match(chunk) is not None

    def href(match):
        name = match.group(2) or match.group(3)
        if anchor and kinds.get(name) == 'secret' and name in values:
            return f'href="{_escaped(values, name, match.group(0))}"'
        return match.group(0)
    chunk = re.sub(r'href="(\{([a-z_]+)\}|%7B([a-z_]+)%7D)"', href, chunk, flags=re.IGNORECASE)

    def one(match):
        name = match.group(1)
        if name is None:
            return match.group(0)[0]
        if kinds.get(name) == 'secret' and name in values:
            return ''
        return _escaped(values, name, match.group(0))
    chunk = _ENCODED.sub(one, chunk)
    return _TOKEN.sub(one, chunk)


def fill_html(rendered: str, values: dict, kinds: dict) -> str:
    """Rendered HTML with values put in (D7): escaped in a tag's attributes and in text; a link on its own in text
    becomes a link to itself, one already inside a link stays text."""
    out, in_anchor = [], False
    for chunk in _TAG.split(rendered):
        if chunk.startswith('<') and chunk.endswith('>'):
            if _ANCHOR_OPEN.match(chunk):
                in_anchor = True
            elif _ANCHOR_CLOSE.match(chunk):
                in_anchor = False
            out.append(_in_tag(chunk, values, kinds))
            continue

        def one(match, in_anchor=in_anchor):
            if match.group(1) is None:
                return match.group(0)[0]
            name = match.group(1)
            escaped = _escaped(values, name, match.group(0))
            if kinds.get(name) in LINKS and not in_anchor and name in values:
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
    content_text = fill_text(body, values, kinds)
    text_body = render_template('email/override.txt', content=content_text, unsubscribe_url=unsubscribe_url,
                                unsubscribe_label=unsubscribe_label)
    html_body = render_template('email/override.html', content=content_html, unsubscribe_url=unsubscribe_url,
                                unsubscribe_label=unsubscribe_label)
    # never a secret link in the subject (D23), which saving refuses too
    in_subject = {name: value for name, value in values.items() if kinds.get(name) != 'secret'}
    return Mail(fill_text(subject, in_subject).replace('\n', ' ').strip(), text_body, html_body)


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
