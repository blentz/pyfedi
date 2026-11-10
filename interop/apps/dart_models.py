"""Read a Flutter app's json_serializable models into a JSON contract (tests/fixtures/peertube_app_models.json): for
each class, every field it decodes, its Dart type and whether it may be null; for each enum, the JSON values it takes.
tests/peertube_app_contract.py checks the API's answers against it, so a field an app requires cannot go missing
unnoticed: its decoders throw on one, and one bad element fails a whole list.

    python3 -m interop.apps.dart_models <app>/lib '<app version>' <RootClass>... \
        > tests/fixtures/peertube_app_models.json

Only the classes the named roots reach are kept (the models the API's answers are decoded into), with every enum.

Only @JsonSerializable classes are read: a model with a fromJson written by hand (the token, TokenAPI) and every
widget are left out, and a test checks those by hand. A field is left out when its @JsonKey excludes it from decoding
or gives it a default."""
import json
import os
import re
import sys

FIELD = re.compile(r'((?:@\w+(?:\([^;]*?\))?\s*)*)final\s+([\w<>?, .]+?)\s+(\w+)\s*;')
CLASS = re.compile(r'@JsonSerializable(?:\([^)]*\))?\s+(?:@\w+(?:\([^)]*\))?\s+)*'
                   r'(?:abstract\s+)?class\s+(\w+)(?:<([^>]*)>)?[^{;]*\{')
ENUM = re.compile(r'\benum\s+(\w+)\s*\{([^}]*)\}')


def _body(source: str, start: int) -> str:
    depth, i = 1, start
    while depth and i < len(source):
        depth += {'{': 1, '}': -1}.get(source[i], 0)
        i += 1
    return source[start:i - 1]


def _enum_values(body: str) -> list:
    values = []
    for match in re.finditer(r'(?:@JsonValue\(([^)]*)\)\s*)?(\w+)\s*(?:\([^)]*\))?\s*[,;]?', body.split(';')[0]):
        raw, name = match.groups()
        if not name or name in ('const', 'final'):
            continue
        if raw is None:
            values.append(name)
        else:
            raw = raw.strip()
            values.append(int(raw) if re.fullmatch(r'-?\d+', raw) else raw.strip('\'"'))
    return values


def read(lib: str) -> dict:
    classes, enums = {}, {}
    for directory, _, names in os.walk(lib):
        for name in sorted(names):
            if not name.endswith('.dart') or name.endswith(('.g.dart', '.freezed.dart')):
                continue
            path = os.path.join(directory, name)
            source = re.sub(r'(?m)^\s*//.*$', '', open(path, encoding='utf-8').read())
            for match in ENUM.finditer(source):
                enums.setdefault(match.group(1), _enum_values(match.group(2)))
            for match in CLASS.finditer(source):
                fields = []
                for field in FIELD.finditer(_body(source, match.end())):
                    annotations, kind, key = field.groups()
                    if 'includeFromJson: false' in annotations or 'defaultValue' in annotations:
                        continue
                    renamed = re.search(r"name:\s*'([^']+)'", annotations)
                    kind = kind.replace(' ', '')
                    fields.append({'key': renamed.group(1) if renamed else key, 'type': kind.rstrip('?'),
                                   'nullable': kind.endswith('?')})
                params = [part.strip().split()[0] for part in (match.group(2) or '').split(',') if part.strip()]
                classes.setdefault(match.group(1), {'file': os.path.relpath(path, lib), 'params': params,
                                                    'fields': fields})
    return {'classes': classes, 'enums': enums}


def reached(models: dict, roots: list[str]) -> dict:
    """The classes `roots` reach through their fields' types."""
    classes, kept, todo = models['classes'], {}, list(roots)
    while todo:
        name = todo.pop()
        if name in kept or name not in classes:
            continue
        kept[name] = classes[name]
        todo += [part for field in classes[name]['fields'] for part in re.findall(r'\w+', field['type'])]
    return {'classes': kept, 'enums': models['enums']}


if __name__ == '__main__':
    lib, version, roots = sys.argv[1], sys.argv[2], sys.argv[3:]
    json.dump({'app': version, 'roots': roots, **reached(read(lib), roots)}, sys.stdout, indent=1, sort_keys=True)
    print()
