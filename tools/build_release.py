"""Build a reproducible public-only ZIP locally; no credentials or uploads."""
import io
from pathlib import Path
import os
import tempfile
import zipfile

from verify_public import ROOT, digest, validate, validate_zip


def build_bytes(root=ROOT):
    root = Path(root)
    data = validate(root)
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(data['files']):
            info = zipfile.ZipInfo('petrify_painter/' + name, (2026, 10, 5, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            payload = (root / 'petrify_painter' / name).read_bytes()
            if digest(payload) != data['files'][name]:
                raise ValueError('Payload changed during build: ' + name)
            archive.writestr(info, payload, compresslevel=9)
    output = result.getvalue()
    validate_zip(io.BytesIO(output), data['files'])
    return data['package'], output


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    name, content = build_bytes()
    output = ROOT / 'dist' / name
    atomic_write(output, content)
    atomic_write(output.with_suffix('.zip.sha256.txt'), (digest(content) + '  ' + name + '\n').encode('utf-8'))
    print('BUILT:', output)
    print('SHA256:', digest(content))
    print('Local draft only. Run verify_public.py --publish-check before proposing publication.')


if __name__ == '__main__':
    main()
