"""Validate the fictional example and exported-file identities offline."""
import ast
import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class ShowcaseArtifactTests(unittest.TestCase):
    def test_example_is_explicitly_fictional_and_analysis_only(self):
        ticket = json.loads((ROOT / 'examples/synthetic-ticket.json').read_text())
        self.assertIs(ticket['synthetic'], True)
        self.assertIs(ticket['auto_repair'], False)

    def test_example_evidence_quotes_exist_in_numbered_windows(self):
        packet = json.loads((ROOT / 'examples/synthetic-evidence.json').read_text())
        identifiers = set()
        for source in packet['sources']:
            self.assertNotIn(source['id'], identifiers)
            identifiers.add(source['id'])
            lines = (ROOT / source['path']).read_text().splitlines()
            start, end = source['line_start'], source['line_end']
            self.assertTrue(1 <= start <= end <= len(lines))
            self.assertIn(source['quote'], '\n'.join(lines[start - 1:end]))

    def test_customization_manifest_paths_and_hashes(self):
        manifest = json.loads((ROOT / 'PERSONAL_EXPORT_MANIFEST.json').read_text())
        for item in manifest['customization_files']:
            path = (ROOT / item['path']).resolve()
            self.assertTrue(path.is_relative_to(ROOT))
            self.assertTrue(path.is_file())
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item['sha256'], item['path'])

    def test_exported_python_files_are_syntactically_valid(self):
        manifest = json.loads((ROOT / 'PERSONAL_EXPORT_MANIFEST.json').read_text())
        for item in manifest['customization_files']:
            path = ROOT / item['path']
            if path.suffix == '.py':
                ast.parse(path.read_text(), filename=item['path'])

    def test_public_fixture_allowlist_has_not_changed(self):
        fixtures = json.loads((ROOT / 'scripts/public_fixture_hashes.json').read_text())
        for name, digest in fixtures.items():
            self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), digest, name)


if __name__ == '__main__':
    unittest.main()
