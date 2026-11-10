"""What a Lemmy API request carries, under alpha's names (spec 2026-11-10-lemmy-v3-design §3)."""
from flask import request
from marshmallow import EXCLUDE


def auth() -> str | None:
    return request.headers.get('Authorization')


def body(schema, renamed: dict | None = None) -> dict:
    """The JSON body under alpha's names, loaded by alpha's schema for the same request. Jerboa's library sends every
    optional field it was not given as null (kotlinx's explicitNulls), which in Lemmy means not given: so a null is
    left out, rather than refused where alpha's schema takes no null or taken as "clear this" where it does."""
    raw = request.get_json(silent=True)
    raw = {name: value for name, value in raw.items() if value is not None} if isinstance(raw, dict) else {}
    for lemmy, alpha in (renamed or {}).items():
        if lemmy in raw:
            raw[alpha] = raw.pop(lemmy)
    return schema().load(raw, unknown=EXCLUDE)
