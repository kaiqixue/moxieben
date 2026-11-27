import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app


class LocalDataPathTests(unittest.TestCase):
    def test_pin_is_stable_and_missing_target_never_creates_empty_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'teacher'
            target.mkdir()
            (target / 'dictation.sqlite3').touch()
            (root / 'local-data-dir.txt').write_text(str(target), encoding='utf-8')
            with patch.object(app, 'ROOT', root), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(app.local_tenant_dir(), str(target))
                (target / 'dictation.sqlite3').unlink()
                with self.assertRaises(ValueError):
                    app.local_tenant_dir()

    def test_pinned_database_cannot_be_overridden_by_an_inherited_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'teacher'
            target.mkdir()
            database = target / 'dictation.sqlite3'
            database.touch()
            (root / 'local-data-dir.txt').write_text(str(target), encoding='utf-8')
            with patch.object(app, 'ROOT', root), patch.dict(os.environ,
                    {'MOXIEBEN_TENANT_DIR': str(root / 'other')}, clear=True):
                self.assertEqual(app.local_tenant_dir(), str(target))
                database.unlink()
                with self.assertRaises(ValueError):
                    app.local_tenant_dir()
                self.assertFalse((root / 'other').exists())

    def test_explicit_test_environment_can_still_use_separate_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app, 'ROOT', Path(directory)), patch.dict(os.environ,
                    {'MOXIEBEN_TENANT_DIR': 'test-only'}, clear=True):
                self.assertEqual(app.local_tenant_dir(), 'test-only')

    def test_no_pin_uses_existing_teacher_database_without_creating_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'Moxieben' / 'TenantData'
            with patch.object(app, 'ROOT', root), patch.dict(os.environ,
                    {'LOCALAPPDATA': str(root)}, clear=True):
                self.assertIsNone(app.local_tenant_dir())
                self.assertFalse(target.exists())
                target.mkdir(parents=True)
                (target / 'dictation.sqlite3').touch()
                self.assertEqual(app.local_tenant_dir(), str(target))
