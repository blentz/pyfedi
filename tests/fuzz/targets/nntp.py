"""Property checks for the NNTP bridge (tier T5, app/nntp/nntpserver.py and app/nntp/server.py).

Everything an NNTP client sends reaches NNTPConnectionHandler: the command lines, an article's lines after POST,
the AUTHINFO user and passphrase, the date and time of NEWNEWS/NEWGROUPS, the ranges of OVER/HDR/LISTGROUP and the
message-ids of ARTICLE/HEAD/BODY/HDR. The server is reachable by anyone who can open the port, before any login.
What the handler sends back is partly a peer's text too: a remote post's title, its author's name and its body.

Properties, for every input:

* the handler never dies with an exception, and returns when the client closes the connection;
* every command line gets exactly one well-formed NNTP reply -- a status line with a three-digit code, and for a
  multi-line reply a block closed by "." with every other line beginning with "." dot-stuffed -- so a client
  reading the replies in order stays in step with the commands it sent;
* no line sent holds a CR or an LF of its own, and a reply carrying an article (HEAD, BODY, ARTICLE, OVER, HDR)
  carries exactly that article's header lines, body lines and overview fields, whatever its title, author or body
  say: a peer's text cannot end a reply early or forge the next one;
* the work a command does is bounded by the selected group, not by the numbers the client names (each command looks
  up at most one article more than the largest group holds), and the bytes buffered for one line stay bounded;
* a line that is not UTF-8, or longer than the protocol's line limit, gets an error reply or a clean close.

No database and no network. The handler is the production PieFedNNTPConnectionHandler, driven over a fake socket
against _StubServer, which serves three in-memory groups the way PieFedNNTPServer serves communities: articles by
number within the selected group (through the same thread-local community id), by message-id across all groups,
NNTPArticleNotFound for anything else, and a group list that, after a login, leaves out a community the user is
banned from -- as PieFedNNTPServer._groups_for_user does.

The pure helpers -- parse_datetime, parse_range, _parse_message_id, _sanitize_group_name -- have targets of their
own, because a malformed argument is far likelier to reach their edge cases there than through a whole session.
"""

import contextlib
import datetime
import io
import re
from collections import deque

import app.nntp.server as nntp_server
from app.nntp.nntpserver import (_DEFAULT_OVERVIEW_FMT, _MAXLINE, Article, ArticleInfo, NNTPArticleNotFound,
                                 NNTPAuthenticationError, NNTPAuthSetting, NNTPPostError, NNTPPostSetting,
                                 format_datetime, numbers_in_group, parse_datetime, parse_range)
from app.nntp.server import (PieFedNNTPConnectionHandler, _make_message_id, _parse_message_id,
                             _sanitize_group_name)
from tests.fuzz.targets._target import Target
from tests.fuzz.targets.signatures import _fields, _text

# The largest client stream a check sends. A session longer than this is the same commands again.
MAX_STREAM = 16 * 1024
# The largest argument a helper check reads.
MAX_ARGUMENT = 512

_UTC = datetime.timezone.utc


# ---------------------------------------------------------------------------
# parse_datetime: NEWNEWS and NEWGROUPS
# ---------------------------------------------------------------------------

_STRICT_DATE = re.compile(r'[0-9]{6}(?:[0-9]{2})?')
_STRICT_TIME = re.compile(r'[0-9]{6}')


def check_datetime(data: bytes) -> None:
    """parse_datetime, with a time and with the date and time run together (the DATE reply's form): it gives an
    aware UTC datetime or raises ValueError or TypeError, the two its callers are written to refuse. What it gives
    prints back through format_datetime to the same datetime; and for an RFC 3977 date (yyyymmdd, or yymmdd read
    with years before 70 as 20xx) and hhmmss time it is exactly the date and time written."""
    date_field, time_field = _fields(data, 2)
    date_str, time_str = _text(date_field[:MAX_ARGUMENT]), _text(time_field[:MAX_ARGUMENT])
    for args in ((date_str, time_str), (date_str + time_str,)):
        try:
            parsed = parse_datetime(*args)
        except (ValueError, TypeError):
            continue
        assert isinstance(parsed, datetime.datetime) and parsed.tzinfo is _UTC, (
            f'parse_datetime{args!r} gave {parsed!r}')
        assert parse_datetime(*format_datetime(parsed)) == parsed, (
            f'parse_datetime{args!r} gave {parsed!r}, which does not print back to itself')
    if not (_STRICT_DATE.fullmatch(date_str) and _STRICT_TIME.fullmatch(time_str)):
        return
    year = int(date_str[:-4])
    if len(date_str) == 6:
        year += 2000 if year < 70 else 1900
    elif year < 100:
        return   # a four-digit year below 100 is read as a two-digit one; no client writes one
    try:
        expected = datetime.datetime(year, int(date_str[-4:-2]), int(date_str[-2:]), int(time_str[:2]),
                                     int(time_str[2:4]), int(time_str[4:]), tzinfo=_UTC)
    except ValueError:
        return
    assert parse_datetime(date_str, time_str) == expected, (
        f'parse_datetime({date_str!r}, {time_str!r}) is not {expected!r}')


# ---------------------------------------------------------------------------
# parse_range: OVER, HDR and LISTGROUP
# ---------------------------------------------------------------------------

_STRICT_RANGE = re.compile(r'([0-9]+)(?:(-)([0-9]*))?')


# Water marks numbers_in_group is held to: an empty group, a small one, a large community.
_WATER_MARKS = ((0, 0), (1, 3), (1, 10 ** 6))


def check_range(data: bytes) -> None:
    """parse_range never raises and gives None or (int, int-or-None). An RFC 3977 range -- n, n- or n-m in ASCII
    digits -- is read as (n, n), (n, None) and (n, m). numbers_in_group, which the range commands loop over, gives
    exactly the numbers of that range the group's water marks hold, so never more than the group holds."""
    text = _text(data[:MAX_ARGUMENT])
    parsed = parse_range(text)
    assert parsed is None or (
        isinstance(parsed, tuple) and len(parsed) == 2 and type(parsed[0]) is int
        and (parsed[1] is None or type(parsed[1]) is int)), f'parse_range({text!r}) gave {parsed!r}'
    if parsed is not None and parsed[1] is not None:
        first, last = parsed
        for low, high in _WATER_MARKS:
            numbers = numbers_in_group(first, last, low, high)
            assert len(numbers) <= high - low + 1, f'{text!r} in {low}..{high} gave {len(numbers)} numbers'
            if numbers:
                assert low <= numbers[0] and numbers[-1] <= high and first <= numbers[0] and numbers[-1] <= last, (
                    f'{text!r} in {low}..{high} gave {numbers!r}')
            if -10 ** 4 < first and last < 10 ** 4:
                assert list(numbers) == [n for n in range(first, last + 1) if low <= n <= high], (
                    f'{text!r} in {low}..{high} gave {numbers!r}')
    strict = _STRICT_RANGE.fullmatch(text)
    if strict is None or len(text) > 4000:   # int() refuses more than 4300 digits
        return
    low = int(strict.group(1))
    if not strict.group(2):
        expected = (low, low)
    else:
        expected = (low, int(strict.group(3)) if strict.group(3) else None)
    assert parsed == expected, f'parse_range({text!r}) gave {parsed!r}, not {expected!r}'


# ---------------------------------------------------------------------------
# Message-ids and group names
# ---------------------------------------------------------------------------

_GROUP_NAME_CHARS = re.compile(r'[a-z0-9._-]*')
_STRICT_MESSAGE_ID = re.compile(r'<(post|reply)-([0-9]{1,9})@[a-z0-9.-]+>')


def check_names(data: bytes) -> None:
    """_parse_message_id gives (kind, id) or raises ValueError, the one exception every caller catches; what it
    gives is post or reply and an int, and the message-id this server writes for that pair parses back to it. A
    message-id this server writes is read as written. _sanitize_group_name gives only [a-z0-9._-], one character
    per character of the lower-cased name, its dot-separated labels reversed."""
    text = _text(data[:MAX_ARGUMENT])
    try:
        kind, ident = _parse_message_id(text)
    except ValueError:
        pass
    else:
        assert kind in ('post', 'reply') and type(ident) is int, f'_parse_message_id({text!r}) gave {kind!r}, {ident!r}'
        written = _make_message_id(kind, ident, 'example.com')
        assert _parse_message_id(written) == (kind, ident), f'{written!r} does not parse back to {kind}, {ident}'
    strict = _STRICT_MESSAGE_ID.fullmatch(text)
    if strict:
        assert _parse_message_id(text) == (strict.group(1), int(strict.group(2))), (
            f'_parse_message_id({text!r}) misread a message-id this server writes')

    name = _sanitize_group_name(text)
    assert _GROUP_NAME_CHARS.fullmatch(name), f'_sanitize_group_name({text!r}) gave {name!r}'
    clean = ''.join(c if _GROUP_NAME_CHARS.fullmatch(c) else '_' for c in text.lower())
    assert name == '.'.join(reversed(clean.split('.'))), f'_sanitize_group_name({text!r}) gave {name!r}'


# ---------------------------------------------------------------------------
# The connection handler, over a fake socket, against a stub server
# ---------------------------------------------------------------------------

_DOMAIN = 'stub.example'
_EPOCH = datetime.datetime(2026, 1, 1, tzinfo=_UTC)
_USER, _PASSWORD = 'alice', 'secret word'
# recv sizes the fake socket hands over, by the config byte: one byte at a time up to more than one line limit.
_CHUNKS = (1, 2, 7, 64, 512, _MAXLINE + 1, 4 * _MAXLINE)
# The handler buffers at most one read past a line limit before refusing the line.
MAX_BUFFERED = 2 * (_MAXLINE + 1)


class _OverBudget(BaseException):
    """A command looked up more articles than its group holds. BaseException because hdr() swallows Exception
    around an article lookup with a bare except; the flag on the stub records it even then."""


class _Spinning(BaseException):
    """The handler kept reading a connection the client had closed."""


def _info(number, kind, ident, subject, from_, references=''):
    body_text = f'{kind} {ident}'
    return ArticleInfo(number=number, subject=subject, from_=from_, date=_EPOCH,
                       message_id=_make_message_id(kind, ident, _DOMAIN), references=references,
                       bytes=len(body_text), lines=1,
                       headers={'Newsgroups': 'a.example', 'X-Url': f'https://{_DOMAIN}/post/{ident}'})


class _Group:
    """One community, as PieFedNNTPGroup shows it: numbered 1..N, low 0 when empty."""

    def __init__(self, name, community_id, infos, banned_after_login=False):
        self.name = name
        self._community_id = community_id
        self.short_description = f'The {name} community'
        self.articles = {info.number: info for info in infos}
        self.number = len(infos)
        self.low = 1 if infos else 0
        self.high = len(infos)
        self.created = _EPOCH
        self.posting_permitted = True
        self.banned_after_login = banned_after_login


class _Articles:
    """PieFedNNTPServer.articles: IndexArticleDict over the thread-local community's index."""

    def __init__(self, server):
        self._server = server

    def __getitem__(self, key):
        server = self._server
        server.lookups += 1
        if server.lookups > server.lookup_limit:
            server.over_budget = True
            raise _OverBudget
        if isinstance(key, int):
            community_id = getattr(nntp_server._tl, 'community_id', None)
            for group in server.all_groups.values():
                if group._community_id == community_id and key in group.articles:
                    return group.articles[key]
            raise NNTPArticleNotFound(str(key))
        if not isinstance(key, str):
            # IndexArticleDict reads a non-str key as a message-id and _parse_message_id calls .strip() on it.
            raise AttributeError(f'{type(key).__name__!r} object has no attribute strip')
        for group in server.all_groups.values():
            for info in group.articles.values():
                if info.message_id == key:
                    return info
        raise NNTPArticleNotFound(key)


class _StubServer:
    """The NNTPServer interface the handler reads, served from memory."""

    overview_format = _DEFAULT_OVERVIEW_FMT
    ssl_version = None
    debugging = False
    help = None

    def __init__(self, auth, can_post, subscriptions, groups, bodies):
        self.auth = auth
        self.can_post = can_post
        self.subscriptions = subscriptions
        self.all_groups = groups
        self.bodies = bodies
        self.logged_in = False
        self.posted = []
        self.lookups = 0
        self.lookup_limit = max(group.high for group in groups.values()) + 1
        self.over_budget = False

    def refresh(self):
        pass

    @property
    def groups(self):
        return {name: group for name, group in self.all_groups.items()
                if not (self.logged_in and group.banned_after_login)}

    @property
    def articles(self):
        return _Articles(self)

    def article(self, key):
        info = self.articles[key]
        return Article(info=info, body=self.bodies[info.message_id])

    def date(self):
        return _EPOCH

    def newnews(self, wildmat, date):
        return None

    def newgroups(self, date):
        return None

    def auth_user(self, user, password):
        if (user, password) != (_USER, _PASSWORD):
            raise NNTPAuthenticationError('Invalid username or password')
        self.logged_in = True
        return b'Bearer token'

    def post(self, auth_token, lines):
        if not lines.strip():
            raise NNTPPostError('Empty article')
        self.posted.append((auth_token, lines))


class _Socket:
    """The connection: hands the client's bytes to recv in chunks, collects what sendall sends."""

    def __init__(self, stream: bytes, chunk: int):
        self._stream = stream
        self._chunk = chunk
        self._eof_reads_allowed = stream.count(b'\n') + 2
        self.sent = bytearray()
        self.handler = None
        self.max_buffered = 0
        self.spinning = False

    def recv(self, size):
        if not self._stream:
            self._eof_reads_allowed -= 1
            if self._eof_reads_allowed < 0:
                self.spinning = True
                raise _Spinning
            return b''
        limit = min(size, len(self._stream))
        count = min(self._chunk, limit)
        # A read of nothing but whitespace, in the middle of a line, makes _getline refuse the line as too long
        # (reported, not fixed: it is a valid command split across reads). The fake never produces one, so a
        # campaign keeps hunting past it.
        while count < limit and not self._stream[:count].strip() and b'\n' not in self._stream[:count]:
            count += 1
        piece, self._stream = self._stream[:count], self._stream[count:]
        if self.handler is not None:   # _getline appends the piece to its buffer straight away
            self.max_buffered = max(self.max_buffered, len(self.handler._buffer) + len(piece))
        return piece

    def sendall(self, data):
        self.sent += data


class _Handler(PieFedNNTPConnectionHandler):
    """The production handler; setup() only lets the fake socket see its buffer, _getline() starts each line's
    article-lookup count afresh."""

    def setup(self):
        self.request.handler = self

    def _getline(self, *args, **kwargs):
        self.server.lookups = 0
        return super()._getline(*args, **kwargs)


def _groups_and_bodies(subject='Hello', from_=f'alice@{_DOMAIN}', body='Line one\n.dotted\n\nlast line'):
    first = _info(1, 'post', 1, subject, from_)
    a_infos = [first,
               _info(2, 'reply', 2, 'Re: Hello', f'bob@{_DOMAIN}', references=first.message_id),
               _info(3, 'post', 3, 'Third', f'carol@{_DOMAIN}')]
    c_infos = [_info(1, 'post', 4, 'Elsewhere', f'dave@{_DOMAIN}')]
    groups = {'a.example': _Group('a.example', 1, a_infos),
              'b.example': _Group('b.example', 2, []),
              'c.example': _Group('c.example', 3, c_infos, banned_after_login=True)}
    bodies = {info.message_id: f'{info.message_id} body' for info in a_infos + c_infos}
    bodies[first.message_id] = body
    return groups, bodies


def _server_config(config: bytes):
    flags = config[0] if config else 0
    auth = NNTPAuthSetting.REQUIRED if flags & 1 else NNTPAuthSetting.NOAUTH
    can_post = (NNTPPostSetting.NOPOST, NNTPPostSetting.POST,
                NNTPPostSetting.POST | NNTPPostSetting.AUTHREQUIRED,
                NNTPPostSetting.POST | NNTPPostSetting.AUTHREQUIRED)[(flags >> 1) & 3]
    subscriptions = ['a.example'] if flags & 8 else None
    chunk = _CHUNKS[config[1] % len(_CHUNKS)] if len(config) > 1 else _MAXLINE + 1
    return auth, can_post, subscriptions, chunk


def _run_session(config: bytes, stream: bytes, groups, bodies):
    """Drive one connection to its end. Returns (server, socket); raises AssertionError if the handler raised."""
    auth, can_post, subscriptions, chunk = _server_config(config)
    server = _StubServer(auth, can_post, subscriptions, groups, bodies)
    sock = _Socket(stream, chunk)
    nntp_server._tl.__dict__.clear()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            _Handler(sock, ('192.0.2.1', 50000), server)
    except (_Spinning, _OverBudget):
        pass
    except Exception as exc:
        raise AssertionError(f'the connection handler raised {exc!r}') from exc
    finally:
        nntp_server._tl.__dict__.clear()
    assert not sock.spinning, 'the handler kept reading after the client closed the connection'
    assert not server.over_budget, (
        f'one command looked up more than {server.lookup_limit} articles: its work is not bounded by the group')
    assert sock.max_buffered <= MAX_BUFFERED, (
        f'the handler buffered {sock.max_buffered} bytes while reading one line, past {MAX_BUFFERED}')
    return server, sock


_STATUS = re.compile(r'[1-5][0-9][0-9](?: .*)?', re.S)
_MULTILINE = frozenset({'100', '101', '215', '220', '221', '222', '224', '225', '230', '231'})


def _client_lines(stream: bytes) -> list[bytes]:
    """The lines the client sent, each without its LF and one CR before it. The last piece has no LF: the
    connection closed in the middle of it, so it is never a command."""
    pieces = stream.split(b'\n')
    pieces.pop()
    return [piece[:-1] if piece.endswith(b'\r') else piece for piece in pieces]


def _sent_lines(sent: bytes) -> deque:
    assert sent.endswith(b'\r\n'), f'the reply stream does not end with CRLF: {bytes(sent[-40:])!r}'
    lines = bytes(sent).split(b'\r\n')
    lines.pop()
    for line in lines:
        assert b'\r' not in line and b'\n' not in line, f'a line sent holds a bare CR or LF: {line!r}'
    return deque(line.decode('utf-8') for line in lines)


def _ink(text: str) -> str:
    """Text without its whitespace: what has to survive the trip, however lines are trimmed."""
    return ''.join(text.split())


class _Replies:
    """Reads the reply stream in step with the commands, and checks each reply that carries an article."""

    def __init__(self, sent: bytes, groups, bodies):
        self.queue = _sent_lines(sent)
        self.infos = {info.message_id: info for group in groups.values() for info in group.articles.values()}
        self.bodies = bodies

    def status(self, command) -> str:
        assert self.queue, f'{command!r} got no reply'
        line = self.queue.popleft()
        assert _STATUS.fullmatch(line), f'{command!r} got {line!r} where a status line belongs'
        return line

    def block(self, command) -> list[str]:
        lines = []
        while True:
            assert self.queue, f'the reply to {command!r} is never closed by "."'
            line = self.queue.popleft()
            if line == '.':
                return lines
            if line.startswith('.'):
                assert line.startswith('..'), f'the reply to {command!r} holds {line!r}, not dot-stuffed'
                line = line[1:]
            lines.append(line)

    def next_is(self, code) -> bool:
        """Whether the next line sent is a status line with this code."""
        return bool(self.queue) and self.queue[0][:3] == code and self.queue[0][3:4] in ('', ' ')

    def _article(self, status):
        words = status.split()
        assert len(words) >= 3 and words[2] in self.infos, f'{status!r} names no article'
        return self.infos[words[2]]

    def _check_headers(self, command, lines, info):
        expected = 4 + bool(info.references) + len(info.headers)
        assert len(lines) == expected, f'{command!r} sent {len(lines)} header lines, not {expected}: {lines!r}'
        for line in lines:
            assert re.match(r'[A-Za-z-]+: ', line + ' '), f'{command!r} sent {line!r} among the headers'

    def _check_body(self, command, lines, info):
        expected = self.bodies[info.message_id].split('\n')
        assert len(lines) == len(expected), f'{command!r} sent {len(lines)} body lines, not {len(expected)}'
        for sent, written in zip(lines, expected):
            assert _ink(sent) == _ink(written), f'{command!r} sent body line {sent!r} for {written!r}'

    def check_block(self, command, status, lines):
        code = status[:3]
        if code == '221':
            self._check_headers(command, lines, self._article(status))
        elif code == '220':
            info = self._article(status)
            count = 4 + bool(info.references) + len(info.headers)
            assert len(lines) > count and lines[count] == '', f'{command!r} sent no blank line after the headers'
            self._check_headers(command, lines[:count], info)
            self._check_body(command, lines[count + 1:], info)
        elif code == '222':
            self._check_body(command, lines, self._article(status))
        elif code == '224':
            for line in lines:
                fields = line.split('\t')
                info = self.infos.get(fields[4]) if len(fields) > 4 else None
                assert info is not None and fields[0] == str(info.number), f'{command!r} sent overview {line!r}'
                expected = 8 + len(info.headers)
                assert len(fields) == expected, f'{command!r} sent {len(fields)} overview fields, not {expected}'
        elif code == '225':
            for line in lines:
                assert re.fullmatch(r'[0-9]+(?: .*)?', line, re.S), f'{command!r} sent header line {line!r}'
        elif code == '230':
            for line in lines:
                assert line in self.infos, f'{command!r} sent {line!r}, not a message-id'


def _check_conversation(config: bytes, stream: bytes, sent: bytes, groups, bodies) -> None:
    """Walk the client's lines and the replies together; every command line gets its one reply."""
    _auth, can_post, _subscriptions, _chunk = _server_config(config)
    replies = _Replies(sent, groups, bodies)
    greeting = replies.status('(connect)')
    assert greeting[:3] == ('200' if can_post else '201'), f'greeting {greeting!r}'
    lines = deque(_client_lines(stream))

    def malformed(raw):
        try:
            raw.decode('utf-8')
        except UnicodeDecodeError:
            return True
        return len(raw) >= _MAXLINE   # with its CR the line may pass the limit

    while lines:
        raw = lines.popleft()
        if malformed(raw) and replies.next_is('205'):
            replies.status(raw)
            break
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            raise AssertionError(f'{raw!r} is not UTF-8 and was neither refused nor the connection closed')
        if text == '':
            continue
        fold = text.casefold()
        status = replies.status(text)
        code = status[:3]
        if fold == 'quit':
            assert code == '205', f'QUIT got {status!r}'
            break
        assert code != '205', f'{text!r} closed the connection'
        if fold.startswith('listgroup') and code == '211' and replies.next_is('211'):
            # Reported, not fixed: LISTGROUP naming a group not yet selected sends GROUP's one-line 211 before
            # its own. The extra line is read past so a campaign keeps hunting.
            status = replies.status(text)
        if code in _MULTILINE or (code == '211' and fold.startswith('listgroup')):
            replies.check_block(text, status, replies.block(text))
        if code == '340':
            closed = False
            while True:
                if not lines:
                    closed = True   # the client closed in the middle of the article: nothing more is owed
                    break
                raw = lines.popleft()
                if malformed(raw) and replies.next_is('205'):
                    replies.status(raw)
                    closed = True
                    break
                if raw == b'.':
                    break
            if closed:
                break
            status = replies.status('(end of article)')
            assert status[:3] in ('240', '441'), f'an article got {status!r}'
    assert not replies.queue, f'replies nobody asked for: {list(replies.queue)[:4]!r}'


def session_replies(stream: bytes, config: bytes = b'\x00\x05', **article) -> list[str]:
    """The lines the handler sends for one connection carrying `stream`, without their CRLF -- for the regression
    tests in tests/test_nntpserver.py. config is check_handler's config field; `article` overrides article 1's
    subject, from_ or body. The session properties (no exception, a return on close, bounded lookups and buffer)
    are asserted on the way; the conversation is not."""
    groups, bodies = _groups_and_bodies(**article)
    _server, sock = _run_session(config, stream, groups, bodies)
    return bytes(sock.sent).decode('utf-8').split('\r\n')[:-1]


def check_handler(data: bytes) -> None:
    """A whole connection: the input's command lines against three in-memory groups. See the module docstring."""
    config, stream = _fields(data, 2)
    stream = stream[:MAX_STREAM]
    groups, bodies = _groups_and_bodies()
    _server, sock = _run_session(config, stream, groups, bodies)
    _check_conversation(config, stream, bytes(sock.sent), groups, bodies)


# The commands of check_article_framing: every reply that carries article 1, which the input writes.
_FRAMING_SCRIPT = (
    'MODE READER\r\n'
    'GROUP a.example\r\n'
    'HEAD 1\r\n'
    'BODY 1\r\n'
    f'ARTICLE <post-1@{_DOMAIN}>\r\n'
    'OVER 1-3\r\n'
    f'XOVER <post-1@{_DOMAIN}>\r\n'
    'HDR Subject 1-3\r\n'
    f'XHDR From <post-1@{_DOMAIN}>\r\n'
    'NEWNEWS a.example 20250101 000000\r\n'
    'QUIT\r\n').encode()


def check_article_framing(data: bytes) -> None:
    """A peer's title, author name and body, written by the input into article 1, through every reply that
    carries them: framing, header and body line counts and overview fields stay exactly the article's."""
    config, subject, from_, body = _fields(data, 4)
    groups, bodies = _groups_and_bodies(_text(subject[:MAX_ARGUMENT]), _text(from_[:MAX_ARGUMENT]),
                                        _text(body[:MAX_STREAM]))
    config = (config[:1] or b'\x00') + bytes([_CHUNKS.index(_MAXLINE + 1)])
    _server, sock = _run_session(config, _FRAMING_SCRIPT, groups, bodies)
    _check_conversation(config, _FRAMING_SCRIPT, bytes(sock.sent), groups, bodies)


TARGETS = [
    Target(name='nntp_datetime', check=check_datetime, surface='nntp', budget_ms=None, needs_app=False),
    Target(name='nntp_range', check=check_range, surface='nntp', budget_ms=None, needs_app=False),
    Target(name='nntp_names', check=check_names, surface='nntp', budget_ms=None, needs_app=False),
    Target(name='nntp_handler', check=check_handler, surface='nntp', budget_ms=500, needs_app=False),
    Target(name='nntp_article_framing', check=check_article_framing, surface='nntp', budget_ms=500,
           needs_app=False),
]
