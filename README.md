# Daily Check

A local Linux desktop task planner with descriptions, daily repeats, timed
notifications, and completion check-ins. Built with Python, Tkinter, and SQLite.
No cloud account or network service is used.

## Requirements

- Python 3 with Tkinter and SQLite support.
- A Linux graphical desktop session.
- `notify-send` for desktop reminder notifications.

## Run

```sh
git clone https://github.com/danny19862025/dailycheck.git
cd dailycheck
/usr/bin/python3 app.py
```

Enter a task name, optional description, start date, and 24-hour reminder time.
Leave **Repeat every day** checked for a routine, or uncheck it for a one-time
task. Click **+ Add task**. Select an existing task to edit and save it.

Select a task and click **Mark done** to record its completion time. The list
shows whether you finished on time or late. **Undo** reverses a mistaken check-in.
Use the date controls to view past check-ins or future plans. Editing a repeating
task updates its schedule; deleting it removes all repeats and check-in history.

Keyboard shortcuts: **Ctrl+N** starts a new task; **Ctrl+S** saves the editor.

## Reminders

Opening the app starts its reminder process. Closing the window leaves reminders
running. Due, unfinished tasks notify every 15 minutes; mark them done in the app
to stop. Missed one-time tasks remind after restart; daily routines remind for
the current day. The computer must be awake and logged in. Desktop Do Not Disturb
settings can silence notifications. Times use local time.

To run only the reminder process:

```sh
/usr/bin/python3 app.py --reminders
```

## Application menu and optional login startup

The included `daily-check.desktop` is a launcher template. Replace
`/absolute/path/to/dailycheck/app.py` in its `Exec` line with the actual absolute
path to your checkout. If the path contains spaces, surround that path with
double quotes. Then install the edited launcher:

```sh
mkdir -p "$HOME/.local/share/applications"
cp daily-check.desktop "$HOME/.local/share/applications/daily-check.desktop"
```

To start reminders at login, copy the edited launcher to
`~/.config/autostart/daily-check-reminders.desktop`, change its name to
`Daily Check Reminders`, and append ` --reminders` to the `Exec` line. Use
`$XDG_CONFIG_HOME/autostart` instead if you configured `XDG_CONFIG_HOME`.
Login startup is optional; cloning the repository does not install it.

Update installed launchers if the checkout moves. To disable login reminders,
remove the autostart entry and stop the running `app.py --reminders` process.
Opening the app starts reminders again.

## Local data

Tasks and check-ins are stored in `~/.local/share/daily-check/tasks.sqlite3`, or
`$XDG_DATA_HOME/daily-check/tasks.sqlite3` when configured. Descriptions are stored
locally without encryption and appear in notifications. Personal task databases,
reminder locks, and Python cache files are excluded from this repository.

## Tests

Run from the checkout directory:

```sh
/usr/bin/python3 -m unittest discover -s . -v
```

Tests use temporary databases. The GUI test requires a graphical session and is
skipped when `DISPLAY` is unavailable. Tests cover recurring reminders, completion
and undo, missed one-time tasks, validation, deletion, and the task editor.
