#!/usr/bin/python3
"""Verify the actual Tk editor reuses its window and its tray service survives closing it."""
from datetime import date
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

PROJECT = Path(__file__).resolve().parents[1]
if '--inside' not in sys.argv:
    with tempfile.TemporaryDirectory(prefix='dailycheck-ui-bus-') as directory:
        config = Path(directory) / 'session.conf'
        config.write_text('<busconfig><type>session</type><listen>unix:tmpdir=/tmp</listen><auth>EXTERNAL</auth><policy context="default"><allow own="*"/><allow send_destination="*"/><allow receive_sender="*"/></policy></busconfig>')
        raise SystemExit(subprocess.call(['dbus-run-session', '--config-file=' + str(config), '--', sys.executable, __file__, '--inside'], cwd=PROJECT))

sys.path.insert(0, str(PROJECT))
import desktop
from gi.repository import GLib
from store import Store

def until(condition, message):
    for _ in range(150):
        try:
            result = condition()
            if result: return result
        except desktop.GLib.Error: pass
        time.sleep(.05)
    raise AssertionError(message)

with tempfile.TemporaryDirectory(prefix='dailycheck-ui-') as directory:
    root = Path(directory); home = root / 'home'; home.mkdir()
    env = dict(os.environ, HOME=str(home), XDG_DATA_HOME=str(root / 'data'), XDG_CONFIG_HOME=str(root / 'config'), DAILYCHECK_FIXTURE_NO_APP='1')
    store = Store(root / 'data/daily-check/tasks.sqlite3')
    task = store.save('UI route fixture', 'No personal data', date.today().isoformat(), '23:59', True)
    store.db.close()
    connection = desktop.bus()
    def call(name, method, args=None): return desktop.call(connection, name, '/' + name.replace('.', '/'), name, method, args)
    def exists(name): return desktop.call(connection, 'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'NameHasOwner', GLib.Variant('(s)', (name,))).unpack()[0]
    def pid(): return desktop.call(connection, 'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'GetConnectionUnixProcessID', GLib.Variant('(s)', (desktop.APP_NAME,))).unpack()[0]
    with (root / 'fixture.log').open('w+') as fixture_log, (root / 'app.log').open('w+') as app_log:
        fixture = subprocess.Popen([sys.executable, str(PROJECT / 'tests/desktop_fixture.py')], env=env, stdout=fixture_log, stderr=fixture_log)
        app = None
        try:
            until(lambda: exists('org.dailycheck.Fixture'), 'Fixture did not register')
            app = subprocess.Popen([sys.executable, str(PROJECT / 'app.py')], env=env, stdout=app_log, stderr=app_log)
            until(lambda: exists(desktop.APP_NAME) and exists(desktop.SERVICE_NAME), 'App/service did not register')
            initial = pid()
            subprocess.run([sys.executable, str(PROJECT / 'app.py'), '--edit', str(task), '--day', date.today().isoformat(), '--slot', '23:59'], env=env, check=True, timeout=8, stdout=app_log, stderr=app_log)
            state = call(desktop.APP_NAME, 'State').unpack()
            assert state == (task, date.today().isoformat(), '23:59') and pid() == initial
            call(desktop.APP_NAME, 'Quit'); app.wait(timeout=5)
            assert not exists(desktop.APP_NAME) and exists(desktop.SERVICE_NAME)
            call(desktop.SERVICE_NAME, 'Open')
            until(lambda: exists(desktop.APP_NAME), 'Tray Open did not reopen the editor')
            assert pid() != initial
            call(desktop.SERVICE_NAME, 'Quit')
            until(lambda: not exists(desktop.APP_NAME) and not exists(desktop.SERVICE_NAME), 'Quit did not stop both editor and reminders')
            print('PASS: real editor routing/reuse, closing leaves tray reminders running, tray reopens editor, and Quit stops both.')
        except Exception:
            app_log.flush();app_log.seek(0);print(app_log.read()[-5000:])
            print((root / 'data/daily-check/reminders.log').read_text()[-5000:] if (root / 'data/daily-check/reminders.log').exists() else '')
            raise
        finally:
            if exists(desktop.SERVICE_NAME): call(desktop.SERVICE_NAME, 'Quit')
            if exists(desktop.APP_NAME): call(desktop.APP_NAME, 'Quit')
            if app and app.poll() is None: app.terminate(); app.wait(timeout=5)
            fixture.terminate(); fixture.wait(timeout=5)
