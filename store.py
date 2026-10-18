"""Local task storage and reminder scheduling."""
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
                day TEXT NOT NULL, time TEXT NOT NULL, daily INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS checks (
                task INTEGER, day TEXT, done TEXT, reminded TEXT,
                PRIMARY KEY(task, day));
        ''')

    def save(self, title, description, day, time, daily, task_id=None):
        if not title.strip():
            raise ValueError('Please give your task a name.')
        try:
            due = datetime.strptime(day + ' ' + time, '%Y-%m-%d %H:%M')
        except ValueError:
            raise ValueError('Use a date like 2026-09-28 and a time like 09:30.') from None
        values = (title.strip(), description.strip(), due.strftime('%Y-%m-%d'), due.strftime('%H:%M'), int(daily))
        with self.db:
            if task_id is None:
                return self.db.execute('INSERT INTO tasks(title,description,day,time,daily) VALUES(?,?,?,?,?)', values).lastrowid
            self.db.execute('UPDATE tasks SET title=?,description=?,day=?,time=?,daily=? WHERE id=?', (*values, task_id))
        return task_id

    def rows(self, day):
        return self.db.execute('''SELECT t.*, c.done, c.reminded FROM tasks t
            LEFT JOIN checks c ON c.task=t.id AND c.day=?
            WHERE t.day=? OR (t.daily=1 AND t.day<=?) ORDER BY t.time,t.id''', (day, day, day)).fetchall()

    def complete(self, task, day, now=None):
        with self.db:
            self.db.execute('''INSERT INTO checks(task,day,done) VALUES(?,?,?)
                ON CONFLICT(task,day) DO UPDATE SET done=excluded.done''',
                (task, day, (now or datetime.now()).isoformat(timespec='seconds')))

    def undo(self, task, day):
        with self.db:
            self.db.execute('UPDATE checks SET done=NULL,reminded=NULL WHERE task=? AND day=?', (task, day))

    def delete(self, task):
        with self.db:
            self.db.execute('DELETE FROM checks WHERE task=?', (task,))
            self.db.execute('DELETE FROM tasks WHERE id=?', (task,))

    def pending(self, now):
        # Include missed one-off tasks after a restart; daily reminders are for today.
        day = now.date().isoformat()
        rows = self.db.execute('''SELECT t.*, CASE WHEN t.daily=1 THEN ? ELSE t.day END AS occurrence,
            c.done,c.reminded FROM tasks t LEFT JOIN checks c ON c.task=t.id
            AND c.day=CASE WHEN t.daily=1 THEN ? ELSE t.day END WHERE t.day<=?''', (day, day, day))
        return [r for r in rows if not r['done']
                and datetime.fromisoformat(r['occurrence'] + 'T' + r['time']) <= now
                and (not r['reminded'] or now - datetime.fromisoformat(r['reminded']) >= timedelta(minutes=15))]

    def notified(self, task, day, now):
        with self.db:
            self.db.execute('''INSERT INTO checks(task,day,reminded) VALUES(?,?,?)
                ON CONFLICT(task,day) DO UPDATE SET reminded=excluded.reminded''',
                (task, day, now.isoformat(timespec='seconds')))
