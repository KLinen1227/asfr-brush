import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from build_release import build_bytes
from verify_public import validate, validate_zip, publication_errors, manifest, digest


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ('distribution', 'petrify_painter'):
            shutil.copytree(ROOT / directory, self.root / directory, ignore=shutil.ignore_patterns('__pycache__'))

    def test_original_snapshot(self):
        self.assertEqual(len(validate(ROOT)['files']), 11)

    def test_missing_file_rejected(self):
        (self.root / 'petrify_painter/petrify_gold.py').unlink()
        with self.assertRaisesRegex(ValueError, 'file list mismatch'):
            validate(self.root)

    def test_extra_private_asset_rejected(self):
        (self.root / 'petrify_painter/private_material.blend').write_bytes(b'not public')
        with self.assertRaisesRegex(ValueError, 'file list mismatch'):
            build_bytes(self.root)

    def test_changed_asset_rejected(self):
        with (self.root / 'petrify_painter/petrify_presets.blend').open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            validate(self.root)

    def test_private_source_even_with_updated_hash_rejected(self):
        source = self.root / 'petrify_painter/__init__.py'
        source.write_bytes(source.read_bytes().replace(b"EDITION = 'PUBLIC'", b"EDITION = 'PRIVATE'"))
        data = manifest(self.root)
        data['files']['__init__.py'] = digest(source.read_bytes())
        (self.root / 'distribution/public-files.json').write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'not PUBLIC'):
            validate(self.root)

    def test_reproducible_build_and_payload(self):
        name, first = build_bytes(self.root)
        self.assertEqual((name, first), build_bytes(self.root))
        validate_zip(io.BytesIO(first), manifest(self.root)['files'])

    def test_publish_gate_closed(self):
        self.assertTrue(publication_errors(self.root))
        self.assertNotIn('Decision required: public_author', publication_errors(self.root))

    def test_generated_cache_never_packaged(self):
        cache = self.root / 'petrify_painter/__pycache__'
        cache.mkdir()
        (cache / 'scratch.pyc').write_bytes(b'generated')
        name, content = build_bytes(self.root)
        validate_zip(io.BytesIO(content), manifest(self.root)['files'])


if __name__ == '__main__':
    unittest.main()
