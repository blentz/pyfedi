"""The table behind the one gate (follow-up F6, review I8): every argument of every route is either in it, so the gate
finds the row it names, or on the allow-list of arguments that name no account, community, post or reply."""
from app.hidden_routes import EXEMPT_ENDPOINTS, NON_OBJECT_ARGUMENTS, OBJECT_ARGUMENTS, actor_kind


def test_every_route_argument_is_gated_or_named_harmless(app):
    """A new route that names a row by an argument the gate does not know fails here, not in production."""
    unknown = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint in EXEMPT_ENDPOINTS:
            continue
        for argument in rule.arguments:
            if argument == 'actor':
                if actor_kind(rule.rule) is None:
                    unknown.append((rule.rule, argument))
            elif argument in OBJECT_ARGUMENTS:
                continue
            elif argument in NON_OBJECT_ARGUMENTS:
                prefixes = NON_OBJECT_ARGUMENTS[argument]
                if prefixes is not None and not rule.rule.startswith(prefixes):
                    unknown.append((rule.rule, argument))
            else:
                unknown.append((rule.rule, argument))
    assert unknown == [], '\n'.join(f'{rule} {argument}' for rule, argument in unknown)


def test_the_table_and_the_allow_list_do_not_overlap(app):
    assert not set(OBJECT_ARGUMENTS) & set(NON_OBJECT_ARGUMENTS)
    assert 'actor' not in OBJECT_ARGUMENTS and 'actor' not in NON_OBJECT_ARGUMENTS


def test_every_exempt_endpoint_exists(app):
    endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
    assert EXEMPT_ENDPOINTS <= endpoints
