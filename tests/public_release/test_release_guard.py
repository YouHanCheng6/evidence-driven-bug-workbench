"""Synthetic offline checks; no customer, employee, or company source data."""
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('release_guard', REPO / 'scripts/check_public_release.py')
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class ReleaseGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_generic_framework_terms_are_allowed(self):
        (self.root / 'README.md').write_text('Bug Workbench / ZenTao / OpenHands / Feishu')
        self.assertEqual(guard.scan(self.root)[1], [])

    def test_token_is_blocked_without_printing_value(self):
        token = 'sk' + '-' + 'A' * 30
        (self.root / 'example.py').write_text(token)
        findings = guard.scan(self.root)[1]
        self.assertTrue(findings)
        self.assertNotIn(token, '\n'.join(findings))

    def test_real_environment_filename_is_blocked(self):
        (self.root / '.env').write_text('MODEL=placeholder')
        self.assertTrue(guard.scan(self.root)[1])

    def test_runtime_data_is_blocked(self):
        folder = self.root / 'uploads'
        folder.mkdir()
        (folder / 'attachment.txt').write_text('synthetic attachment')
        self.assertTrue(guard.scan(self.root)[1])

    def test_fixture_allowlist_requires_unchanged_bytes(self):
        token = 'sk' + '-' + 'B' * 30
        fixture = self.root / 'fixture.py'
        fixture.write_text(token)
        scripts = self.root / 'scripts'
        scripts.mkdir()
        (scripts / 'public_fixture_hashes.json').write_text(json.dumps({'fixture.py': hashlib.sha256(fixture.read_bytes()).hexdigest()}))
        self.assertEqual(guard.scan(self.root)[1], [])
        fixture.write_text(token + '\n# modified')
        self.assertTrue(guard.scan(self.root)[1])

    def test_symlink_outside_export_is_blocked(self):
        (self.root / 'linked.py').symlink_to(REPO / 'scripts/check_public_release.py')
        self.assertTrue(guard.scan(self.root)[1])


if __name__ == '__main__':
    unittest.main()
