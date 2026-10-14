"""Property checks for the alpha API's request parsing and the small helpers behind it (tier T6, app/api/alpha/schema.py,
app/api/alpha/__init__.py and app/utils.py).

Every /api/alpha route takes its arguments through a marshmallow request schema, applied by flask-smorest's
@bp.arguments(...) with webargs' FlaskParser: a JSON body for most routes, the query string for the GET listings
(where every value arrives as a string, and a List field takes every repetition of its key), a multipart file for the
uploads. The caller is any API client, authenticated or not -- argument parsing runs before the view body, so before
any login check. Owner ruling: invalid client input is a 400, never a generic 500.

Properties:

* api_schemas -- schema.load(value), for every request schema and any JSON value, either returns or raises
  marshmallow.ValidationError; and what it returns honours what the schema declares: every key is a declared field
  (unless the schema INCLUDEs unknown keys), every required field is there, an Integer is an int and not a bool, a
  Boolean a bool, a Float a finite float, a String a str, a List a list of its inner field's values, a Nested a dict
  of its schema's, None only where allow_none, and every validator holds -- OneOf, Length, Range, Equal, and the three
  hand-written ones (validate_color_code, validate_datetime_string, validate_non_empty_string) by an independent
  reading of each (assert_loaded below).
* api_request_args -- the same, through the parser and location the route itself uses, inside a request: whatever
  the parser raises, the API's error handler (shared_error_handler) answers it with a 400; what it returns honours
  the schema as above. This is the whole of what a client's malformed arguments reach before the view body runs.
* api_validators -- the hand-written validators and expand_hex_color on any string: each accepts exactly what an
  independent reading accepts and raises nothing but ValidationError.
* safe_order_by -- for every model and field set it is called with, any sort string gives an ascending or
  descending order on a column of that model whose name is in the field set (or the documented default), never
  anything else, never an exception.

No database and no network. The route arguments are found by introspection (every @bp.arguments on a function in
app.api.alpha.routes), so a route added later is fuzzed without editing this file. The request target builds the
request with Flask's test_request_context and never calls a view: the views need a database for their login checks,
and everything a malformed argument can do before that runs here.
"""

import inspect
import io
import math
import string
from datetime import datetime

from flask import current_app
from marshmallow import INCLUDE, ValidationError, fields, validate
from sqlalchemy import asc, desc

import app.api.alpha.routes as api_routes
from app.api.alpha import shared_error_handler, site_bp
from app.api.alpha.schema import validate_color_code, validate_datetime_string, validate_non_empty_string
from app.models import Community, Instance, User
from app.utils import expand_hex_color, safe_order_by
from tests.fuzz.targets._target import Target
from tests.fuzz.targets.signatures import _json_or_text, _text

# The largest input a check reads. A schema value past this says nothing a shorter one does not.
MAX_INPUT = 16 * 1024
# The largest request body or query string api_request_args sends. Larger than MAX_INPUT because nesting depth is
# what a body's size buys: JSON nested deeply enough to exhaust the decoder's stack takes tens of thousands of
# brackets, and the application accepts bodies up to MAX_CONTENT_LENGTH (100 MiB by default).
MAX_BODY = 256 * 1024

# The datetime format the API's datetime strings are written in (DefaultSchema.Meta.datetimeformat).
API_DATETIME_FORMAT = '%Y-%m-%dT%H:%M:%S.%fZ'


# ---------------------------------------------------------------------------
# The request schemas, found from the routes
# ---------------------------------------------------------------------------

def route_arguments() -> list[tuple[type, str]]:
    """(schema class, location) for every @bp.arguments on a route in app.api.alpha.routes, sorted by schema name and
    location so the first input byte picks the same pair from run to run."""
    found = {}
    for value in vars(api_routes).values():
        if not inspect.isfunction(value):
            continue
        for parameter in getattr(value, '_apidoc', {}).get('arguments', {}).get('parameters', []):
            schema = parameter['schema']
            schema_class = schema if isinstance(schema, type) else type(schema)
            found[(schema_class.__name__, parameter['in'])] = schema_class
    return [(found[key], key[1]) for key in sorted(found)]


ROUTE_ARGUMENTS = route_arguments()
REQUEST_SCHEMAS = sorted({schema_class for schema_class, _ in ROUTE_ARGUMENTS}, key=lambda cls: cls.__name__)
# One instance each: building a schema is the slow part of a load, and a schema instance holds no per-load state.
_SCHEMA_INSTANCES = {schema_class: schema_class() for schema_class in REQUEST_SCHEMAS}


# ---------------------------------------------------------------------------
# What a loaded value must look like
# ---------------------------------------------------------------------------

def is_hex_colour(text: str) -> bool:
    """'#' and three or six hex digits, and nothing else -- read character by character, sharing nothing with
    validate_color_code's regex."""
    return len(text) in (4, 7) and text[0] == '#' and all(c in string.hexdigits for c in text[1:])


def is_api_datetime(text: str) -> bool:
    try:
        datetime.strptime(text, API_DATETIME_FORMAT)
    except ValueError:
        return False
    return True


def has_visible_character(text: str) -> bool:
    return any(not c.isspace() for c in text)


def _assert_validated(validator, value, where: str) -> None:
    if isinstance(validator, validate.OneOf):
        assert value in validator.choices, f'{where}: {value!r} is not one of {validator.choices!r}'
    elif isinstance(validator, validate.Length):
        size = len(value)
        assert validator.equal is None or size == validator.equal, f'{where}: length {size} != {validator.equal}'
        assert validator.min is None or size >= validator.min, f'{where}: length {size} < {validator.min}'
        assert validator.max is None or size <= validator.max, f'{where}: length {size} > {validator.max}'
    elif isinstance(validator, validate.Range):
        if validator.min is not None:
            assert value >= validator.min if validator.min_inclusive else value > validator.min, f'{where}: {value}'
        if validator.max is not None:
            assert value <= validator.max if validator.max_inclusive else value < validator.max, f'{where}: {value}'
    elif isinstance(validator, validate.Equal):
        assert value == validator.comparable, f'{where}: {value!r} != {validator.comparable!r}'
    elif validator is validate_color_code:
        assert is_hex_colour(value), f'{where}: {value!r} was accepted as a hex colour'
    elif validator is validate_datetime_string:
        assert is_api_datetime(value), f'{where}: {value!r} was accepted as an API datetime'
    elif validator is validate_non_empty_string:
        assert has_visible_character(value), f'{where}: {value!r} was accepted as non-empty'
    else:
        raise AssertionError(f'{where}: no reference for validator {validator!r}; teach _assert_validated about it')


def _assert_value(field, value, where: str) -> None:
    if value is None:
        assert field.allow_none, f'{where}: None, but the field does not allow none'
        return
    if isinstance(field, fields.Nested):
        if field.many:
            assert isinstance(value, list), f'{where}: {type(value).__name__}, not a list'
            for index, item in enumerate(value):
                assert_loaded(field.schema, item, f'{where}[{index}]')
        else:
            assert_loaded(field.schema, value, where)
    elif isinstance(field, fields.List):
        assert isinstance(value, list), f'{where}: {type(value).__name__}, not a list'
        for index, item in enumerate(value):
            _assert_value(field.inner, item, f'{where}[{index}]')
    elif isinstance(field, fields.Boolean):
        assert isinstance(value, bool), f'{where}: {value!r} is not a bool'
    elif isinstance(field, fields.Integer):
        assert isinstance(value, int) and not isinstance(value, bool), f'{where}: {value!r} is not an int'
    elif isinstance(field, fields.Float):
        assert isinstance(value, float), f'{where}: {value!r} is not a float'
        assert field.allow_nan or math.isfinite(value), f'{where}: {value!r} is not finite'
    elif isinstance(field, fields.String):
        assert isinstance(value, str), f'{where}: {value!r} is not a str'
    elif isinstance(field, fields.Dict):
        assert isinstance(value, dict), f'{where}: {value!r} is not a dict'
    elif type(field) is fields.Raw:
        pass
    else:
        raise AssertionError(f'{where}: no reference for {type(field).__name__}; teach _assert_value about it')
    for validator in field.validators:
        _assert_validated(validator, value, where)


def assert_loaded(schema, loaded, where: str) -> None:
    """`loaded`, what schema.load returned, holds only what the schema declares, in the declared types."""
    assert isinstance(loaded, dict), f'{where}: {type(loaded).__name__}, not a dict'
    declared = {(field.attribute or name): field for name, field in schema.load_fields.items()}
    if schema.unknown != INCLUDE:
        assert set(loaded) <= set(declared), f'{where}: undeclared keys {sorted(set(loaded) - set(declared))}'
    for key, field in declared.items():
        if key not in loaded:
            assert not field.required, f'{where}: required {key!r} is missing'
            continue
        _assert_value(field, loaded[key], f'{where}.{key}')


# ---------------------------------------------------------------------------
# api_schemas: schema.load on any JSON
# ---------------------------------------------------------------------------

def check_api_schemas(data: bytes) -> None:
    """The first byte picks a request schema; the rest is JSON (a string when it is not JSON) given to .load."""
    if not data:
        return
    schema_class = REQUEST_SCHEMAS[data[0] % len(REQUEST_SCHEMAS)]
    schema = _SCHEMA_INSTANCES[schema_class]
    value = _json_or_text(data[1:MAX_INPUT])
    try:
        loaded = schema.load(value)
    except ValidationError:
        return
    assert_loaded(schema, loaded, schema_class.__name__)


# ---------------------------------------------------------------------------
# api_request_args: the route's own parser and location, and the error handler
# ---------------------------------------------------------------------------

# The Content-Type a JSON body is sent with, picked by the second byte. webargs reads a body as JSON for any JSON
# media type and ignores it otherwise.
_JSON_CONTENT_TYPES = ('application/json', 'application/json; charset=utf-8', 'application/activity+json',
                       'text/plain')


def _request_kwargs(location: str, variant: int, body: bytes) -> dict:
    if location == 'json':
        return {'data': body, 'content_type': _JSON_CONTENT_TYPES[variant % len(_JSON_CONTENT_TYPES)]}
    if location == 'query':
        # A WSGI environ carries the raw query string as bytes decoded as Latin-1 (PEP 3333).
        return {'environ_overrides': {'QUERY_STRING': body.decode('latin-1')}}
    assert location == 'files', f'no request shape for location {location!r}'
    return {'data': {'file': (io.BytesIO(body), 'upload.png')}, 'content_type': 'multipart/form-data'}


def check_api_request_args(data: bytes) -> None:
    """Byte 0 picks a (schema, location) a route uses, byte 1 the variant (a JSON body's Content-Type), and the rest
    is the body, the query string or the uploaded file. Parsed by the parser the API blueprints use, inside a request;
    a raise is answered by shared_error_handler, which must say 400."""
    if len(data) < 2:
        return
    schema_class, location = ROUTE_ARGUMENTS[data[0] % len(ROUTE_ARGUMENTS)]
    kwargs = _request_kwargs(location, data[1], data[2:MAX_BODY])
    schema = _SCHEMA_INSTANCES[schema_class]
    with current_app.test_request_context('/api/alpha/fuzz', method='POST', **kwargs):
        try:
            loaded = site_bp.ARGUMENTS_PARSER.parse(schema, location=location)
        except Exception as error:   # every exception: the property is what the API answers to it
            response, status = shared_error_handler(error)
            assert status == 400, (f'{schema_class.__name__} ({location}): {type(error).__name__}: {error!s:.200} '
                                   f'is answered with {status}, not 400')
            assert response.get_json()['code'] == 400
            return
    assert_loaded(schema, loaded, f'{schema_class.__name__} ({location})')


# ---------------------------------------------------------------------------
# api_validators: the hand-written validators and expand_hex_color
# ---------------------------------------------------------------------------

def _accepts(validator, text: str) -> bool:
    """validate_*'s verdict: True for a return of True, False for a ValidationError. marshmallow refuses a value whose
    validator returns False, so False counts as a refusal too. Any other result or exception escapes."""
    try:
        result = validator(text)
    except ValidationError:
        return False
    assert isinstance(result, bool), f'{validator.__name__}({text!r}) returned {result!r}'
    return result


def check_api_validators(data: bytes) -> None:
    """Byte 0 picks the function; the rest, as UTF-8 text, is its argument."""
    if not data:
        return
    which, text = data[0] % 4, _text(data[1:MAX_INPUT])
    if which == 0:
        assert _accepts(validate_color_code, text) is is_hex_colour(text), f'validate_color_code({text!r})'
    elif which == 1:
        assert _accepts(validate_datetime_string, text) is is_api_datetime(text), f'validate_datetime_string({text!r})'
    elif which == 2:
        assert _accepts(validate_non_empty_string, text) is has_visible_character(text), (
            f'validate_non_empty_string({text!r})')
    else:
        expanded = expand_hex_color(text)
        if len(text) == 4 and is_hex_colour(text):
            assert expanded == '#' + ''.join(c * 2 for c in text[1:]), f'expand_hex_color({text!r}) = {expanded!r}'
        else:
            assert expanded is text, f'expand_hex_color({text!r}) = {expanded!r}, not the input unchanged'


# ---------------------------------------------------------------------------
# safe_order_by
# ---------------------------------------------------------------------------

# (model, allowed fields) exactly as each caller passes them: app/main/routes.py's community list and
# app/admin/routes.py's communities, users and instances.
ORDER_BY_CALLERS = [
    (Community, frozenset({'title', 'subscriptions_count', 'post_count', 'post_reply_count', 'last_active',
                           'created_at'})),
    (Community, frozenset({'title', 'topic_id', 'subscriptions_count', 'show_popular', 'show_all', 'post_count',
                           'content_retention', 'nsfw', 'post_reply_count', 'last_active'})),
    (User, frozenset({'user_name', 'banned', 'reports', 'attitude', 'reputation', 'created', 'last_seen'})),
    (Instance, frozenset({'domain', 'software', 'version', 'trusted', 'last_seen', 'last_successful_send',
                          'failures', 'gone_forever', 'dormant'})),
]


def check_safe_order_by(data: bytes) -> None:
    """Byte 0 picks the caller; the rest, as UTF-8 text, is the sort_by query argument. The order is the one the
    sort string names when its first word is an allowed field -- descending exactly when its second word is 'desc' in
    any case -- and otherwise descending on the first allowed field in name order."""
    if not data:
        return
    model, allowed = ORDER_BY_CALLERS[data[0] % len(ORDER_BY_CALLERS)]
    sort_param = _text(data[1:MAX_INPUT])
    order = safe_order_by(sort_param, model, set(allowed))

    words = sort_param.split()
    if words and words[0] in allowed:
        column = getattr(model, words[0])
        expected = desc(column) if len(words) > 1 and words[1].lower() == 'desc' else asc(column)
    else:
        expected = desc(getattr(model, min(allowed)))
    assert str(order) == str(expected), f'safe_order_by({sort_param!r}) on {model.__name__} = {order}, not {expected}'


TARGETS = [
    Target(name='api_schemas', check=check_api_schemas, surface='api', budget_ms=None, needs_app=False),
    Target(name='api_request_args', check=check_api_request_args, surface='api', budget_ms=None, needs_app=True),
    Target(name='api_validators', check=check_api_validators, surface='api', budget_ms=None, needs_app=False),
    Target(name='safe_order_by', check=check_safe_order_by, surface='api', budget_ms=None, needs_app=False),
]
