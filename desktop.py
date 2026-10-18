"""Native notification actions and a KDE/StatusNotifier tray, without a cloud service."""
from datetime import datetime
import fcntl
from html import escape
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import threading

try:
    from gi.repository import Gio, GLib
except ImportError:
    Gio = GLib = None

APP_NAME = 'org.dailycheck.App'
SERVICE_NAME = 'org.dailycheck.Service'
APP_XML = '''<node><interface name="org.dailycheck.App">
<method name="Show"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/></method>
<method name="Quit"/><method name="State"><arg type="i" direction="out"/><arg type="s" direction="out"/><arg type="s" direction="out"/></method>
</interface></node>'''
SERVICE_XML = '''<node><interface name="org.dailycheck.Service">
<method name="Open"/><method name="Quit"/>
<method name="Pause"><arg type="b" direction="in"/></method>
<method name="State"><arg type="b" direction="out"/><arg type="b" direction="out"/></method>
</interface></node>'''
SNI_XML = '''<node><interface name="org.kde.StatusNotifierItem">
<method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
<method name="ProvideXdgActivationToken"><arg type="s" direction="in"/></method>
''' + ''.join(f'<property name="{name}" type="{kind}" access="read"/>' for name, kind in {
    'Category':'s', 'Id':'s', 'Title':'s', 'Status':'s', 'WindowId':'i', 'IconName':'s',
    'IconThemePath':'s', 'IconPixmap':'a(iiay)', 'OverlayIconName':'s', 'OverlayIconPixmap':'a(iiay)',
    'AttentionIconName':'s', 'AttentionIconPixmap':'a(iiay)', 'AttentionMovieName':'s',
    'ToolTip':'(sa(iiay)ss)', 'ItemIsMenu':'b', 'Menu':'o'}.items()) + '''
<signal name="NewToolTip"/><signal name="NewIcon"/><signal name="NewStatus"><arg type="s"/></signal>
</interface></node>'''
MENU_XML = '''<node><interface name="com.canonical.dbusmenu">
<property name="Version" type="u" access="read"/><property name="TextDirection" type="s" access="read"/>
<property name="Status" type="s" access="read"/><property name="IconThemePath" type="as" access="read"/>
<method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
<arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
<method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/></method>
<method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
<method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/></method>
<method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
<method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
<method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
<signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
</interface></node>'''


def bus():
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def call(connection, name, path, interface, method, args=None, timeout=1500):
    return connection.call_sync(name, path, interface, method, args, None, Gio.DBusCallFlags.NONE, timeout, None)


def screen_locked(connection=None):
    if Gio is None:
        return False
    try:
        connection = connection or bus()
        for name, path in [('org.freedesktop.ScreenSaver', '/ScreenSaver'), ('org.freedesktop.ScreenSaver', '/org/freedesktop/ScreenSaver')]:
            try:
                if call(connection, name, path, 'org.freedesktop.ScreenSaver', 'GetActive', timeout=250).unpack()[0]:
                    return True
            except GLib.Error:
                pass
    except GLib.Error:
        pass
    return False


def show_existing(task=0, day='', slot=''):
    if Gio is None:
        return False
    try:
        call(bus(), APP_NAME, '/org/dailycheck/App', APP_NAME, 'Show', GLib.Variant('(iss)', (int(task or 0), day, slot)))
        return True
    except GLib.Error:
        return False


def launch_ui(task=0, day='', slot=''):
    if show_existing(task, day, slot):
        return
    command = [sys.executable, str(Path(__file__).with_name('app.py'))]
    if task:
        command += ['--edit', str(task), '--day', day, '--slot', slot]
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


class UiBridge:
    """Pump GIO from Tk so notification Edit reuses the existing task editor."""
    def __init__(self, app):
        self.app, self.owner, self.registration = app, None, None
        if Gio is None:
            return
        try:
            self.connection = bus()
            result = call(self.connection, 'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'RequestName', GLib.Variant('(su)', (APP_NAME, 4))).unpack()[0]
            if result != 1:
                return
            self.registration = self.connection.register_object('/org/dailycheck/App', Gio.DBusNodeInfo.new_for_xml(APP_XML).interfaces[0], self.handle, None, None)
            app.after(60, self.pump)
        except GLib.Error:
            pass

    def handle(self, _conn, _sender, _path, _interface, method, args, invocation):
        if method == 'State':
            invocation.return_value(GLib.Variant('(iss)', (self.app.edit_id or 0, self.app.day.get(), self.app.time_var.get())))
            return
        invocation.return_value(None)
        if method == 'Quit':
            self.app.after_idle(self.app.destroy)
        else:
            task, day, slot = args.unpack()
            self.app.after_idle(lambda: self.app.show_task(task, day, slot))

    def pump(self):
        context = GLib.MainContext.default()
        for _ in range(30):
            if not context.pending():
                break
            context.iteration(False)
        self.app.after(60, self.pump)

    def close(self):
        if self.registration:
            self.connection.unregister_object(self.registration)
            call(self.connection, 'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'ReleaseName', GLib.Variant('(s)', (APP_NAME,)))
            self.registration = None


class Service:
    def __init__(self, store, connection=None, opener=launch_ui):
        self.store, self.connection, self.opener = store, connection or bus(), opener
        self.paused, self.tray_registered, self.revision = False, False, 1
        self.notifications, self.inflight, self.registrations, self.tts_process = {}, set(), [], None
        self.loop = GLib.MainLoop()
        self.registrations.append(self.connection.register_object('/org/dailycheck/Service', Gio.DBusNodeInfo.new_for_xml(SERVICE_XML).interfaces[0], self.handle_service, None, None))
        self.registrations.append(self.connection.register_object('/StatusNotifierItem', Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0], self.handle_tray, self.tray_property, None))
        self.registrations.append(self.connection.register_object('/Menu', Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0], self.handle_menu, self.menu_property, None))
        self.owner = Gio.bus_own_name_on_connection(self.connection, SERVICE_NAME, Gio.BusNameOwnerFlags.NONE, None, None)
        self.subscription = self.connection.signal_subscribe('org.freedesktop.Notifications', 'org.freedesktop.Notifications', None, '/org/freedesktop/Notifications', None, Gio.DBusSignalFlags.NONE, self.notification_signal)
        self.watcher = Gio.bus_watch_name_on_connection(self.connection, 'org.kde.StatusNotifierWatcher', Gio.BusNameWatcherFlags.NONE, self.register_tray, lambda *_: setattr(self, 'tray_registered', False))
        self.ticker = GLib.timeout_add_seconds(5, self.tick)
        GLib.idle_add(self.tick)

    def register_tray(self, connection, *_args):
        def done(conn, result):
            try:
                conn.call_finish(result)
                self.tray_registered = True
            except GLib.Error:
                self.tray_registered = False
        connection.call('org.kde.StatusNotifierWatcher', '/StatusNotifierWatcher', 'org.kde.StatusNotifierWatcher', 'RegisterStatusNotifierItem', GLib.Variant('(s)', ('/StatusNotifierItem',)), None, Gio.DBusCallFlags.NONE, 1500, None, done)

    def tray_property(self, _c, _s, _p, _i, name):
        values = {'Category': ('s', 'ApplicationStatus'), 'Id': ('s', 'dailycheck'), 'Title': ('s', 'Daily Check'),
                  'Status': ('s', 'Active'), 'WindowId': ('i', 0), 'IconName': ('s', 'appointment-soon'),
                  'IconThemePath': ('s', ''), 'Menu': ('o', '/Menu'), 'ItemIsMenu': ('b', False),
                  'ToolTip': ('(sa(iiay)ss)', ('appointment-soon', [], 'Daily Check', 'Reminders paused' if self.paused else 'Reminders running · Click to open'))}
        if name.endswith('Pixmap'):
            return GLib.Variant('a(iiay)', [])
        kind, value = values.get(name, ('s', ''))
        return GLib.Variant(kind, value)

    def menu_property(self, _c, _s, _p, _i, name):
        kind, value = {'Version': ('u', 3), 'TextDirection': ('s', 'ltr'), 'Status': ('s', 'normal'), 'IconThemePath': ('as', [])}[name]
        return GLib.Variant(kind, value)

    def menu_nodes(self):
        return {0: {'children-display': GLib.Variant('s', 'submenu')},
                1: {'label': GLib.Variant('s', 'Open Daily Check'), 'enabled': GLib.Variant('b', True)},
                2: {'label': GLib.Variant('s', 'Resume reminders' if self.paused else 'Pause reminders'), 'enabled': GLib.Variant('b', True)},
                3: {'label': GLib.Variant('s', 'Quit Daily Check'), 'enabled': GLib.Variant('b', True)}}

    def handle_menu(self, _c, _s, _p, _i, method, args, invocation):
        values, nodes = args.unpack(), self.menu_nodes()
        if method == 'GetLayout':
            parent, depth, names = values
            if parent not in nodes:
                invocation.return_dbus_error('com.canonical.dbusmenu.Error', 'Unknown menu item'); return
            props = lambda n: {k: v for k, v in nodes[n].items() if not names or k in names}
            children = [GLib.Variant('(ia{sv}av)', (n, props(n), [])) for n in (1, 2, 3)] if parent == 0 and depth != 0 else []
            invocation.return_value(GLib.Variant('(u(ia{sv}av))', (self.revision, (parent, props(parent), children))))
        elif method == 'GetGroupProperties':
            ids, names = values
            invocation.return_value(GLib.Variant('(a(ia{sv}))', ([(n, {k: v for k, v in nodes[n].items() if not names or k in names}) for n in ids if n in nodes],)))
        elif method == 'GetProperty':
            node, name = values
            if node not in nodes or name not in nodes[node]:
                invocation.return_dbus_error('com.canonical.dbusmenu.Error', 'Unknown property'); return
            invocation.return_value(GLib.Variant('(v)', (nodes[node][name],)))
        elif method == 'Event':
            self.menu_event(values[0], values[1]); invocation.return_value(None)
        elif method == 'EventGroup':
            for node, event, *_ in values[0]: self.menu_event(node, event)
            invocation.return_value(GLib.Variant('(ai)', ([],)))
        elif method == 'AboutToShow':
            invocation.return_value(GLib.Variant('(b)', (False,)))
        else:
            invocation.return_value(GLib.Variant('(aiai)', ([], [])))

    def menu_event(self, node, event):
        if event != 'clicked':
            return
        if node == 1:
            self.opener()
        elif node == 2:
            self.pause(not self.paused)
        elif node == 3:
            self.quit()

    def handle_tray(self, _c, _s, _p, _i, method, args, invocation):
        if method in ('Activate', 'SecondaryActivate', 'ContextMenu'):
            self.opener()
        invocation.return_value(None)

    def handle_service(self, _c, _s, _p, _i, method, args, invocation):
        if method == 'State':
            invocation.return_value(GLib.Variant('(bb)', (self.paused, self.tray_registered))); return
        invocation.return_value(None)
        if method == 'Open': self.opener()
        elif method == 'Pause': self.pause(args.unpack()[0])
        elif method == 'Quit': GLib.idle_add(self.quit)

    def pause(self, value):
        self.paused = value
        self.revision += 1
        self.connection.emit_signal(None, '/Menu', 'com.canonical.dbusmenu', 'LayoutUpdated', GLib.Variant('(ui)', (self.revision, 0)))
        self.connection.emit_signal(None, '/StatusNotifierItem', 'org.kde.StatusNotifierItem', 'NewToolTip', None)

    def notify(self, row):
        key = (row['id'], row['occurrence'], row['slot'])
        if key in self.inflight:
            return
        self.inflight.add(key)
        settings = self.store.settings()
        hints = {'desktop-entry': GLib.Variant('s', 'daily-check'), 'urgency': GLib.Variant('y', 1), 'suppress-sound': GLib.Variant('b', not bool(settings['sound']))}
        if settings['sound'] == 'system': hints['sound-name'] = GLib.Variant('s', 'message-new-instant')
        elif settings['sound']: hints['sound-file'] = GLib.Variant('s', settings['sound'])
        actions = ['done', 'Done', '10m', 'Snooze 10 min', '1h', 'Snooze 1 hour', 'tomorrow', 'Tomorrow', 'edit', 'Edit', 'default', 'Open']
        previous = next((number for number, target in self.notifications.items() if target == key), 0)
        body = f"Due {row['occurrence']} at {row['slot']} · {row['priority']} · {escape(row['category'])}\n{escape(row['description'][:1500])}"
        payload = GLib.Variant('(susssasa{sv}i)', ('Daily Check', previous, 'appointment-soon', 'Time to check in: ' + row['title'], body, actions, hints, -1))
        def sent(connection, result):
            self.inflight.discard(key)
            try:
                number = connection.call_finish(result).unpack()[0]
                self.notifications[number] = key
                self.store.notified(*key[:2], datetime.now(), slot=key[2])
                if settings['tts']:
                    if self.tts_process and self.tts_process.poll() is None:
                        self.tts_process.terminate()
                    self.tts_process = speak(row['title'])
            except (GLib.Error, ValueError):
                pass
        self.connection.call('org.freedesktop.Notifications', '/org/freedesktop/Notifications', 'org.freedesktop.Notifications', 'Notify', payload, None, Gio.DBusCallFlags.NONE, 2000, None, sent)

    def close_notification(self, number):
        self.notifications.pop(number, None)
        self.connection.call('org.freedesktop.Notifications', '/org/freedesktop/Notifications', 'org.freedesktop.Notifications', 'CloseNotification', GLib.Variant('(u)', (number,)), None, Gio.DBusCallFlags.NONE, 1000, None, None)

    def notification_signal(self, _c, _sender, _path, _interface, signal, args):
        values = args.unpack()
        number = values[0]
        if signal == 'NotificationClosed':
            self.notifications.pop(number, None); return
        if signal != 'ActionInvoked' or number not in self.notifications:
            return
        task, day, slot = self.notifications[number]
        action = values[1]
        try:
            row = self.store.occurrence(task, day, slot, allow_history=False)
            if row['done']:
                self.close_notification(number); return
            if action == 'done':
                self.store.complete(task, day, slot=slot, locked=screen_locked(self.connection))
            elif action in ('10m', '1h', 'tomorrow'):
                self.store.snooze(task, day, action, slot=slot)
            elif action in ('edit', 'default'):
                self.opener(task, day, slot)
            else:
                return
            self.close_notification(number)
        except ValueError as error:
            self.close_notification(number)
            self.opener(task, day, slot)
            payload = GLib.Variant('(susssasa{sv}i)', ('Daily Check', 0, 'appointment-soon', 'Check-in was not saved', escape(str(error)), [], {'suppress-sound': GLib.Variant('b', True)}, 8000))
            self.connection.call('org.freedesktop.Notifications', '/org/freedesktop/Notifications', 'org.freedesktop.Notifications', 'Notify', payload, None, Gio.DBusCallFlags.NONE, 1500, None, None)
            print(str(error), file=sys.stderr, flush=True)

    def tick(self):
        try:
            self.store.auto_backup()
        except (OSError, sqlite3.Error) as error:
            print('Automatic backup failed: ' + str(error), file=sys.stderr, flush=True)
        for number, (task, day, slot) in list(self.notifications.items()):
            try:
                row = self.store.occurrence(task, day, slot, allow_history=False)
                if row['done'] or row['snoozed']:
                    self.close_notification(number)
            except ValueError:
                self.close_notification(number)
        if not self.paused:
            for row in self.store.pending(datetime.now())[:10]:
                self.notify(row)
        return True

    def quit(self):
        if self.tts_process and self.tts_process.poll() is None:
            self.tts_process.terminate()
        for number in list(self.notifications): self.close_notification(number)
        try:
            call(self.connection, APP_NAME, '/org/dailycheck/App', APP_NAME, 'Quit', timeout=500)
        except GLib.Error:
            pass
        self.loop.quit()
        return False


def speak(text):
    executable = shutil.which('espeak-ng') or shutil.which('espeak')
    if not executable:
        return None
    try:
        child = subprocess.Popen([executable, '--stdin'], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child.stdin.write(str(text)[:300].encode())
        child.stdin.close()
        threading.Thread(target=child.wait, daemon=True).start()
        return child
    except OSError:
        return None


def play_sound(path):
    if path == 'system':
        path = '/usr/share/sounds/freedesktop/stereo/message-new-instant.oga'
    if path and Path(path).is_file():
        command = [shutil.which('paplay'), path] if shutil.which('paplay') else [shutil.which('ffplay'), '-nodisp', '-autoexit', '-loglevel', 'quiet', path]
        if command[0]:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_service(data):
    from store import Store
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (data / 'reminders.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        store = Store(data / 'tasks.sqlite3')
        if Gio is not None:
            service = Service(store)
            try: service.loop.run()
            finally: store.db.close()
        else:
            # Basic reminders still work when Python GObject bindings are unavailable.
            while True:
                store.auto_backup()
                for row in store.pending(datetime.now()):
                    try:
                        result = subprocess.run(['notify-send', '--app-name=Daily Check', '--icon=appointment-soon', '--', row['title'], row['description']], timeout=10, capture_output=True)
                        if result.returncode == 0: store.notified(row['id'], row['occurrence'], datetime.now(), slot=row['slot'])
                    except (OSError, subprocess.TimeoutExpired): pass
                time.sleep(20)
