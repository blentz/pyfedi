"""Every alpha API route's refusal, reached rather than stepped over.

Sub-project 107 -- a second sweep over `app/api/alpha/routes.py`. Sub-project
85's slice A already pins the property that NOTHING answers while the API is
off; it sends empty bodies, so most routes are refused by schema validation
before the `if not enable_api()` line is reached, and those lines stayed
uncovered -- about ninety of them, one per route.

This sweep builds a minimal VALID payload for each route from the marshmallow
schema flask-smorest recorded on it, so the request gets past validation and
the gate is what answers. That covers the refusal on every route, and it means
a route whose gate is missing is caught by name rather than by a property
holding for the wrong reason.

No defect found: every route refuses.
"""
from datetime import datetime

import pytest
from flask import current_app, g
from marshmallow import fields as ma_fields
from marshmallow.validate import Length, OneOf, Range

from app import db
from app.models import Site

GATE = 'alpha api is not enabled'


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    return SimpleNamespace(client=app.test_client(), app=app,
                           baseline=api_baseline)


def a_value(field):
    """Something the field will accept, whatever it is."""
    validators = getattr(field, 'validate', None) or []
    if not isinstance(validators, (list, tuple, set)):
        validators = [validators]   # a single validator is not a list
    for validator in validators:
        if isinstance(validator, OneOf):
            return list(validator.choices)[0]
        if isinstance(validator, Range) and validator.min is not None:
            return validator.min
        if isinstance(validator, Length) and validator.min:
            return 'x' * validator.min
    if isinstance(field, ma_fields.Boolean):
        return True
    if isinstance(field, ma_fields.Integer):
        return 1
    if isinstance(field, (ma_fields.Float, ma_fields.Decimal)):
        return 1
    if isinstance(field, ma_fields.List):
        return []
    if isinstance(field, ma_fields.Nested):
        return minimal_payload(field.nested if not callable(field.nested)
                               else field.nested())
    if isinstance(field, ma_fields.DateTime):
        return datetime(2026, 1, 1).isoformat()
    if isinstance(field, ma_fields.Dict):
        return {}
    return 'x'


def minimal_payload(schema):
    """Every required field on `schema`, and nothing else."""
    instance = schema() if isinstance(schema, type) else schema
    payload = {}
    for name, field in instance.fields.items():
        if field.required:
            payload[field.data_key or name] = a_value(field)
    return payload


def documented_routes(app):
    """Each flask-smorest route under /api/alpha, with the schemas it takes."""
    rows = []
    for rule in app.url_map.iter_rules():
        path = str(rule)
        if not path.startswith('/api/alpha'):
            continue
        if rule.endpoint.split('.')[0] == 'api_alpha':
            continue            # a Lemmy-V3 placeholder, deliberately ungated
        if '<' in path:
            continue            # none of the gated routes take a path segment
        view = app.view_functions[rule.endpoint]
        arguments = (getattr(view, '_apidoc', None) or {}).get('arguments', {})
        parameters = arguments.get('parameters') or []
        for method in sorted(rule.methods - {'HEAD', 'OPTIONS'}):
            rows.append((method, path, parameters))
    return rows


def send(client, method, path, parameters):
    query = {}
    body = {}
    for parameter in parameters:
        if not isinstance(parameter, dict) or 'schema' not in parameter:
            continue
        payload = minimal_payload(parameter['schema'])
        if parameter.get('in') == 'query':
            query.update(payload)
        else:
            body.update(payload)
    return client.open(path, method=method, query_string=query, json=body)


class TestEveryRouteRefusesWhenTheApiIsOff:
    def test_each_one_says_so_by_name(self, env):
        """The payloads are built to PASS validation, so the gate is what
        answers. Anything that still fails validation is listed rather than
        ignored -- a route that cannot be reached here is one this sweep does
        not speak for."""
        refused, unreached, answered = [], [], []
        for method, path, parameters in documented_routes(env.app):
            response = send(env.client, method, path, parameters)
            body = response.get_json(silent=True) or {}
            if response.status_code < 400:
                answered.append(f'{method} {path} -> {response.status_code}')
            elif body.get('message') == GATE:
                refused.append(f'{method} {path}')
            else:
                unreached.append(f'{method} {path} -> {response.status_code}')
        assert answered == []
        assert len(refused) > 110, f'only {len(refused)} reached the gate'
        # 113 of the 118 reach the gate. The five that do not are the three
        # upload endpoints, which want a multipart file rather than JSON, and
        # two GETs whose synthesised id has to name a row that exists; all
        # five are covered by name in tests/test_api_routes_*.py.
        assert len(unreached) == 5, unreached

    # With the API ON, these two run a keyset query over `post`, and
    # sqlakeyset warns once per nullable column it is ordering by. Those
    # warnings are a recorded outstanding item (seven `Post` columns want a
    # migration) and the suite counts them; this sweep must not add copies of
    # them to that count. Both routes are swept with the API OFF above, and
    # both have their own test files.
    NOISY = {'/api/alpha/post/list2'}

    def test_and_none_of_them_says_so_when_it_is_on(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
        still_refusing = []
        for method, path, parameters in documented_routes(env.app):
            if path in self.NOISY:
                continue
            response = send(env.client, method, path, parameters)
            body = response.get_json(silent=True) or {}
            if body.get('message') == GATE:
                still_refusing.append(f'{method} {path}')
        assert still_refusing == []


class TestTheSweepItself:
    def test_it_covers_more_than_ninety_routes(self, env):
        assert len(documented_routes(env.app)) > 90

    def test_a_payload_is_built_for_the_fields_that_need_one(self, env):
        from app.api.alpha.schema import FollowCommunityRequest
        payload = minimal_payload(FollowCommunityRequest)
        assert payload
        assert all(value is not None for value in payload.values())

    def test_a_schema_with_no_required_fields_builds_nothing(self, env):
        from marshmallow import Schema

        class Nothing(Schema):
            optional = ma_fields.String()

        assert minimal_payload(Nothing) == {}

    def test_each_kind_of_field_gets_something_it_accepts(self, env):
        assert a_value(ma_fields.Integer()) == 1
        assert a_value(ma_fields.Boolean()) is True
        assert a_value(ma_fields.List(ma_fields.Integer())) == []
        assert a_value(ma_fields.Dict()) == {}
        assert a_value(ma_fields.String()) == 'x'
        assert a_value(ma_fields.Float()) == 1
        assert 'T' in a_value(ma_fields.DateTime())

    def test_a_field_that_only_accepts_certain_values(self, env):
        field = ma_fields.String(validate=OneOf(['New', 'Old']))
        assert a_value(field) == 'New'

    def test_a_field_with_a_minimum(self, env):
        assert a_value(ma_fields.Integer(validate=Range(min=5))) == 5
        assert a_value(ma_fields.String(validate=Length(min=3))) == 'xxx'
