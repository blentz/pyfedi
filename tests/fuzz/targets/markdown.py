"""Property checks for markdown rendering, HTML text helpers and the regexes they run (tier T3).

Every post, comment, profile and community description a peer or a user writes goes through this code before it
is stored or rendered: markdown_to_html and its preprocessors, the jinja filters that turn @user@host, !c@host and
~f@host into links, the helpers that cut a microblog post into a title and a link, the posting-warning and
first-paragraph sanitisers, the enhanced-images markdown extra, and the URL reader in front of podcast credits.
Each check asserts the contract the function documents -- the allowlist property on HTML out, that code spans
survive the stash/pop round trip, that a link is never created inside another link or a code span, length caps,
that only the documented markup is added -- and a raise on any input is a finding.

The regex targets (redos_*, and every target with a budget_ms) are the ReDoS gate: their committed seeds include a
pump input built for each regex's backtracking shape, at the 64 KiB a federated post can reach (remote markdown and
HTML bodies are not length-capped on ingest; the local post form caps at 50,000 characters), and the corpus replay
fails an input that runs past the budget. Inputs are cut at MAX_INPUT so a campaign's -max_len cannot outgrow that.

No database and no network. allowlist_html and markdown_to_html get test_env, which keeps them off the DB-backed
fediverse_domains() and get_emoji_replacements(); first_paragraph and sanitise_posting_warning have no test_env, so
those two are swapped out for empty answers for the duration (ap_json._no_db_lookups). The link filters get
server_name_override, so none needs an app context. Multi-argument inputs use signatures._fields.
"""

import re
from urllib.parse import urlsplit

import markdown2
from bs4 import BeautifulSoup
from bs4.element import PreformattedString
from furl import furl

from app.activitypub.util import castopod_episode_url
from app.discovery.credits import _document_urls, _normal_url, _urls_in
from app.discovery.sources import is_hostname
from app.instance.util import is_fedi_handle
from app.markdown_extras import EnhancedImages, apply_enhanced_image_attributes
from app.utils import (COMMUNITY_PATTERN, FEED_PATTERN, LINK_PATTERN, PERSON_PATTERN, allowlist_html,
                       community_link_to_href, escape_img, escape_non_html_angle_brackets, feed_link_to_href,
                       first_paragraph, fix_www_links, handle_better_lists, handle_blockquotes, handle_bold_em,
                       handle_lemmy_autocomplete, handle_lemmy_spoilers, handle_naked_spoilers, handle_reddit_spoilers,
                       handle_spoiler_spacing, handle_video_embeds, html_to_text, links_with_parens, make_quotes_straight,
                       markdown_to_html, markdown_to_text, mastodon_extra_field_link, microblog_content_to_link,
                       microblog_content_to_title, person_link_to_href, piefed_markdown_to_lemmy_markdown, pop_code,
                       pop_link, remove_images, sanitise_posting_warning, shorten_string, shorten_url,
                       stash_code_html, stash_code_md, stash_link_html, strip_quote_fallback, validate_email)
from app.visibility import MENTION_PATTERN
from tests.fuzz.targets._target import Target
from tests.fuzz.targets.ap_json import _no_db_lookups
from tests.fuzz.targets.sanitizers import assert_allowlisted
from tests.fuzz.targets.signatures import _fields, _json_or_text, _text

# The largest input a check reads: 64 KiB, the size a federated body reaches with room to spare.
MAX_INPUT = 64 * 1024
TEST_ENV = {'fn_string': 'fn-test'}
SERVER = 'fuzz-server.example'
# A stash placeholder the checks choose, so a round trip can be compared; an input carrying it is skipped.
PLACEHOLDER = 'Qq7FUZZSTASH7qQ'


def _input_text(data: bytes) -> str:
    return _text(data[:MAX_INPUT])


def _code_spans_md(text: str) -> list[str]:
    """The fenced and inline code spans stash_code_md lifts out of markdown, read without its regexes: a fenced
    block is the leftmost ``` and the first ``` after it; inline code is a backtick, one or more characters that
    are neither a backtick nor a newline, and a backtick, in what the fenced blocks leave -- where an inline span
    can hold a fenced block, which is then part of it. Linear, so a pump seed times the function under test and
    not this check. The caller has made sure PLACEHOLDER is not in `text`."""
    fenced, rest, position = [], [], 0
    while True:
        start = text.find('```', position)
        end = text.find('```', start + 3) if start != -1 else -1
        if end == -1:
            rest.append(text[position:])
            break
        fenced.append(text[start:end + 3])
        rest.append(text[position:start] + f'{PLACEHOLDER}{len(fenced) - 1}$')
        position = end + 3
    marker = re.compile(re.escape(PLACEHOLDER) + r'([0-9]+)\$')
    inline = [marker.sub(lambda m: fenced[int(m.group(1))], span) for span in re.findall(r'`[^`\n]+`', ''.join(rest))]
    return fenced + inline


def _code_spans_html(text: str) -> list[str]:
    """<code>...</code> spans as stash_code_html lifts them: the leftmost <code> and the first </code> after it."""
    spans, position = [], 0
    while True:
        start = text.find('<code>', position)
        end = text.find('</code>', start + 6) if start != -1 else -1
        if end == -1:
            return spans
        spans.append(text[start:end + 7])
        position = end + 7


def _added(before: str, after: str, token: str) -> int:
    return after.count(token) - before.count(token)


def _only_escapes_brackets(source: str, result: str) -> bool:
    """Whether `result` is `source` with some '<' turned into '&lt;' and some '>' into '&gt;', and nothing else.
    Unambiguous read left to right: where the source has '<', the result has '<' or starts '&lt;'."""
    i = j = 0
    while i < len(source):
        char = source[i]
        if char == '<' and result.startswith('&lt;', j):
            j += 4
        elif char == '>' and result.startswith('&gt;', j):
            j += 4
        elif j < len(result) and result[j] == char:
            j += 1
        else:
            return False
        i += 1
    return j == len(result)


_CURLY = {'&#8220;': '"', '&#8221;': '"', '&#8216;': "'", '&#8217;': "'"}


def _only_straightens_quotes(source: str, result: str) -> bool:
    """Whether `result` is `source` with some of smarty-pants' four curly-quote entities turned into the straight
    quote they stand for, and nothing else."""
    i = j = 0
    while i < len(source):
        for entity, straight in _CURLY.items():
            if source.startswith(entity, i) and result.startswith(straight, j) and not source.startswith(straight, i):
                i += len(entity)
                j += 1
                break
        else:
            if j < len(result) and result[j] == source[i]:
                i += 1
                j += 1
            else:
                return False
    return j == len(result)


def _loaded_images(html: str) -> list:
    """The <img> elements in `html` that load something: a bare <img> with no src shows nothing."""
    return [img for img in BeautifulSoup(html, 'html.parser').find_all('img') if img.get('src')]


def _hrefs(html: str) -> list:
    return [a.get('href') for a in BeautifulSoup(html, 'html.parser').find_all('a')]


# ---------------------------------------------------------------------------
# markdown_to_html
# ---------------------------------------------------------------------------

def check_markdown_to_html(data: bytes) -> None:
    """markdown_to_html: for any text, and either setting of anchors_new_tab and allow_img (first byte), the HTML
    out satisfies the allowlist property and nothing raises. With allow_img=False (the notification pages) no image
    is loaded. Empty text is ''. Budgeted: the pump_* seeds are the constructs markdown2 is quadratic in."""
    control = data[0] if data else 0
    text = _input_text(data[1:])
    html = markdown_to_html(text, anchors_new_tab=not control & 1, allow_img=not control & 2, test_env=TEST_ENV)
    assert_allowlisted(html)
    if control & 2:
        assert not _loaded_images(html), 'markdown_to_html(allow_img=False) loads an image'
    if not text:
        assert html == ''


# ---------------------------------------------------------------------------
# The preprocessors
# ---------------------------------------------------------------------------

def _markdown_stashing(function):
    """A preprocessor that stashes markdown code: every code span in the input comes back out verbatim."""
    def check(text, result):
        for span in _code_spans_md(text):
            assert span in result, f'{function.__name__} lost the code span {span[:80]!r}'
    check.function = function
    return check


def _html_stashing(function, *pairs):
    """A preprocessor that stashes <code> spans: every span comes back out verbatim, and it adds each pair of
    tags as a pair, never an opening tag without its closing one."""
    def check(text, result):
        for span in _code_spans_html(text):
            assert span in result, f'{function.__name__} lost the code span {span[:80]!r}'
        for opening, closing in pairs:
            assert _added(text, result, opening) == _added(text, result, closing), (
                f'{function.__name__} added {opening!r} and {closing!r} unequally')
    check.function = function
    return check


def _check_escape_angle_brackets(text, result):
    _markdown_stashing(escape_non_html_angle_brackets)(text, result)
    assert _only_escapes_brackets(text, result), 'escape_non_html_angle_brackets changed more than brackets'


def _check_bold_em(text, result):
    _markdown_stashing(handle_bold_em)(text, result)
    for opening, closing in (('<strong>', '</strong>'), ('<em>', '</em>')):
        assert _added(text, result, opening) == _added(text, result, closing), (
            f'handle_bold_em added {opening!r} and {closing!r} unequally')


def _check_lemmy_autocomplete(text, result):
    """It only ever replaces a [!c@host](url) link by its bracket text, so the text never grows. Code spans are
    not asserted: a span inside the dropped (url) goes with it, by design."""
    assert len(result) <= len(text), 'handle_lemmy_autocomplete lengthened the text'


def _check_make_quotes_straight(text, result):
    _html_stashing(make_quotes_straight)(text, result)
    assert _only_straightens_quotes(text, result), 'make_quotes_straight changed more than curly quotes'


def _check_lemmy_spoilers(text, result):
    _html_stashing(handle_lemmy_spoilers, ('<div class="spoiler_block symmetric">', '</div></details>'))(text, result)


def _anchors(html: str) -> list:
    """(href, names of the elements inside, text) of every anchor, in document order."""
    return [(a.get('href'), [e.name for e in a.find_all(True)], a.get_text())
            for a in BeautifulSoup(html, 'html.parser').find_all('a')]


def _check_links_with_parens(text, result):
    """Never raises; keeps every anchor and every element inside one (nested markup and nested links included),
    changes an href by at most one ')' added or removed at its end, and changes a link's text only by moving one
    ')' into or out of its end: the text never loses another character."""
    before, after = _anchors(text), _anchors(result)
    assert len(before) == len(after), 'links_with_parens changed the number of anchors'
    for (old_href, old_inner, old_text), (new_href, new_inner, new_text) in zip(before, after):
        assert new_inner == old_inner, f'links_with_parens changed the markup inside a link: {old_inner} {new_inner}'
        assert new_text in (old_text, old_text + ')') or (old_text.endswith(')') and new_text == old_text[:-1]), (
            f'links_with_parens changed link text {old_text[-80:]!r} to {new_text[-80:]!r}')
        if old_href is None or new_href == old_href:
            continue
        assert new_href + ')' == old_href or old_href + ')' == new_href, (
            f'links_with_parens changed href {old_href!r} to {new_href!r}')


def _check_fix_www_links(text, result):
    before, after = _hrefs(text), _hrefs(result)
    assert len(before) == len(after)
    for old, new in zip(before, after):
        expected = 'https://' + old if old and old.startswith('www.') else old
        assert new == expected, f'fix_www_links changed href {old!r} to {new!r}'


def _check_lemmy_markdown(text, result):
    """Two spaces go in before every CRLF that follows a non-space character, and nothing else changes."""
    assert result.replace('  \r\n', '\r\n') == text.replace('  \r\n', '\r\n')
    assert len(result) == len(text) + 2 * len(re.findall(r'\S\r\n', text))


PREPROCESSORS = [
    (escape_non_html_angle_brackets, _check_escape_angle_brackets),
    (handle_bold_em, _check_bold_em),
    (handle_better_lists, _html_stashing(handle_better_lists)),
    (handle_lemmy_autocomplete, _check_lemmy_autocomplete),
    (handle_naked_spoilers, _markdown_stashing(handle_naked_spoilers)),
    (handle_spoiler_spacing, _markdown_stashing(handle_spoiler_spacing)),
    (handle_video_embeds, _markdown_stashing(handle_video_embeds)),
    (handle_reddit_spoilers, _html_stashing(handle_reddit_spoilers, ('<tg-spoiler>', '</tg-spoiler>'))),
    (handle_blockquotes, _html_stashing(handle_blockquotes, ('<blockquote markdown="1">', '</blockquote>'))),
    (handle_lemmy_spoilers, _check_lemmy_spoilers),
    (make_quotes_straight, _check_make_quotes_straight),
    (escape_img, lambda text, result: None),   # asserted on its own below
    (links_with_parens, _check_links_with_parens),
    (fix_www_links, _check_fix_www_links),
    (piefed_markdown_to_lemmy_markdown, _check_lemmy_markdown),
]


def check_markdown_preprocessors(data: bytes) -> None:
    """Each markdown_to_html preprocessor, chosen by the first byte, over the rest as text: it returns a str, it
    gives back every code span it stashed, and it changes only what it is for (see each _check_*).

    escape_img replaces every <img ...> with a placeholder, so none is left for the browser to load.
    Budgeted: the seeds include a pump for each preprocessor regex with a backtracking shape (the bold and
    blockquote regexes among them)."""
    function, check = PREPROCESSORS[(data[0] if data else 0) % len(PREPROCESSORS)]
    text = _input_text(data[1:])
    if PLACEHOLDER in text:
        return
    result = function(text)
    assert isinstance(result, str), f'{function.__name__} returned {type(result).__name__}'
    check(text, result)
    if function is escape_img:
        assert not _loaded_images(result), 'escape_img left an image to load'


# ---------------------------------------------------------------------------
# Code and link stash round trip
# ---------------------------------------------------------------------------

def check_markdown_code_stash(data: bytes) -> None:
    """stash_code_html, stash_code_md and stash_link_html, each undone by pop_code/pop_link: the round trip is the
    identity, and there is one placeholder per snippet, in the stashed text or inside another snippet (an inline
    code span can hold a fenced block's). The preprocessors format the stashed text, so a round trip that is not
    the identity changes code (or a link) the author wrote."""
    text = _input_text(data)
    if PLACEHOLDER in text:
        return
    for stash, pop in ((stash_code_html, pop_code), (stash_code_md, pop_code), (stash_link_html, pop_link)):
        snippets, stashed = stash(text, PLACEHOLDER)
        placeholders = stashed.count(PLACEHOLDER) + sum(snippet.count(PLACEHOLDER) for snippet in snippets)
        assert placeholders == len(snippets), f'{stash.__name__} placeholders != snippets'
        restored = pop(snippets, stashed, PLACEHOLDER)
        assert restored == text, f'{stash.__name__} then {pop.__name__} changed the text'


# ---------------------------------------------------------------------------
# Actor link filters (community_links, feed_links, person_links) and the mention regex
# ---------------------------------------------------------------------------

_CREATED = re.compile(r'^https://' + re.escape(SERVER) + r'/(?:community|feed|user)/lookup/')
_ACTOR_FILTERS = (community_link_to_href, feed_link_to_href, person_link_to_href)
_ACTOR_PATTERNS = (COMMUNITY_PATTERN, FEED_PATTERN, PERSON_PATTERN, MENTION_PATTERN)
_HANDLE_CHARS = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-')


def _rendered(soup):
    """`soup` without the nodes a browser renders nothing for: comments, CDATA, declarations, doctypes and
    processing instructions."""
    for node in soup.find_all(string=lambda string: isinstance(string, PreformattedString)):
        node.extract()
    return soup


def check_markdown_actor_links(data: bytes) -> None:
    """community_link_to_href, feed_link_to_href and person_link_to_href, the jinja filters every rendered body
    goes through, over what they are given in production: allowlist_html output. (redos_actor_patterns times
    their regexes on raw text.)

    A link the filter creates is never inside an existing <a> or a <code>, and taking the created links back out
    gives the document it was given: the filter only wraps handles in text. Comments, CDATA and the like are left
    out of that comparison: the filters also rewrite a handle inside a comment, which renders nothing, and cannot
    end it (a created link holds no '-->'). A filter never rewrites inside a tag (owner ruling 2026-10-13): the
    output has the <img> and <a> elements it was given, plus the created links, and every element other than a
    created link carries the attributes it had -- an alt or title naming @user@host is left as it is, no '<a'
    injected into it."""
    html = allowlist_html(_input_text(data), test_env=TEST_ENV)
    if SERVER in html or PLACEHOLDER in html:
        return
    original = _rendered(BeautifulSoup(html, 'html.parser'))
    attributes = [(tag.name, tag.attrs) for tag in original.find_all(True)]
    images, anchors = len(original.find_all('img')), len(original.find_all('a'))
    # html.parser nests an <a> inside an <a> as written; a browser closes the first at the second, so text after the
    # inner one's end is in no link at all. In such a document "inside an existing <a>" means different things to
    # the two, and only <code> is asserted.
    containers = ['code'] if any(a.find_parent('a') for a in original.find_all('a')) else ['a', 'code']
    for function in _ACTOR_FILTERS:
        result = function(html, server_name_override=SERVER)
        assert isinstance(result, str)
        soup = BeautifulSoup(result, 'html.parser')
        for anchor in soup.find_all('a', href=_CREATED):
            assert anchor.find_parent(containers) is None, (
                f'{function.__name__} created a link inside an existing <a> or <code>: {anchor}')
        created = soup.find_all('a', href=_CREATED)
        assert len(soup.find_all('img')) == images, f'{function.__name__} changed the number of <img> elements'
        assert len(soup.find_all('a')) == anchors + len(created), f'{function.__name__} broke an <a> element'
        for anchor in created:
            anchor.unwrap()
        assert [(tag.name, tag.attrs) for tag in _rendered(soup).find_all(True)] == attributes, (
            f'{function.__name__} rewrote inside a tag')
        assert str(_rendered(soup)) == str(original), f'{function.__name__} changed more than wrapping handles in links'


# What each filter wraps a handle in, read back out: the created anchor and its text.
_CREATED_MARKUP = re.compile(r'<a href="https://' + re.escape(SERVER) + r'/(?:community|feed|user)/lookup/'
                             r'[A-Za-z0-9_.-]*/[A-Za-z0-9_.-]*"(?: rel="nofollow noindex")?>([!~@][^<]*)</a>')


def check_redos_actor_patterns(data: bytes) -> None:
    """PERSON_PATTERN, COMMUNITY_PATTERN and FEED_PATTERN through their filters, and visibility.MENTION_PATTERN
    (send_post's mention reader), timed over raw text up to 64 KiB, long runs of @a, !a and ~a among the seeds.
    Every handle matched is made of the handle characters, and a filter's output with its created links read
    back out is its input: it adds links and changes nothing else."""
    text = _input_text(data)
    if SERVER in text or PLACEHOLDER in text:
        return
    for function in _ACTOR_FILTERS:
        result = function(text, server_name_override=SERVER)
        assert _CREATED_MARKUP.sub(r'\1', result) == text, f'{function.__name__} changed more than adding links'
    for pattern in _ACTOR_PATTERNS:
        for match in pattern.finditer(text):
            assert set(match.group(1)) <= _HANDLE_CHARS and set(match.group(2)) <= _HANDLE_CHARS


# ---------------------------------------------------------------------------
# HTML and text helpers
# ---------------------------------------------------------------------------

def check_html_text_helpers(data: bytes) -> None:
    """html_to_text, markdown_to_text, shorten_string, shorten_url, remove_images and mastodon_extra_field_link.

    html_to_text never lengthens its input (entities only shorten) and is the identity on text holding no '<'
    or '&' and not only whitespace (BeautifulSoup turns a document of only whitespace into one space or newline). shorten_string(s, n), n from the first byte, is s when it fits and otherwise s cut to n-3 characters
    and an ellipsis, so never longer than n. shorten_url(s, n) is s without its http:// and https:// shortened
    the same way, and at most 20 characters by default. remove_images leaves no <img> and no <video>.
    mastodon_extra_field_link
    gives the field back or the href of one of its anchors."""
    control, text = _fields(data[:MAX_INPUT], 2)
    text = _text(text)
    max_length = 3 + (control[0] if control else 0)

    plain = html_to_text(text)
    assert isinstance(plain, str) and len(plain) <= len(text), 'html_to_text lengthened its input'
    if '<' not in text and '&' not in text:
        if text.strip():
            assert plain == text, 'html_to_text changed text that holds no markup'
        else:
            assert not plain.strip(), 'html_to_text turned whitespace into text'

    stripped = markdown_to_text(text)
    assert len(stripped) == len(text) - 2 * text.count('# ')

    short = shorten_string(text, max_length)
    assert len(short) <= max_length
    assert short == (text if len(text) <= max_length else text[:max_length - 3] + '…')

    bare = text.replace('https://', '').replace('http://', '')
    assert shorten_url(text, max_length) == (bare if len(bare) <= max_length else bare[:max_length - 3] + '…')
    assert len(shorten_url(text)) <= 20

    without = BeautifulSoup(remove_images(text), 'html.parser')
    assert not without.find_all(['img', 'video']), 'remove_images left an image or a video'

    link = mastodon_extra_field_link(text)
    assert isinstance(link, str)
    assert link == text or link in _hrefs(text)


# ---------------------------------------------------------------------------
# Microblog posts: title, link, quote fallback
# ---------------------------------------------------------------------------

EXCLUDED_HOST = 'excluded.example'


def _compact(text: str) -> str:
    return ''.join(text.split())


def _host_or_none(href):
    try:
        return furl(href).host
    except ValueError:
        return None


def check_microblog_html(data: bytes) -> None:
    """microblog_content_to_title, microblog_content_to_link and strip_quote_fallback.

    The title is a str of at most 197 characters and its link '' or the href of an anchor in an <h1>. The link is
    None or the href of an anchor that is not `skip` and not on the excluded host. strip_quote_fallback removes
    only what it documents: .quote-inline elements and one final "RE: <a href=quote_id>", so the text it leaves,
    whitespace aside, is the text it was given with at most that "RE:" and that link's text gone from the end."""
    first, second = _fields(data[:MAX_INPUT], 2)
    quote_id, html = _text(first), _text(second)

    title, link = microblog_content_to_title(html)
    assert isinstance(title, str) and len(title) <= 197, f'title of {len(title)} characters'
    soup = BeautifulSoup(html, 'html.parser')
    assert link == '' or link in [a.get('href') for h1 in soup.find_all('h1') for a in h1.find_all('a')]

    found = microblog_content_to_link(html, EXCLUDED_HOST, quote_id or None)
    if found is not None:
        assert found in _hrefs(html) and found != (quote_id or None)
        assert _host_or_none(found) != EXCLUDED_HOST

    result = strip_quote_fallback(html, quote_id)
    if result is html:
        return
    before = BeautifulSoup(html, 'html.parser')
    for element in before.select('.quote-inline'):
        element.decompose()
    kept, given = _compact(BeautifulSoup(result, 'html.parser').get_text()), _compact(before.get_text())
    assert given.startswith(kept), 'strip_quote_fallback removed text the author wrote'
    removed = given[len(kept):]
    quote_texts = {_compact(a.get_text()) for a in before.find_all('a') if a.get('href') == quote_id}
    assert removed == '' or (removed.startswith('RE:') and removed[3:] in quote_texts), (
        f'strip_quote_fallback removed {removed[:80]!r}')


# ---------------------------------------------------------------------------
# Posting warning and first paragraph (allowlist_html without test_env)
# ---------------------------------------------------------------------------

def check_posting_warning_html(data: bytes) -> None:
    """first_paragraph and sanitise_posting_warning, both rendered |safe: the allowlist property holds on what
    they return, and the posting warning fits its String(512) column."""
    text = _input_text(data)
    with _no_db_lookups():
        paragraph = first_paragraph(text)
        warning = sanitise_posting_warning(text)
    assert_allowlisted(paragraph)
    assert_allowlisted(warning)
    assert len(warning) <= 512, f'posting warning of {len(warning)} characters'


# ---------------------------------------------------------------------------
# Enhanced images
# ---------------------------------------------------------------------------

_IMG_ATTRS = frozenset({'src', 'alt', 'title', 'data-enhanced-img'})
_APPLIED_ATTRS = frozenset({'src', 'alt', 'title', 'align', 'width', 'height', 'class'})


def check_enhanced_images(data: bytes) -> None:
    """EnhancedImages.run and apply_enhanced_image_attributes. Every image the extra builds is exactly one <img>
    with only the attributes it sets, and after apply_enhanced_image_attributes every such image carries only
    those plus align/width/height/class, inside at most an <a> with only an href: a value taken from the
    markdown (alt text, title, URL, width, height, class) is escaped and cannot add an attribute or an element.
    Budgeted: _md_img_with_attrs_re and apply's img regex are matched over the whole text, apply's on the text with
    the built images put back, as markdown_to_html gives it the whole rendered body."""
    text = _input_text(data)
    md = markdown2.Markdown(extras={'enhanced-images': True})
    md.reset()   # convert() would; the extra is driven directly so no other stage touches the text
    extra = EnhancedImages(md, None)
    hashed = extra.run(text) if extra.test(text) else text
    assert isinstance(apply_enhanced_image_attributes(md._unhash_html_spans(hashed), md), str)
    spans = list(md.html_spans.values())
    for span in spans:
        elements = BeautifulSoup(span, 'html.parser').find_all(True)
        assert [e.name for e in elements] == ['img'], f'the extra built {span[:120]!r}'
        assert set(elements[0].attrs) <= _IMG_ATTRS, f'unexpected attributes in {span[:120]!r}'
    applied = BeautifulSoup(apply_enhanced_image_attributes(''.join(spans), md), 'html.parser')
    for element in applied.find_all(True):
        if element.name == 'a':
            assert set(element.attrs) <= {'href'} and [e.name for e in element.find_all(True)] == ['img']
        else:
            assert element.name == 'img', f'apply_enhanced_image_attributes built a <{element.name}>'
            assert set(element.attrs) <= _APPLIED_ATTRS, f'unexpected attributes {sorted(element.attrs)}'


# ---------------------------------------------------------------------------
# Podcast credit URLs
# ---------------------------------------------------------------------------

def _is_normal(url) -> bool:
    return isinstance(url, str) and url.startswith(('http://', 'https://')) and _normal_url(url) == url


def check_credit_urls(data: bytes) -> None:
    """_urls_in, _normal_url and _document_urls, which match a podcast's credited people against the URLs their
    actor documents give. Never raises; every URL out is http(s), has a host, and is already normal (_normal_url
    gives it back unchanged); _normal_url gives a str only for an http(s) URL with a host."""
    value = _json_or_text(data[:MAX_INPUT])
    texts = [value] if isinstance(value, str) else []
    if isinstance(value, (dict, list)):
        texts.extend(item for item in (value.values() if isinstance(value, dict) else value) if isinstance(item, str))
    for text in texts:
        normal = _normal_url(text)
        if normal is not None:
            assert normal.startswith(('http://', 'https://')) and urlsplit(normal).hostname, (
                f'_normal_url gave {normal!r}')
        for url in _urls_in(text):
            assert _is_normal(url), f'_urls_in gave {url!r}'
    document = value if isinstance(value, dict) else {'url': value, 'attachment': [{'value': value, 'href': value}]}
    for url in _document_urls(document):
        assert _is_normal(url), f'_document_urls gave {url!r}'


# ---------------------------------------------------------------------------
# Regex-only targets (ReDoS)
# ---------------------------------------------------------------------------

_LINK_REFUSED = frozenset('<>"\'')
_LINK_TRAILING = tuple('?!.,:*_~;')


def _assert_link_match(text: str, match) -> None:
    link = match.group(1)
    assert link.startswith(('http://', 'https://', 'www.')), f'link {link[:80]!r}'
    assert not (set(link) & _LINK_REFUSED) and not any(char.isspace() for char in link), f'link {link[:80]!r}'
    assert not link.endswith(_LINK_TRAILING) and not link.endswith('))'), f'link {link[-80:]!r}'
    after = text[match.end():match.end() + 2]
    if after[:1] and after[0] in '?!.,:*_~);':
        after = after[1:]
    assert after == '' or after[0] == '<' or after[0].isspace(), f'link followed by {after!r}'


def check_redos_link_pattern(data: bytes) -> None:
    """LINK_PATTERN, the auto-link regex markdown2 is given and escape_non_html_angle_brackets matches inside
    <...>: every link it finds starts with http://, https:// or www., holds no space, quote or angle bracket, does
    not end in trailing punctuation, and is followed only by optional punctuation then '<', a space or the end."""
    text = _input_text(data)
    for match in LINK_PATTERN.finditer(text):
        _assert_link_match(text, match)
    match = LINK_PATTERN.match(text)
    if match:
        _assert_link_match(text, match)


def check_redos_allowlist_html(data: bytes) -> None:
    """allowlist_html, timed: the spoiler, strikethrough, sub/superscript, ruby, embedded-media and empty-anchor
    regexes it runs over every remote body, and the code and link stashes around them. The allowlist property
    holds on the output (tests/fuzz/targets/sanitizers.py's allowlist_html target asserts it untimed)."""
    assert_allowlisted(allowlist_html(_input_text(data), test_env=TEST_ENV))


_EPISODE_PATH = re.compile(r'/@[^/]+/episodes/[^/]+/?')


def check_redos_castopod(data: bytes) -> None:
    """castopod_episode_url's _CASTOPOD_EPISODE_LINK over a Note's content, timed: it gives None or an http(s)
    episode-page URL on the actor's own host, with no query."""
    note = {'type': 'Note', 'content': _input_text(data)}
    url = castopod_episode_url(note, 'https://pod.example/@show')
    if url is None:
        return
    parts = urlsplit(url)
    assert parts.scheme in ('http', 'https') and parts.hostname == 'pod.example', f'episode url {url!r}'
    assert _EPISODE_PATH.fullmatch(parts.path) and not parts.query, f'episode url {url!r}'


_LABEL = frozenset('abcdefghijklmnopqrstuvwxyz0123456789-')
_EMAIL_LOCAL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.!#$%&'*+/=?^_`{|}~-")
_EMAIL_LABEL = frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-')


def _reference_hostname(value: str) -> bool:
    """...and the last label not a number by the WHATWG test (all digits, or '0x' and hex digits): a browser reads
    such a name as an IPv4 address ('0x7f.0x1' is 127.0.0.1), and no top-level domain is all-numeric."""
    labels = value.split('.')
    last = labels[-1]
    if last.isdigit() or (last.startswith('0x') and set(last[2:]) <= set('0123456789abcdef')):
        return False
    return len(labels) >= 2 and all(
        1 <= len(label) <= 63 and set(label) <= _LABEL and label[0] != '-' and label[-1] != '-' for label in labels)


def _reference_email(value: str) -> bool:
    local, at, domain = value.partition('@')
    labels = domain.split('.')
    return bool(at and local and set(local) <= _EMAIL_LOCAL and len(labels) >= 2
                and all(label and set(label) <= _EMAIL_LABEL for label in labels))


def check_redos_hostnames(data: bytes) -> None:
    """discovery's is_hostname (_HOSTNAME), utils.validate_email (EMAIL_RE) and instance.util.is_fedi_handle, timed,
    each against an independent reading of the grammar it implements: a lower-case DNS name of two or more labels
    of 1-63 letters, digits and hyphens, no label starting or ending with a hyphen, the last not a number; an
    address whose local part is the RFC 5322 atom characters and dots and whose domain is two or more labels
    (validate_email strips first); a handle that is such an address with or without one leading character."""
    text = _input_text(data)
    assert is_hostname(text) is _reference_hostname(text), f'is_hostname disagrees about {text[:80]!r}'
    assert validate_email(text) is _reference_email(text.strip()), f'validate_email disagrees about {text[:80]!r}'
    expected = _reference_email(text.strip()) or _reference_email(text[1:].strip())
    assert bool(is_fedi_handle(text)) is expected, f'is_fedi_handle disagrees about {text[:80]!r}'


# Budgets are about ten times the slowest committed seed, measured in the test container outside coverage
# (2026-10-13): markdown_preprocessors/blockquotes_250_deep 184 ms (handle_blockquotes re-reads the body once
# per level), markdown_code_stash/fence_pump 20 ms, enhanced_images/img_tag_pump 2 ms, the link pumps 11 ms,
# redos_allowlist_html/spoiler_pump 60 ms, redos_actor_patterns/person_pump 51 ms, redos_castopod/space_pump 2 ms,
# redos_hostnames/email_domain_pump 23 ms. Every pump seed took seconds to hours on the regex it was built for.
# markdown_to_html is budgeted too (owner ruling 2026-10-13): its pump_* seeds are the constructs markdown2 reads in
# quadratic time, which markdown_to_html now rewrites first, and each is cut to MARKDOWN_RENDER_CAP; the slowest,
# pump_image_links, took 0.7 s, and the slowest shape known at the cap about 1 s.
# The untimed targets run BeautifulSoup or markdown2 over the input, whose cost is theirs, not a regex's.
TARGETS = [
    Target(name='markdown_to_html', check=check_markdown_to_html, surface='markdown', budget_ms=2000,
           needs_app=False),
    Target(name='markdown_preprocessors', check=check_markdown_preprocessors, surface='markdown', budget_ms=2000,
           needs_app=False),
    Target(name='markdown_code_stash', check=check_markdown_code_stash, surface='markdown', budget_ms=200,
           needs_app=False),
    Target(name='markdown_actor_links', check=check_markdown_actor_links, surface='markdown', budget_ms=None,
           needs_app=False),
    Target(name='html_text_helpers', check=check_html_text_helpers, surface='markdown', budget_ms=None,
           needs_app=False),
    Target(name='microblog_html', check=check_microblog_html, surface='markdown', budget_ms=None, needs_app=False),
    Target(name='posting_warning_html', check=check_posting_warning_html, surface='markdown', budget_ms=None,
           needs_app=False),
    Target(name='enhanced_images', check=check_enhanced_images, surface='markdown', budget_ms=50,
           needs_app=False),
    Target(name='credit_urls', check=check_credit_urls, surface='text_patterns', budget_ms=None, needs_app=False),
    Target(name='redos_link_pattern', check=check_redos_link_pattern, surface='markdown', budget_ms=150,
           needs_app=False),
    Target(name='redos_allowlist_html', check=check_redos_allowlist_html, surface='markdown', budget_ms=600,
           needs_app=False),
    Target(name='redos_actor_patterns', check=check_redos_actor_patterns, surface='markdown', budget_ms=500,
           needs_app=False),
    Target(name='redos_castopod', check=check_redos_castopod, surface='ap_json', budget_ms=50, needs_app=False),
    Target(name='redos_hostnames', check=check_redos_hostnames, surface='text_patterns', budget_ms=250,
           needs_app=False),
]
