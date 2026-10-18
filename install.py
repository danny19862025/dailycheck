#!/usr/bin/python3
"""Install launchers for this checkout; preserve the existing login-startup preference."""
import argparse
import os
from pathlib import Path
import sys
import tempfile


def config_home(name, fallback):
    path = Path(os.environ.get(name, str(Path.home() / fallback)))
    return path if path.is_absolute() else Path.home() / fallback


def exec_arg(value):
    # Exec argument quoting, followed by the desktop-file backslash escaping layer.
    raw = '"' + ''.join('%%' if c == '%' else '\\' + c if c in '\\"`$' else c for c in str(value)) + '"'
    return raw.replace('\\', '\\\\')


def entry(script, reminders=False):
    args = exec_arg(sys.executable) + ' ' + exec_arg(script) + (' --reminders' if reminders else '')
    return '\n'.join(['[Desktop Entry]', 'Type=Application', 'Name=Daily Check' + (' Reminders' if reminders else ''),
                      'Comment=Local task planning, reminders, and check-ins', 'Exec=' + args, 'Icon=appointment-soon',
                      'Terminal=false', 'Categories=Office;Utility;', 'Keywords=task;reminder;todo;daily;',
                      'StartupNotify=false', 'X-DailyCheck-Managed=true', ''])


def write(path, contents):
    if path.exists():
        previous = path.read_text()
        lines = set(previous.splitlines())
        if 'X-DailyCheck-Managed=true' not in lines and not lines.intersection({'Name=Daily Check', 'Name=Daily Check Reminders'}):
            raise ValueError(f'Existing launcher is not managed by Daily Check: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', prefix='.dailycheck-', dir=path.parent, delete=False) as output:
        temporary = Path(output.name); output.write(contents)
    try:
        temporary.chmod(0o644); os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    flags = parser.add_mutually_exclusive_group()
    flags.add_argument('--autostart', action='store_true')
    flags.add_argument('--no-autostart', action='store_true')
    args = parser.parse_args()
    script = Path(__file__).resolve().with_name('app.py')
    launcher = config_home('XDG_DATA_HOME', '.local/share') / 'applications/daily-check.desktop'
    autostart = config_home('XDG_CONFIG_HOME', '.config') / 'autostart/daily-check-reminders.desktop'
    write(launcher, entry(script))
    if args.no_autostart and autostart.exists():
        previous = autostart.read_text()
        lines = set(previous.splitlines())
        if 'X-DailyCheck-Managed=true' not in lines and 'Name=Daily Check Reminders' not in lines:
            raise ValueError('The existing autostart entry is not managed by Daily Check.')
        autostart.unlink()
    elif args.autostart or autostart.exists():
        write(autostart, entry(script, reminders=True))
    print('Installed application launcher:', launcher)
    print('Login reminders:', 'enabled' if autostart.exists() else 'disabled')
    print('Keep this checkout at:', script.parent)


if __name__ == '__main__':
    main()
