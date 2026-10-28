"""Pure mapping between FediTest's view of a PieFed account or note and PieFed's own; no I/O, so the unit tests
(tests/test_piefed_mapping.py) run without the harness. Stdlib only: the conformance runner image has no bs4."""
import re
from html.parser import HTMLParser

TITLE_LIMIT = 50   # a microblog post has no title of its own, but PieFed's form requires one


def acct_to_userid(acct_uri: str, host: str = 'piefed.test') -> str:
    """'acct:alice@piefed.test' -> 'alice'; ValueError for an account that is not on `host`."""
    scheme, _, rest = acct_uri.partition(':')
    userid, _, account_host = rest.rpartition('@')
    if scheme != 'acct' or not userid or account_host != host:
        raise ValueError(f'not an acct: URI on {host}: {acct_uri}')
    return userid


def microblog_title(content: str) -> str:
    """The title PieFed requires for a post made of `content`: its first 50 characters, cut back to whole words, so the
    body starts with the title as whole words and PieFed federates the body alone (note_content). One word past the limit is cut."""
    content = content.strip()
    if len(content) <= TITLE_LIMIT:
        return content
    cut = TITLE_LIMIT
    while cut > 0 and content[cut].isalnum():
        cut -= 1
    return content[:cut].strip() or content[:TITLE_LIMIT]


class _Paragraphs(HTMLParser):
    """The text of each <p>, with a link shown as its address (so a posted URL reads back as posted) and <br> as a newline."""

    def __init__(self):
        super().__init__()
        self.paragraphs, self._open, self._in_link = [], False, False

    def handle_starttag(self, tag, attrs):
        if tag == 'p':
            self.paragraphs.append('')
            self._open = True
        elif tag == 'br' and self._open:
            self.paragraphs[-1] += '\n'
        elif tag == 'a' and self._open and dict(attrs).get('href'):
            self.paragraphs[-1] += dict(attrs)['href']
            self._in_link = True    # the link's own text is dropped: it can be shortened, its address cannot

    def handle_endtag(self, tag):
        if tag == 'p':
            self._open = False
        elif tag == 'a':
            self._in_link = False

    def handle_data(self, data):
        if self._open and not self._in_link:
            self.paragraphs[-1] += data


def note_text(html: str, title: str) -> str:
    """The text that was posted, from the Note content PieFed federates for it: the paragraphs joined by a blank line,
    without the title paragraph PieFed puts in front when the body does not start with the title as whole words
    (app/activitypub/serializers/microblog.py note_content). A lone paragraph is never the title prefix."""
    parser = _Paragraphs()
    parser.feed(html)
    paragraphs = parser.paragraphs
    if title and len(paragraphs) > 1 and paragraphs[0] == title:
        paragraphs = paragraphs[1:]
    return '\n\n'.join(paragraphs)


def received_ap_id(rows: list[tuple]) -> str | None:
    """The ap_id of the first row of an ap_id-first query, None when PieFed has no such object (an unknown object is an answer, not an error)."""
    return rows[0][0] if rows else None


def libpq_dsn(database_url: str) -> str:
    """DATABASE_URL names a SQLAlchemy driver ('postgresql+psycopg2://'); psycopg2.connect wants plain 'postgresql://'."""
    return re.sub(r'^postgresql\+\w+://', 'postgresql://', database_url)
