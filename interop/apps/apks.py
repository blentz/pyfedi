"""The newest release of each Android app under test, fetched when the harness runs (owner policy: track the latest).

An app's matrix.json entry names where its APK comes from:
- {"fdroid": "<package>"}: f-droid.org's suggested version, from its package API.
- {"github": "<owner>/<repo>", "asset": "<regex>"}: the latest GitHub release's first asset matching the regex (with
  "prerelease": true, the newest release even if it is a prerelease: Thunder ships its PieFed fixes only there). When
  several match, one built for x86_64 or for every ABI wins over an arm-only one: the emulator is x86_64 (it can
  translate arm64, but slower).
Downloads are cached by file name under interop/apps/.apks (gitignored), so a re-run fetches only new releases.

With "extract_native_libs": true, the APK is rewritten to have its native libraries extracted at install
(android:extractNativeLibs="true") and signed again with the harness's own key. An arm-only React Native app (the
official Pixelfed app, plan W1.4a) keeps its libraries stored in the APK, and React Native's SoLoader cannot load them
from there through the emulator's arm translation: the app died at launch. Extracted, it runs.
"""
import re
import struct
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

CACHE = Path(__file__).parent / '.apks'
EXTRACT_NATIVE_LIBS = 'extractNativeLibs'
_STRING_POOL, _START_ELEMENT, _BOOLEAN = 0x0001, 0x0102, 0x12
FDROID = 'https://f-droid.org'
GITHUB = 'https://api.github.com'


@dataclass(frozen=True)
class Apk:
    url: str
    version: str
    filename: str


def _abi_rank(name: str) -> int:
    lowered = name.lower()
    if 'x86_64' in lowered or 'x86-64' in lowered:
        return 0
    if not re.search(r'arm|aarch|x86', lowered):
        return 1   # no ABI in the name: a universal APK
    if 'arm64' in lowered or 'aarch64' in lowered:
        return 2
    return 3


def resolve(source: dict, http: httpx.Client) -> Apk:
    if 'fdroid' in source:
        package = source['fdroid']
        info = http.get(f'{FDROID}/api/v1/packages/{package}').raise_for_status().json()
        code = info['suggestedVersionCode']
        name = next((p.get('versionName') for p in info.get('packages', []) if p.get('versionCode') == code), None)
        filename = f'{package}_{code}.apk'
        return Apk(f'{FDROID}/repo/{filename}', name or str(code), filename)
    if 'github' in source:
        if source.get('prerelease'):
            # the newest release, prerelease or not: GitHub lists them newest first, and `latest` skips prereleases
            releases = http.get(f'{GITHUB}/repos/{source["github"]}/releases', params={'per_page': 10}).raise_for_status().json()
            release = next((r for r in releases if not r.get('draft')), None)
            if release is None:
                raise LookupError(f'{source["github"]}: no published release')
        else:
            release = http.get(f'{GITHUB}/repos/{source["github"]}/releases/latest').raise_for_status().json()
        pattern = re.compile(source.get('asset', r'\.apk$'))
        assets = [a for a in release.get('assets', []) if pattern.search(a['name'])]
        if not assets:
            raise LookupError(f'{source["github"]} {release.get("tag_name")}: no asset matches {pattern.pattern}')
        asset = min(assets, key=lambda a: (_abi_rank(a['name']), a['name']))
        return Apk(asset['browser_download_url'], release.get('tag_name', '').lstrip('v'), asset['name'])
    raise ValueError(f'unknown APK source {source!r}')


def fetch(source: dict, http: httpx.Client, cache: Path = CACHE) -> tuple[Path, str]:
    """The APK on disk and its version."""
    apk = resolve(source, http)
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / apk.filename
    if not path.exists() or path.stat().st_size == 0:
        partial = path.with_name(path.name + '.part')
        with http.stream('GET', apk.url, follow_redirects=True) as response:
            response.raise_for_status()
            with partial.open('wb') as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)
        partial.rename(path)
    return path, apk.version


def _pool_strings(axml: bytes) -> list[str]:
    """The string pool of a binary XML document (its first chunk after the 8-byte document header)."""
    kind, header_size, _size = struct.unpack_from('<HHI', axml, 8)
    if kind != _STRING_POOL:
        raise ValueError('no string pool where a binary manifest has one')
    count, _styles, flags, strings_start, _styles_start = struct.unpack_from('<IIIII', axml, 16)
    offsets = struct.unpack_from(f'<{count}I', axml, 8 + header_size)
    found = []
    for offset in offsets:
        at = 8 + strings_start + offset
        if flags & 0x100:   # UTF-8: two lengths, characters then bytes, each one or two bytes
            at += 2 if axml[at] & 0x80 else 1
            length = ((axml[at] & 0x7f) << 8 | axml[at + 1]) if axml[at] & 0x80 else axml[at]
            at += 2 if axml[at] & 0x80 else 1
            found.append(axml[at:at + length].decode('utf-8', 'replace'))
        else:
            length = struct.unpack_from('<H', axml, at)[0]
            found.append(axml[at + 2:at + 2 + length * 2].decode('utf-16-le', 'replace'))
    return found


def extracting_native_libs(axml: bytes) -> bytes:
    """The binary manifest `axml` with android:extractNativeLibs set to true where it is set, the same length."""
    names = _pool_strings(axml)
    target = names.index(EXTRACT_NATIVE_LIBS) if EXTRACT_NATIVE_LIBS in names else None
    data, at, changed = bytearray(axml), 8, False
    while at < len(data):
        kind, _header_size, size = struct.unpack_from('<HHI', data, at)
        if kind == _START_ELEMENT:
            attribute_start, attribute_size, attribute_count = struct.unpack_from('<HHH', data, at + 24)
            for index in range(attribute_count):
                attribute = at + 16 + attribute_start + index * attribute_size
                if target is not None and struct.unpack_from('<I', data, attribute + 4)[0] == target:
                    struct.pack_into('<BI', data, attribute + 15, _BOOLEAN, 0xFFFFFFFF)
                    changed = True
        at += size
    if not changed:
        raise LookupError(f'the manifest sets no {EXTRACT_NATIVE_LIBS}')
    return bytes(data)


def with_native_libs_extracted(apk: Path, cache: Path = CACHE) -> Path:
    """`apk` rewritten to extract its native libraries, aligned and signed with the harness's key (made on first use),
    cached beside it."""
    signed = apk.with_name(apk.stem + '-extracted.apk')
    if signed.exists() and signed.stat().st_size:
        return signed
    unsigned, aligned = apk.with_name(apk.stem + '-unsigned.apk'), apk.with_name(apk.stem + '-aligned.apk')
    with zipfile.ZipFile(apk) as source, zipfile.ZipFile(unsigned, 'w') as target:
        for item in source.infolist():
            if item.filename.startswith('META-INF/') and item.filename.rsplit('.', 1)[-1] in ('SF', 'RSA', 'EC', 'MF'):
                continue   # the old signature, which no longer matches
            body = source.read(item.filename)
            target.writestr(item, extracting_native_libs(body) if item.filename == 'AndroidManifest.xml' else body)
    keystore = cache / 'harness.keystore'
    if not keystore.exists():
        subprocess.run(['keytool', '-genkeypair', '-keystore', str(keystore), '-storepass', 'harness', '-keypass',
                        'harness', '-alias', 'harness', '-keyalg', 'RSA', '-keysize', '2048', '-validity', '3650',
                        '-dname', 'CN=PieFed interop harness'], check=True, capture_output=True)
    subprocess.run(['zipalign', '-f', '4', str(unsigned), str(aligned)], check=True, capture_output=True)
    subprocess.run(['apksigner', 'sign', '--ks', str(keystore), '--ks-pass', 'pass:harness', '--out', str(signed),
                    str(aligned)], check=True, capture_output=True)
    unsigned.unlink()
    aligned.unlink()
    return signed
