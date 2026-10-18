"""A private notification server, screen lock, tray watcher, and app activation target."""
import json
import os
from gi.repository import Gio, GLib

NOTIFICATIONS = '''<node><interface name="org.freedesktop.Notifications">
<method name="Notify"><arg type="s" direction="in"/><arg type="u" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="as" direction="in"/><arg type="a{sv}" direction="in"/><arg type="i" direction="in"/><arg type="u" direction="out"/></method>
<method name="CloseNotification"><arg type="u" direction="in"/></method>
<method name="GetCapabilities"><arg type="as" direction="out"/></method>
<signal name="ActionInvoked"><arg type="u"/><arg type="s"/></signal>
<signal name="NotificationClosed"><arg type="u"/><arg type="u"/></signal></interface></node>'''
CONTROL = '''<node><interface name="org.dailycheck.Fixture">
<method name="Get"><arg type="s" direction="out"/></method>
<method name="Action"><arg type="u" direction="in"/><arg type="s" direction="in"/></method>
<method name="Lock"><arg type="b" direction="in"/></method>
</interface></node>'''
WATCHER = '''<node><interface name="org.kde.StatusNotifierWatcher">
<method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
<property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
<property name="ProtocolVersion" type="i" access="read"/>
<property name="RegisteredStatusNotifierItems" type="as" access="read"/>
</interface></node>'''
SCREEN = '''<node><interface name="org.freedesktop.ScreenSaver"><method name="GetActive"><arg type="b" direction="out"/></method></interface></node>'''
APP = '''<node><interface name="org.dailycheck.App"><method name="Show"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/></method><method name="Quit"/></interface></node>'''
state = dict(notifications=[], closed=[], tray=[], opened=[], locked=False)
connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)

def handle(_c, sender, path, interface, method, params, invocation):
    values = params.unpack()
    if method == 'Get': invocation.return_value(GLib.Variant('(s)', (json.dumps(state),)))
    elif method == 'Lock': state['locked'] = values[0]; invocation.return_value(None)
    elif method == 'Action':
        connection.emit_signal(None, '/org/freedesktop/Notifications', 'org.freedesktop.Notifications', 'ActionInvoked', params)
        invocation.return_value(None)
    elif method == 'GetCapabilities': invocation.return_value(GLib.Variant('(as)', (['actions', 'body', 'sound'],)))
    elif method == 'Notify':
        number = values[1] or len(state['notifications']) + 1
        state['notifications'].append(dict(number=number, title=values[3], body=values[4], actions=values[5], hints=values[6]))
        invocation.return_value(GLib.Variant('(u)', (number,)))
    elif method == 'CloseNotification':
        state['closed'].append(values[0]); invocation.return_value(None)
        connection.emit_signal(None, '/org/freedesktop/Notifications', interface, 'NotificationClosed', GLib.Variant('(uu)', (values[0], 3)))
    elif method == 'RegisterStatusNotifierItem': state['tray'].append([sender, values[0]]); invocation.return_value(None)
    elif method == 'GetActive': invocation.return_value(GLib.Variant('(b)', (state['locked'],)))
    elif method == 'Show': state['opened'].append(list(values)); invocation.return_value(None)
    elif method == 'Quit': state['quit'] = True; invocation.return_value(None)

for name in ('org.dailycheck.Fixture', 'org.freedesktop.Notifications', 'org.kde.StatusNotifierWatcher', 'org.freedesktop.ScreenSaver', 'org.dailycheck.App'):
    if name == 'org.dailycheck.App' and os.environ.get('DAILYCHECK_FIXTURE_NO_APP'):
        continue
    result = connection.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'RequestName', GLib.Variant('(su)', (name, 4)), None, Gio.DBusCallFlags.NONE, 1000, None)
    assert result.unpack()[0] == 1
registrations = []
for path, xml in [('/Fixture', CONTROL), ('/org/freedesktop/Notifications', NOTIFICATIONS), ('/StatusNotifierWatcher', WATCHER), ('/ScreenSaver', SCREEN), ('/org/dailycheck/App', APP)]:
    registrations.append(connection.register_object(path, Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0], handle, lambda _c, _s, _p, _i, n: GLib.Variant('b', True) if n == 'IsStatusNotifierHostRegistered' else GLib.Variant('i', 0) if n == 'ProtocolVersion' else GLib.Variant('as', [a + b for a, b in state['tray']]), None))
print('READY', flush=True)
GLib.MainLoop().run()
