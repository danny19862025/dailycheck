"""Portable launcher installation and optional local audio behavior."""
from datetime import datetime, timedelta
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import desktop
from install import entry
from store import Store


class InstallTests(unittest.TestCase):
    def test_launcher_install_preserves_autostart_and_can_disable_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = dict(os.environ, HOME=str(root), XDG_DATA_HOME=str(root / 'data'), XDG_CONFIG_HOME=str(root / 'config'))
            command = [sys.executable, str(Path(__file__).with_name('install.py'))]
            subprocess.run(command, env=env, check=True, capture_output=True)
            start = root / 'config/autostart/daily-check-reminders.desktop'
            launcher = root / 'data/applications/daily-check.desktop'
            self.assertTrue(launcher.exists()); self.assertFalse(start.exists())
            subprocess.run([*command, '--autostart'], env=env, check=True, capture_output=True)
            self.assertIn('--reminders', start.read_text())
            subprocess.run(command, env=env, check=True, capture_output=True)
            self.assertTrue(start.exists())
            subprocess.run([*command, '--no-autostart'], env=env, check=True, capture_output=True)
            self.assertFalse(start.exists()); self.assertTrue(launcher.exists())
            launcher.write_text('[Desktop Entry]\nName=Other App\n')
            self.assertNotEqual(subprocess.run(command, env=env, capture_output=True).returncode, 0)
            self.assertIn('Other App', launcher.read_text())

    @unittest.skipUnless(desktop.GLib, 'Requires GObject bindings')
    def test_exec_arguments_quote_spaces_and_field_codes(self):
        path = Path('/tmp/a space $d `x` "quote" 100%/app.py')
        data = entry(path)
        file = desktop.GLib.KeyFile(); file.load_from_data(data, len(data), desktop.GLib.KeyFileFlags.NONE)
        _, args = desktop.GLib.shell_parse_argv(file.get_string('Desktop Entry', 'Exec'))
        self.assertEqual(args[1].replace('%%', '%'), str(path))

    def test_optional_speech_and_audio_use_local_processes_and_literal_text(self):
        with patch('desktop.shutil.which', return_value='/usr/bin/espeak-ng'), patch('desktop.subprocess.Popen') as process:
            desktop.speak('Literal ; $(text)')
            self.assertEqual(process.call_args.args[0], ['/usr/bin/espeak-ng', '--stdin'])
            process.return_value.stdin.write.assert_called_once_with(b'Literal ; $(text)')
        with tempfile.TemporaryDirectory() as directory:
            sound = Path(directory) / 'a sound.oga'; sound.touch()
            with patch('desktop.shutil.which', return_value='/usr/bin/paplay'), patch('desktop.subprocess.Popen') as process:
                desktop.play_sound(str(sound))
                self.assertEqual(process.call_args.args[0], ['/usr/bin/paplay', str(sound)])
        with patch('desktop.shutil.which', return_value=None), patch('desktop.subprocess.Popen') as process:
            self.assertIsNone(desktop.speak('Missing optional engine')); process.assert_not_called()

    def test_export_freezes_active_timers_and_daily_backup_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); store = Store(root / 'tasks.sqlite3')
            self.addCleanup(store.db.close)
            now = datetime.now().replace(microsecond=0)
            task = store.save('Timer', '', now.date().isoformat(), '09:00', True)
            store.track(task, now.date().isoformat(), now=now - timedelta(seconds=90))
            export = root / 'export.json'; store.export_file(export)
            other = Store(root / 'other/tasks.sqlite3'); self.addCleanup(other.db.close); other.import_file(export)
            row = other.rows(now.date().isoformat())[0]
            self.assertGreaterEqual(row['elapsed'], 90); self.assertIsNone(row['started'])
            backup_dir = root / 'backups'
            for i in range(40):
                (backup_dir / f'tasks-{(now - timedelta(days=i + 1)).date()}.sqlite3').touch()
            store.auto_backup((now + timedelta(days=1)).date().isoformat())
            self.assertEqual(len(list(backup_dir.glob('tasks-????-??-??.sqlite3'))), 30)
