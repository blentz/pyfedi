"""Whether Jerboa can decode an answer of the Lemmy API (spec 2026-11-10-lemmy-v3-design §2). Its library decodes with
kotlinx.serialization and ignoreUnknownKeys: a property with no default must be there, a null is taken only by a
nullable one, and one bad element fails a whole list. The models are read from the library's source into
tests/fixtures/jerboa_models.json (interop/apps/kotlin_models.py, which records the version read);
`problems(value, 'GetPostsResponse')` lists each way an answer breaks the model it is decoded into, by path."""
import json
import re
from pathlib import Path

MODELS = json.loads((Path(__file__).parent / 'fixtures' / 'jerboa_models.json').read_text())
INTEGERS = {'Int', 'Long', 'Short', 'Byte'}
NUMBERS = {'Double', 'Float'}


def _parse(kind: str) -> tuple[str, list[str]]:
    match = re.fullmatch(r'(\w+)(?:<(.*)>)?', kind)
    arguments, depth, current = [], 0, ''
    for char in match.group(2) or '':
        depth += {'<': 1, '>': -1}.get(char, 0)
        if char == ',' and depth == 0:
            arguments.append(current)
            current = ''
        else:
            current += char
    return match.group(1), arguments + [current] if current else arguments


def problems(value, kind: str, path: str = '') -> list[str]:
    name, arguments = _parse(kind.rstrip('?'))
    if name == 'String':
        return [] if isinstance(value, str) else [f'{path}: {value!r}, not String']
    if name in INTEGERS:
        return [] if isinstance(value, int) and not isinstance(value, bool) else [f'{path}: {value!r}, not {name}']
    if name in NUMBERS:
        return [] if isinstance(value, (int, float)) and not isinstance(value, bool) else [f'{path}: not a number']
    if name == 'Boolean':
        return [] if isinstance(value, bool) else [f'{path}: {value!r}, not Boolean']
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
        raise KeyError(f'{name} is not in the contract: add it as a root (interop/apps/kotlin_models.py)')
    if not isinstance(value, dict):
        return [f'{path or "."}: not an object ({name})']
    found = []
    for field in model['fields']:
        where = f"{path}.{field['key']}"
        if field['key'] not in value:
            if not field['optional']:
                found.append(f'{where}: missing')
        elif value[field['key']] is None:
            if not field['nullable']:
                found.append(f'{where}: null')
        else:
            found += problems(value[field['key']], field['type'], where)
    return found
