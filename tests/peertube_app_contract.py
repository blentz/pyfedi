"""Whether PeerTube's own app (Framasoft's, Flutter) can decode an answer (spec 2026-11-09-peertube-login-design §2).
Its decoders throw on a missing, null or mistyped field, or an enum value they do not know, and one bad element fails
a whole list. The models are read from the app's source into tests/fixtures/peertube_app_models.json
(interop/apps/dart_models.py, which records the version read); `problems(value, 'VideoDetails')` lists each way an
answer breaks the model it is decoded into, by path.

A nullable field may be missing; when it is there it is decoded whole, so a nullable object's own required fields
are checked too. An unknown type name (a model written by hand, outside json_serializable) is not checked."""
import json
import re
from pathlib import Path

MODELS = json.loads((Path(__file__).parent / 'fixtures' / 'peertube_app_models.json').read_text())
STRINGS = {'String', 'DateTime', 'Uri'}
ANYTHING = {'dynamic', 'Object'}


def _split(arguments: str) -> list[str]:
    """`A, Map<B, C>` as ['A', 'Map<B, C>']."""
    parts, depth, current = [], 0, ''
    for char in arguments:
        depth += {'<': 1, '>': -1}.get(char, 0)
        if char == ',' and depth == 0:
            parts.append(current)
            current = ''
        else:
            current += char
    return parts + [current] if current else parts


def _parse(kind: str) -> tuple[str, list[str]]:
    """A type's name and its type arguments; `?` (nullable) is the field's to say."""
    match = re.fullmatch(r'(\w+)(?:<(.*)>)?', kind.rstrip('?'))
    return match.group(1), _split(match.group(2) or '')


def _bound(kind: str, arguments: dict) -> str:
    """`kind` with a generic class's type parameters replaced by what it was given."""
    for parameter, argument in arguments.items():
        kind = re.sub(rf'\b{parameter}\b', argument.rstrip('?'), kind)
    return kind


def problems(value, kind: str, path: str = '') -> list[str]:
    name, arguments = _parse(kind)
    if name in ANYTHING:
        return []
    if name in STRINGS:
        return [] if isinstance(value, str) else [f'{path}: {type(value).__name__}, not {name}']
    if name == 'int':
        return [] if isinstance(value, int) and not isinstance(value, bool) else [f'{path}: {value!r}, not int']
    if name in ('double', 'num'):
        return [] if isinstance(value, (int, float)) and not isinstance(value, bool) else [f'{path}: not a number']
    if name == 'bool':
        return [] if isinstance(value, bool) else [f'{path}: {value!r}, not bool']
    if name == 'List':
        if not isinstance(value, list):
            return [f'{path}: not a list']
        return [found for index, item in enumerate(value) for found in problems(item, arguments[0], f'{path}[{index}]')]
    if name == 'Map':
        return [] if isinstance(value, dict) else [f'{path}: not an object']
    if name in MODELS['enums']:
        return [] if value in MODELS['enums'][name] else [f'{path}: {value!r} is no {name}']
    model = MODELS['classes'].get(name)
    if model is None:
        return []
    if not isinstance(value, dict):
        return [f'{path or "."}: not an object ({name})']
    given = dict(zip(model['params'], arguments))
    found = []
    for field in model['fields']:
        inner = value.get(field['key'])
        if inner is None:
            if not field['nullable']:
                found.append(f"{path}.{field['key']}: missing or null")
            continue
        found += problems(inner, _bound(field['type'], given), f"{path}.{field['key']}")
    return found
