"""Offline, fail-closed validation of the reviewed public release snapshot."""
import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def manifest(root=ROOT):
    return json.loads((Path(root) / 'distribution/public-files.json').read_text(encoding='utf-8'))


def validate_zip(path, expected):
    with zipfile.ZipFile(path) as archive:
        entries = [i.filename for i in archive.infolist() if not i.is_dir()]
        wanted = {'petrify_painter/' + name for name in expected}
        if len(entries) != len(set(entries)) or set(entries) != wanted:
            raise ValueError('ZIP file list differs from the public allowlist')
        if archive.testzip() is not None:
            raise ValueError('ZIP CRC failure')
        for name, checksum in expected.items():
            if digest(archive.read('petrify_painter/' + name)) != checksum:
                raise ValueError('ZIP payload hash mismatch: ' + name)


def publication_errors(root=ROOT):
    root = Path(root)
    state = json.loads((root / 'distribution/publication.json').read_text(encoding='utf-8'))
    errors = []
    for key in ('public_author', 'code_license', 'asset_license'):
        if not isinstance(state.get(key), str) or not state[key].strip():
            errors.append('Decision required: ' + key)
    for key in ('asset_rights_confirmed', 'upload_approved'):
        if state.get(key) is not True:
            errors.append('Confirmation required: ' + key)
    if not (root / 'LICENSE').is_file():
        errors.append('Actual LICENSE file not supplied')
    return errors


def validate(root=ROOT, check_archive=True):
    root = Path(root)
    baseline = manifest(root)
    if baseline.get('edition') != 'PUBLIC' or baseline.get('version') != [0, 0, 1]:
        raise ValueError('This validator is pinned to PUBLIC 0.0.1')
    expected = baseline['files']
    package = root / 'petrify_painter'
    if package.is_symlink():
        raise ValueError('Package must not be a symlink')
    for name in expected:
        parts = PurePosixPath(name).parts
        if '\\' in name or PurePosixPath(name).is_absolute() or '..' in parts:
            raise ValueError('Unsafe allowlist path')
    actual = set()
    for path in package.rglob('*'):
        if path.is_symlink():
            raise ValueError('Symlink not allowed: ' + str(path.relative_to(root)))
        rel = path.relative_to(package)
        if path.is_file() and '__pycache__' not in rel.parts:
            actual.add(rel.as_posix())
    if actual != set(expected):
        raise ValueError('Package file list mismatch; missing=%s extra=%s' %
                         (sorted(set(expected) - actual), sorted(actual - set(expected))))
    for name, checksum in expected.items():
        data = (package / name).read_bytes()
        if digest(data) != checksum:
            raise ValueError('Reviewed payload hash mismatch: ' + name)
        if name.endswith('.py'):
            compile(data, name, 'exec')
    assignments = {}
    for node in ast.parse((package / '__init__.py').read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ('EDITION', 'DISPLAY_NAME', 'bl_info'):
                    assignments[target.id] = ast.literal_eval(node.value)
    info = assignments.get('bl_info', {})
    if assignments.get('EDITION') != 'PUBLIC' or info.get('version') != (0, 0, 1):
        raise ValueError('Source is not PUBLIC 0.0.1')
    if info.get('name') != 'ASFR笔刷0.0.1':
        raise ValueError('Unexpected release name')
    # Heuristics only: not a complete secrets/security audit. Scan publishable text.
    ignored = {'.git', '__pycache__', 'release-assets', 'dist', 'local-checks'}
    patterns = [r'[A-Za-z]:[\\/]Users[\\/][^\s]+',
                r'gh[pousr]_[A-Za-z0-9]{30,}', r'github_pat_[A-Za-z0-9_]{30,}',
                r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----']
    for path in root.rglob('*'):
        rel = path.relative_to(root)
        if set(rel.parts) & ignored or path.suffix not in ('.py', '.md', '.json', '.yml', '.yaml'):
            continue
        if path.is_symlink():
            raise ValueError('Publishable text is symlinked: ' + rel.as_posix())
        content = path.read_text(encoding='utf-8')
        if any(re.search(pattern, content) for pattern in patterns):
            raise ValueError('Possible personal path or credential in ' + rel.as_posix())
    original = root / 'release-assets' / baseline['package']
    if check_archive and original.exists():
        if digest(original.read_bytes()) != baseline['verified_zip_sha256']:
            raise ValueError('Original verified release ZIP changed')
        validate_zip(original, expected)
    return baseline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish-check', action='store_true', help='Check pending decisions; NEVER uploads')
    args = parser.parse_args()
    try:
        data = validate()
        print('PASS: PUBLIC 0.0.1, %d reviewed payload files; no network used' % len(data['files']))
        if args.publish_check:
            errors = publication_errors()
            if errors:
                print('NOT READY TO PUBLISH:\n- ' + '\n- '.join(errors))
                return 2
            print('Publication fields present; this is NOT a legal review or upload')
        return 0
    except (ValueError, OSError, SyntaxError, zipfile.BadZipFile) as exc:
        print('FAIL:', exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
