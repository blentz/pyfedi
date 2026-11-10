"""Read a Kotlin API library's kotlinx.serialization models into a JSON contract (tests/fixtures/jerboa_models.json):
for each data class, every property it decodes, its Kotlin type and whether it may be missing; for each enum, the
values it takes. tests/lemmy_app_contract.py checks the API's answers against it. Jerboa decodes with
ignoreUnknownKeys only, so a property that is neither nullable nor defaulted is required: a missing one throws, and
the whole answer fails (spec 2026-11-10-lemmy-v3-design §2).

    python3 -m interop.apps.kotlin_models <datatypes dir>[:<dir>...] <enums file> '<library version>' <RootClass>... \
        > tests/fixtures/jerboa_models.json

Type aliases (`typealias PostId = Long`) are resolved; only the classes the named roots reach are kept, with every
enum. 'optional' records a default (`= null`, `= 0`): only such a property may be missing, since kotlinx.serialization
requires every other one, a nullable one included. 'nullable' records a `?`: only such a property may be null."""
import json
import os
import re
import sys

CLASS = re.compile(r'data\s+class\s+(\w+)\s*\(')
PROPERTY = re.compile(r'(?:@\w+(?:\([^)]*\))?\s*)*va[lr]\s+(\w+)\s*:\s*([\w<>?, .]+?)\s*(=\s*[^,\n]+?)?\s*(?:/\*.*?\*/\s*)?,?\s*$',
                      re.MULTILINE)
ENUM = re.compile(r'enum\s+class\s+(\w+)\s*(?:\([^)]*\))?\s*(?::[^{]*)?\{([^}]*)\}', re.DOTALL)
ALIAS = re.compile(r'typealias\s+(\w+)\s*=\s*([\w<>?, .]+)')


def _parameters(source: str, start: int) -> str:
    depth, i = 1, start
    while depth and i < len(source):
        depth += {'(': 1, ')': -1}.get(source[i], 0)
        i += 1
    return source[start:i - 1]


def _enum_values(body: str) -> list[str]:
    values = []
    for entry in body.split(';')[0].split('\n'):
        match = re.match(r'\s*(?:@SerialName\("([^"]+)"\)\s*)?([A-Z]\w*)\s*(?:\([^)]*\))?\s*,?\s*$', entry)
        if match:
            values.append(match.group(1) or match.group(2))
    return values


def read(directories: list[str], enums_file: str) -> dict:
    classes, enums, aliases = {}, {}, {}
    sources = [os.path.join(directory, name) for directory in directories for name in sorted(os.listdir(directory))
               if name.endswith('.kt')]
    for path in sources + [enums_file]:
        source = re.sub(r'(?m)^\s*//.*$', '', open(path, encoding='utf-8').read())
        for match in ALIAS.finditer(source):
            aliases[match.group(1)] = match.group(2).strip()
        for match in ENUM.finditer(source):
            enums.setdefault(match.group(1), _enum_values(match.group(2)))
        for match in CLASS.finditer(source):
            fields = []
            for prop in PROPERTY.finditer(_parameters(source, match.end())):
                key, kind, default = prop.groups()
                kind = kind.replace(' ', '')
                fields.append({'key': key, 'type': kind.rstrip('?'), 'nullable': kind.endswith('?'),
                               'optional': default is not None})
            classes.setdefault(match.group(1), {'file': os.path.basename(path), 'fields': fields})
    for model in classes.values():
        for field in model['fields']:
            field['type'] = re.sub(r'\w+', lambda word: aliases.get(word.group(0), word.group(0)), field['type'])
    return {'classes': classes, 'enums': enums}


def reached(models: dict, roots: list[str]) -> dict:
    """The classes `roots` reach through their properties' types."""
    classes, kept, todo = models['classes'], {}, list(roots)
    while todo:
        name = todo.pop()
        if name in kept or name not in classes:
            continue
        kept[name] = classes[name]
        todo += [part for field in classes[name]['fields'] for part in re.findall(r'\w+', field['type'])]
    return {'classes': kept, 'enums': models['enums']}


if __name__ == '__main__':
    datatypes, enums_file, version, roots = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
    models = reached(read(datatypes.split(os.pathsep), enums_file), roots)
    json.dump({'library': version, 'roots': roots, **models}, sys.stdout, indent=1, sort_keys=True)
    print()
