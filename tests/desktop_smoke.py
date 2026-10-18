#!/usr/bin/python3
"""Exercise real D-Bus notification actions and tray menus against private desktop fixtures."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

PROJECT = Path(__file__).resolve().parents[1]
if '--inside' not in sys.argv:
    with tempfile.TemporaryDirectory(prefix='dailycheck-bus-') as directory:
        config = Path(directory) / 'session.conf'
        config.write_text('<busconfig><type>session</type><listen>unix:tmpdir=/tmp</listen><auth>EXTERNAL</auth><policy context="default"><allow own="*"/><allow send_destination="*"/><allow receive_sender="*"/></policy></busconfig>')
        raise SystemExit(subprocess.call(['dbus-run-session', '--config-file=' + str(config), '--', sys.executable, __file__, '--inside'], cwd=PROJECT))

sys.path.insert(0, str(PROJECT))
import desktop
from store import Store
from gi.repository import GLib

def until(condition, message):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = condition()
        if result: return result
        time.sleep(.05)
    raise AssertionError(message)

with tempfile.TemporaryDirectory(prefix='dailycheck-desktop-') as directory:
    root = Path(directory); data = root / 'data'; home = root / 'home'; home.mkdir()
    env = dict(os.environ, HOME=str(home), XDG_DATA_HOME=str(data), XDG_CONFIG_HOME=str(root / 'config'), PYTHONUNBUFFERED='1')
    store = Store(data / 'daily-check/tasks.sqlite3')
    store.set_settings(sound='', tts=False)
    now = datetime.now(); due = now - timedelta(minutes=2); day = due.date().isoformat(); slot = due.strftime('%H:%M')
    ids = {}
    for name in ('Done', 'Ten', 'Hour', 'Tomorrow', 'Edit', 'Locked', 'Focus'):
        ids[name] = store.save('Fixture ' + name, 'Private desktop test', day, slot, False, focus=name == 'Focus', focus_after=1)
    children = []
    with (root / 'fixture.log').open('w+') as fixture_log, (root / 'service.log').open('w+') as service_log:
        try:
            children.append(subprocess.Popen([sys.executable, str(PROJECT / 'tests/desktop_fixture.py')], cwd=PROJECT, env=env, stdout=fixture_log, stderr=fixture_log))
            connection = desktop.bus()
            def control(method, params=None): return desktop.call(connection, 'org.dailycheck.Fixture', '/Fixture', 'org.dailycheck.Fixture', method, params)
            def state():
                try: return json.loads(control('Get').unpack()[0])
                except desktop.GLib.Error: return None
            until(state, 'Fixture did not start')
            children.append(subprocess.Popen([sys.executable, str(PROJECT / 'app.py'), '--reminders'], cwd=PROJECT, env=env, stdout=service_log, stderr=service_log))
            fixture = until(lambda: (s if (s := state()) and len(s['notifications']) >= 7 and s['tray'] else None), 'Service did not register tray and deliver reminders')
            notices = {n['title'].removeprefix('Time to check in: Fixture '): n for n in fixture['notifications']}
            assert all({'done', '10m', '1h', 'tomorrow', 'edit'} <= set(n['actions'][::2]) for n in notices.values())
            assert all(n['hints']['suppress-sound'] for n in notices.values())
            props = desktop.call(connection, desktop.SERVICE_NAME, '/StatusNotifierItem', 'org.freedesktop.DBus.Properties', 'GetAll', GLib.Variant('(s)', ('org.kde.StatusNotifierItem',))).unpack()[0]
            assert props['Menu'] == '/Menu' and props['IconName'] == 'appointment-soon'
            layout = desktop.call(connection, desktop.SERVICE_NAME, '/Menu', 'com.canonical.dbusmenu', 'GetLayout', GLib.Variant('(iias)', (0, -1, []))).unpack()
            assert len(layout[1][2]) == 3
            def action(name, value): control('Action', GLib.Variant('(us)', (notices[name]['number'], value)))
            action('Done', 'done'); until(lambda: store.occurrence(ids['Done'], day)['done'], 'Done action was not saved')
            for name, action_name, minutes in [('Ten', '10m', 10), ('Hour', '1h', 60), ('Tomorrow', 'tomorrow', None)]:
                action(name, action_name)
                until(lambda: store.occurrence(ids[name], day)['snoozed'], 'Snooze was not saved')
                until_time = datetime.fromisoformat(store.occurrence(ids[name], day)['snoozed'])
                if minutes: assert abs((until_time - datetime.now()).total_seconds() - minutes * 60) < 5
                else: assert until_time.date() == now.date() + timedelta(days=1) and until_time.strftime('%H:%M') == slot
            action('Edit', 'edit'); until(lambda: [ids['Edit'], day, slot] in state()['opened'], 'Edit did not activate the task editor')
            control('Lock', GLib.Variant('(b)', (True,)))
            action('Locked', 'done'); until(lambda: [ids['Locked'], day, slot] in state()['opened'], 'Locked check-in refusal did not activate editor')
            assert not store.occurrence(ids['Locked'], day)['done']
            control('Lock', GLib.Variant('(b)', (False,)))
            action('Focus', 'done'); until(lambda: [ids['Focus'], day, slot] in state()['opened'], 'Focus-window refusal was not handled')
            assert not store.occurrence(ids['Focus'], day)['done']
            desktop.call(connection, desktop.SERVICE_NAME, '/StatusNotifierItem', 'org.kde.StatusNotifierItem', 'Activate', GLib.Variant('(ii)', (0, 0)))
            until(lambda: [0, '', ''] in state()['opened'], 'Tray click did not open the app')
            for expected in (True, False):
                desktop.call(connection, desktop.SERVICE_NAME, '/Menu', 'com.canonical.dbusmenu', 'Event', GLib.Variant('(isvu)', (2, 'clicked', GLib.Variant('i', 0), 0)))
                active = desktop.call(connection, desktop.SERVICE_NAME, '/org/dailycheck/Service', desktop.SERVICE_NAME, 'State').unpack()
                assert active == (expected, True)
            desktop.call(connection, desktop.SERVICE_NAME, '/org/dailycheck/Service', desktop.SERVICE_NAME, 'Quit')
            children[-1].wait(timeout=5)
            assert state().get('quit')
            print('PASS: real D-Bus Done, all snooze choices, Edit routing, lock/focus refusal, tray registration/properties/menu, pause/resume, and Quit. Only private fixtures were changed.')
        except Exception:
            for label, output in [('fixture', fixture_log), ('service', service_log)]:
                output.flush(); output.seek(0); print(label + ':\n' + output.read()[-4000:])
            raise
        finally:
            for child in children:
                if child.poll() is None: child.terminate()
                child.wait(timeout=5)
            store.db.close()
