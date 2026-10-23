"""Private SQLite storage, recurrence, per-occurrence check-ins, and portable backups."""
from datetime import date, datetime, timedelta
import calendar
from contextlib import closing
import csv
import io
import json
import os
from pathlib import Path
import sqlite3
import uuid

VERSION = 2
REPEATS = ('once', 'daily', 'weekly', 'monthly')
PRIORITIES = ('Low', 'Med', 'High')


def timestamp(value=None):
    return (value or datetime.now()).isoformat(timespec='seconds')


def validate(title, description, day, time, daily, **options):
    title, description = str(title).strip(), str(description).strip()
    if not title or len(title) > 300 or len(description) > 65536:
        raise ValueError('Use a task name of 1–300 characters and details under 64 KiB.')
    try:
        start = date.fromisoformat(str(day))
        raw_times = options.get('times') or str(time).split(',')
        if isinstance(raw_times, str):
            raw_times = raw_times.split(',')
        times = sorted(set(datetime.strptime(str(t).strip(), '%H:%M').strftime('%H:%M') for t in raw_times))
        if not 1 <= len(times) <= 12:
            raise ValueError()
        end = options.get('end_day') or ''
        if end and date.fromisoformat(str(end)) < start:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('Use YYYY-MM-DD dates and 1–12 comma-separated HH:MM times. End date must follow start date.') from None
    kind = options.get('repeat_kind', 'daily' if daily else 'once')
    priority = options.get('priority', 'Med')
    try:
        interval = int(options.get('interval', 1))
        weekdays = sorted(set(int(n) for n in options.get('weekdays', [start.weekday()])))
        before = int(options.get('focus_before', 0))
        after = int(options.get('focus_after', 60))
        if kind not in REPEATS or priority not in PRIORITIES or not 1 <= interval <= 365 or not all(0 <= n <= 6 for n in weekdays):
            raise ValueError()
        if kind == 'weekly' and not weekdays:
            raise ValueError()
        if not 0 <= before <= 1440 or not 1 <= after <= 10080:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('Check repeat interval, weekdays, priority, and focus-window minutes.') from None
    tags = options.get('tags', [])
    if isinstance(tags, str):
        tags = tags.split(',')
    tags = list(dict.fromkeys(str(t).strip() for t in tags if str(t).strip()))
    category = str(options.get('category', 'Home')).strip() or 'Home'
    if len(tags) > 20 or any(len(t) > 60 for t in tags) or len(category) > 60:
        raise ValueError('Use up to 20 tags and names of at most 60 characters.')
    return dict(title=title, description=description, day=start.isoformat(), time=times[0], daily=int(kind != 'once'),
                priority=priority, category=category, tags=tags, repeat_kind=kind, interval=interval,
                weekdays=weekdays, end_day=str(end), times=times, focus=int(bool(options.get('focus', False))),
                focus_before=before, focus_after=after)


def occurs(task, day):
    day = date.fromisoformat(day) if isinstance(day, str) else day
    start = date.fromisoformat(task['day'])
    if day < start or (task['end_day'] and day > date.fromisoformat(task['end_day'])):
        return False
    interval, kind = task['interval'], task['repeat_kind']
    if kind == 'once':
        return day == start
    if kind == 'daily':
        return (day - start).days % interval == 0
    if kind == 'weekly':
        monday = start - timedelta(days=start.weekday())
        return ((day - monday).days // 7) % interval == 0 and day.weekday() in task['weekdays']
    months = (day.year - start.year) * 12 + day.month - start.month
    return months % interval == 0 and day.day == min(start.day, calendar.monthrange(day.year, day.month)[1])


def checklist(description, previous=()):
    """Markdown checklist definitions; completion belongs to an occurrence, not the template."""
    import re
    available = {}
    for item in previous:
        available.setdefault(item['text'], []).append(item['id'])
    result = []
    for line in description.splitlines():
        match = re.match(r'^\s*(?:[-*]\s+)?\[[ xX]\]\s+(.+?)\s*$', line)
        if match:
            text = match[1]
            ids = available.get(text, [])
            result.append({'id': ids.pop(0) if ids else uuid.uuid4().hex, 'text': text})
    return result


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        old = self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone()
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version > VERSION:
            self.db.close()
            raise ValueError('This database needs a newer Daily Check version.')
        if old and version < VERSION:
            self.backup(self.path.parent / 'backups' / ('before-upgrade-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.sqlite3'))
        self.migrate()
        os.chmod(self.path, 0o600)
        self.auto_backup()

    def migrate(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('''CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL,
                description TEXT NOT NULL, day TEXT NOT NULL, time TEXT NOT NULL, daily INTEGER NOT NULL)''')
            columns = {r['name'] for r in self.db.execute('PRAGMA table_info(tasks)')}
            additions = dict(priority="TEXT NOT NULL DEFAULT 'Med'", category="TEXT NOT NULL DEFAULT 'Home'",
                             tags="TEXT NOT NULL DEFAULT '[]'", repeat_kind="TEXT NOT NULL DEFAULT 'once'",
                             interval='INTEGER NOT NULL DEFAULT 1', weekdays="TEXT NOT NULL DEFAULT '[]'",
                             end_day="TEXT NOT NULL DEFAULT ''", times="TEXT NOT NULL DEFAULT '[]'",
                             checklist="TEXT NOT NULL DEFAULT '[]'", focus='INTEGER NOT NULL DEFAULT 0',
                             focus_before='INTEGER NOT NULL DEFAULT 0', focus_after='INTEGER NOT NULL DEFAULT 60')
            for name, schema in additions.items():
                if name not in columns:
                    self.db.execute(f'ALTER TABLE tasks ADD COLUMN {name} {schema}')
            if 'repeat_kind' not in columns:
                for r in self.db.execute('SELECT * FROM tasks').fetchall():
                    self.db.execute('UPDATE tasks SET repeat_kind=?,times=?,weekdays=?,checklist=? WHERE id=?',
                                    ('daily' if r['daily'] else 'once', json.dumps([r['time']]), json.dumps([date.fromisoformat(r['day']).weekday()]), json.dumps(checklist(r['description'])), r['id']))
            check_columns = {r['name'] for r in self.db.execute('PRAGMA table_info(checks)')}
            if check_columns and 'slot' not in check_columns:
                self.db.execute('ALTER TABLE checks RENAME TO legacy_checks')
            self.db.execute('''CREATE TABLE IF NOT EXISTS checks (task INTEGER NOT NULL, day TEXT NOT NULL,
                slot TEXT NOT NULL, done TEXT, reminded TEXT, snoozed TEXT, elapsed INTEGER NOT NULL DEFAULT 0,
                started TEXT, checklist_done TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(task,day,slot))''')
            if check_columns and 'slot' not in check_columns:
                self.db.execute('''INSERT INTO checks(task,day,slot,done,reminded)
                    SELECT c.task,c.day,t.time,c.done,c.reminded FROM legacy_checks c JOIN tasks t ON t.id=c.task''')
                self.db.execute('DROP TABLE legacy_checks')
            self.db.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            from calendar_sync import init_schema
            init_schema(self.db)
            self.db.execute(f'PRAGMA user_version={VERSION}')

    def settings(self):
        values = dict(theme='System', sound='system', tts=False, lock_guard=True)
        values.update({r['key']: json.loads(r['value']) for r in self.db.execute('SELECT * FROM settings')})
        return values

    def set_settings(self, **values):
        allowed = {'theme', 'sound', 'tts', 'lock_guard'}
        if set(values) - allowed or values.get('theme', 'System') not in ('System', 'Light', 'Dark'):
            raise ValueError('Invalid settings')
        with self.db:
            for key, value in values.items():
                if key in ('tts', 'lock_guard') and not isinstance(value, bool):
                    raise ValueError('Use a boolean setting')
                if key == 'sound' and (not isinstance(value, str) or (value and value != 'system' and not Path(value).is_file())):
                    raise ValueError('Choose an existing sound file')
                self.db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(value)))

    @staticmethod
    def decode(row):
        task = dict(row)
        for key in ('tags', 'weekdays', 'times', 'checklist'):
            task[key] = json.loads(task[key])
        return task

    def tasks(self):
        return [self.decode(r) for r in self.db.execute('SELECT * FROM tasks ORDER BY time,id')]

    def task(self, task_id):
        row = self.db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise ValueError('This task no longer exists.')
        return self.decode(row)

    def save(self, title, description, day, time, daily=True, task_id=None, **options):
        task = validate(title, description, day, time, daily, **options)
        previous = self.task(task_id)['checklist'] if task_id is not None else []
        task['checklist'] = checklist(task['description'], previous)
        if len(task['checklist']) > 200:
            raise ValueError('Use at most 200 checklist items per task.')
        with self.db:
            return self._write_task(task, task_id)

    def _write_task(self, task, task_id=None):
        encoded = {k: json.dumps(v) if isinstance(v, list) else v for k, v in task.items()}
        if task_id is None:
            names = ','.join(encoded)
            return self.db.execute(f'INSERT INTO tasks({names}) VALUES({",".join("?" for _ in encoded)})', tuple(encoded.values())).lastrowid
        self.db.execute(f'UPDATE tasks SET {",".join(k+"=?" for k in encoded)} WHERE id=?', (*encoded.values(), task_id))
        return task_id

    def occurrence(self, task_id, day, slot=None, allow_history=True):
        day = date.fromisoformat(day).isoformat()
        task = self.task(task_id)
        slot = slot or task['times'][0]
        saved = self.db.execute('SELECT * FROM checks WHERE task=? AND day=? AND slot=?', (task_id, day, slot)).fetchone()
        if not (occurs(task, day) and slot in task['times']) and not (saved and allow_history):
            raise ValueError('This reminder is no longer scheduled.')
        row = dict(task, occurrence=day, slot=slot, due=day + 'T' + slot,
                   done=None, reminded=None, snoozed=None, elapsed=0, started=None, checklist_done=[])
        if saved:
            row.update(dict(saved))
            row['checklist_done'] = json.loads(row['checklist_done'])
        row['id'] = task_id
        row['time'] = slot
        return row

    @staticmethod
    def status(row, now):
        if row['done']:
            return 'done'
        return 'late' if now > datetime.fromisoformat(row['due']) else 'pending'

    def rows(self, day, search='', state='All', category='All', tag='All', now=None):
        now = now or datetime.now()
        date.fromisoformat(day)
        rows = []
        for task in self.tasks():
            slots = set(task['times']) if occurs(task, day) else set()
            slots.update(r['slot'] for r in self.db.execute('SELECT slot FROM checks WHERE task=? AND day=? AND done IS NOT NULL', (task['id'], day)))
            for slot in sorted(slots):
                rows.append(self.occurrence(task['id'], day, slot))
        # A snoozed repeat from yesterday remains actionable today, independently of today's reminder.
        for r in self.db.execute('SELECT task,day,slot FROM checks WHERE done IS NULL AND day<>? AND snoozed LIKE ?', (day, day + '%')):
            task = self.task(r['task'])
            if r['slot'] in task['times'] and occurs(task, r['day']):
                rows.append(self.occurrence(r['task'], r['day'], r['slot']))
        def matches(row):
            haystack = ' '.join([row['title'], row['description'], row['category'], *row['tags']]).casefold()
            actual = self.status(row, now)
            return (not search or search.casefold() in haystack) and (category == 'All' or row['category'] == category) and (tag == 'All' or tag in row['tags']) and (state == 'All' or (state == 'Pending' and not row['done']) or state.casefold() == actual)
        return sorted(filter(matches, rows), key=lambda r: (r['time'], -PRIORITIES.index(r['priority']), r['id']))

    def _ensure(self, task, day, slot):
        self.db.execute('INSERT OR IGNORE INTO checks(task,day,slot) VALUES(?,?,?)', (task, day, slot))

    def complete(self, task, day, now=None, slot=None, locked=False):
        now = now or datetime.now()
        row = self.occurrence(task, day, slot)
        if row['done']:
            return
        if date.fromisoformat(day) > now.date():
            raise ValueError('Check in on the day of the task.')
        if locked and self.settings()['lock_guard']:
            raise ValueError('Unlock your screen before marking a task done.')
        anchor = datetime.fromisoformat(row['snoozed'] or row['due'])
        if row['focus'] and not anchor - timedelta(minutes=row['focus_before']) <= now <= anchor + timedelta(minutes=row['focus_after']):
            raise ValueError(f"Focus mode: check in between {anchor - timedelta(minutes=row['focus_before']):%d %b %H:%M} and {anchor + timedelta(minutes=row['focus_after']):%d %b %H:%M}.")
        elapsed = self.elapsed(row, now)
        with self.db:
            self._ensure(task, day, row['slot'])
            self.db.execute('''UPDATE checks SET done=?,snoozed=NULL,elapsed=?,started=NULL,checklist_done=?
                WHERE task=? AND day=? AND slot=? AND done IS NULL''', (timestamp(now), elapsed, json.dumps([i['id'] for i in row['checklist']]), task, day, row['slot']))

    def undo(self, task, day, slot=None):
        row = self.occurrence(task, day, slot)
        with self.db:
            self.db.execute('UPDATE checks SET done=NULL,reminded=NULL,snoozed=NULL WHERE task=? AND day=? AND slot=?', (task, day, row['slot']))

    def delete(self, task):
        with self.db:
            self.db.execute('DELETE FROM checks WHERE task=?', (task,))
            self.db.execute('DELETE FROM tasks WHERE id=?', (task,))

    def pending(self, now):
        candidates = self.rows(now.date().isoformat(), now=now)
        for task in self.tasks():
            if task['repeat_kind'] == 'once' and task['day'] < now.date().isoformat():
                candidates.extend(self.occurrence(task['id'], task['day'], slot) for slot in task['times'])
        for r in self.db.execute('SELECT task,day,slot FROM checks WHERE done IS NULL AND snoozed IS NOT NULL AND snoozed<=?', (timestamp(now),)):
            candidates.append(self.occurrence(r['task'], r['day'], r['slot']))
        seen, result = set(), []
        for row in candidates:
            key = row['id'], row['occurrence'], row['slot']
            if key in seen:
                continue
            seen.add(key)
            # Retired slots remain in history, but must never generate new reminders.
            task = self.task(row['id'])
            if row['slot'] not in task['times'] or not occurs(task, row['occurrence']):
                continue
            if row['done'] or now < datetime.fromisoformat(row['snoozed'] or row['due']):
                continue
            if not row['reminded'] or now - datetime.fromisoformat(row['reminded']) >= timedelta(minutes=15):
                result.append(row)
        return result

    def notified(self, task, day, now, slot=None):
        row = self.occurrence(task, day, slot)
        with self.db:
            self._ensure(task, day, row['slot'])
            self.db.execute('UPDATE checks SET reminded=? WHERE task=? AND day=? AND slot=?', (timestamp(now), task, day, row['slot']))

    def snooze(self, task, day, duration, now=None, slot=None):
        now = now or datetime.now()
        row = self.occurrence(task, day, slot)
        if row['done']:
            raise ValueError('This reminder is already complete.')
        if duration == 'tomorrow':
            until = datetime.combine(now.date() + timedelta(days=1), datetime.strptime(row['slot'], '%H:%M').time())
        elif duration in ('10m', '1h'):
            until = now + timedelta(minutes=10 if duration == '10m' else 60)
        else:
            raise ValueError('Choose 10m, 1h, or tomorrow.')
        with self.db:
            self._ensure(task, day, row['slot'])
            self.db.execute('UPDATE checks SET snoozed=?,reminded=NULL WHERE task=? AND day=? AND slot=?', (timestamp(until), task, day, row['slot']))
        return until

    @staticmethod
    def elapsed(row, now=None):
        return row['elapsed'] + (max(0, int(((now or datetime.now()) - datetime.fromisoformat(row['started'])).total_seconds())) if row['started'] else 0)

    def track(self, task, day, slot=None, running=True, now=None):
        now = now or datetime.now()
        row = self.occurrence(task, day, slot)
        if row['done']:
            raise ValueError('Undo the check-in before tracking more time.')
        with self.db:
            if running:
                for active in self.db.execute('SELECT * FROM checks WHERE started IS NOT NULL').fetchall():
                    elapsed = self.elapsed(active, now)
                    self.db.execute('UPDATE checks SET elapsed=?,started=NULL WHERE task=? AND day=? AND slot=?', (elapsed, active['task'], active['day'], active['slot']))
            row = self.occurrence(task, day, slot)
            self._ensure(task, day, row['slot'])
            self.db.execute('UPDATE checks SET elapsed=?,started=? WHERE task=? AND day=? AND slot=?', (self.elapsed(row, now), timestamp(now) if running else None, task, day, row['slot']))

    def toggle_subtask(self, task, day, subtask, done, slot=None):
        row = self.occurrence(task, day, slot)
        if row['done'] or subtask not in {s['id'] for s in row['checklist']}:
            raise ValueError('Choose an unfinished task and an existing checklist item.')
        values = set(row['checklist_done'])
        values.add(subtask) if done else values.discard(subtask)
        with self.db:
            self._ensure(task, day, row['slot'])
            self.db.execute('UPDATE checks SET checklist_done=? WHERE task=? AND day=? AND slot=?', (json.dumps(sorted(values)), task, day, row['slot']))

    def statistics(self, now=None, task_id=None):
        now = now or datetime.now()
        today = now.date()
        tasks = [t for t in self.tasks() if task_id is None or t['id'] == task_id]
        first = min((date.fromisoformat(t['day']) for t in tasks), default=today)
        # Daily statistics are bounded to the last ten years; task schedules have no such limit.
        first = max(first, today - timedelta(days=3660))
        completed_days, scheduled_days, week, month, tracked = set(), [], [0, 0], [0, 0], 0
        monday = today - timedelta(days=today.weekday())
        month_start = today.replace(day=1)
        task_ids = {t['id'] for t in tasks}
        checks = {(r['task'], r['day'], r['slot']): dict(r) for r in self.db.execute('SELECT * FROM checks WHERE day>=? AND day<=?', (first.isoformat(), today.isoformat())) if r['task'] in task_ids}
        by_day = {}
        for key, saved in checks.items():
            by_day.setdefault(key[1], []).append(key)
            tracked += self.elapsed(saved, now)
        day = first
        while day <= today:
            occurrences = [(t, slot) for t in tasks if occurs(t, day) for slot in t['times']]
            keys = {(t['id'], day.isoformat(), slot): day.isoformat() + 'T' + slot for t, slot in occurrences}
            keys.update({key: key[1] + 'T' + key[2] for key in by_day.get(day.isoformat(), [])})
            all_done = bool(keys) and all(checks.get(k, {}).get('done') for k in keys)
            due_today = any(datetime.fromisoformat(due) <= now for due in keys.values())
            if keys and (day < today or due_today or all_done):
                scheduled_days.append(day)
                if all_done:
                    completed_days.add(day)
            for key, due in keys.items():
                saved = checks.get(key, {})
                if datetime.fromisoformat(due) > now:
                    continue
                on_time = bool(saved.get('done') and saved['done'] <= due + ':00')
                if day >= monday:
                    week[0] += int(on_time); week[1] += 1
                if day >= month_start:
                    month[0] += int(on_time); month[1] += 1
            day += timedelta(days=1)
        current = best = count = 0
        for day in scheduled_days:
            count = count + 1 if day in completed_days else 0
            best = max(best, count)
        for day in reversed(scheduled_days):
            if day == today and day not in completed_days:
                continue  # Today's streak is still in progress until the day ends.
            if day not in completed_days:
                break
            current += 1
        return dict(current=current, best=best, week=week, month=month, tracked=tracked,
                    week_percent=round(100 * week[0] / week[1]) if week[1] else 0,
                    month_percent=round(100 * month[0] / month[1]) if month[1] else 0)

    def protect_database(self, path):
        target = Path(path).resolve()
        if target in {Path(str(self.path.resolve()) + suffix) for suffix in ('', '-wal', '-shm', '-journal')}:
            raise ValueError('Choose a destination other than the active task database.')

    def backup(self, path):
        path = Path(path)
        self.protect_database(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with closing(sqlite3.connect(temporary)) as target:
                self.db.backup(target)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def auto_backup(self, today=None):
        directory = self.path.parent / 'backups'
        destination = directory / f'tasks-{today or date.today().isoformat()}.sqlite3'
        if not destination.exists():
            self.backup(destination)
            for old in sorted(directory.glob('tasks-????-??-??.sqlite3'))[:-30]:
                old.unlink(missing_ok=True)
        return destination

    def export_file(self, path):
        path = Path(path)
        self.protect_database(path)
        tasks = self.tasks()
        if path.suffix.lower() == '.csv':
            buffer = io.StringIO(newline='')
            names = ['title', 'description', 'day', 'times', 'repeat_kind', 'interval', 'weekdays', 'end_day', 'priority', 'category', 'tags', 'focus', 'focus_before', 'focus_after']
            writer = csv.DictWriter(buffer, fieldnames=names)
            writer.writeheader()
            for task in tasks:
                writer.writerow({k: json.dumps(task[k]) if isinstance(task[k], list) else task[k] for k in names})
            contents = buffer.getvalue()
        else:
            checks = []
            for row in self.db.execute('SELECT * FROM checks'):
                saved = dict(row)
                saved['elapsed'] = self.elapsed(saved)
                saved['started'] = None
                checks.append(saved)
            contents = json.dumps(dict(format='dailycheck', version=VERSION, tasks=tasks,
                                       checks=checks), indent=2, ensure_ascii=False)
        temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8', newline='') as out:
                os.chmod(temporary, 0o600)
                out.write(contents)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def import_file(self, path):
        try:
            return self._import_file(path)
        except (KeyError, TypeError, AttributeError, IndexError, OverflowError, csv.Error) as error:
            raise ValueError('Invalid Daily Check export: check its fields and data types.') from error

    def _import_file(self, path):
        path = Path(path)
        if path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError('Imports are limited to 10 MiB.')
        raw = path.read_text(encoding='utf-8-sig')
        if path.suffix.lower() == '.csv':
            raw_tasks = list(csv.DictReader(io.StringIO(raw)))
            raw_checks = []
            for i, task in enumerate(raw_tasks):
                task['id'] = i
                for key in ('times', 'weekdays', 'tags'):
                    task[key] = json.loads(task[key])
                task['focus'] = bool(int(task.get('focus', '0')))
        else:
            data = json.loads(raw)
            if data.get('format') != 'dailycheck' or data.get('version') != VERSION:
                raise ValueError('Choose a Daily Check version 2 JSON export or CSV task export.')
            raw_tasks, raw_checks = data['tasks'], data['checks']
        if not isinstance(raw_tasks, list) or not isinstance(raw_checks, list) or len(raw_tasks) > 10000 or len(raw_checks) > 100000:
            raise ValueError('Import exceeds the task or check-in limit.')
        tasks, ids = [], set()
        for source in raw_tasks:
            task_id = int(source['id'])
            if task_id in ids:
                raise ValueError('Duplicate task IDs in import.')
            ids.add(task_id)
            options = {key: source[key] for key in ('times', 'repeat_kind', 'interval', 'weekdays', 'end_day', 'priority', 'category', 'tags', 'focus', 'focus_before', 'focus_after') if key in source}
            task = validate(source['title'], source['description'], source['day'], source.get('time', '09:00'), source.get('daily', False), **options)
            previous = source.get('checklist', [])
            if not isinstance(previous, list) or any(not isinstance(i, dict) or not isinstance(i.get('id'), str) or not isinstance(i.get('text'), str) for i in previous):
                raise ValueError('Invalid checklist import.')
            task['checklist'] = checklist(task['description'], previous)
            tasks.append((task_id, task))
        checks, keys = [], set()
        for source in raw_checks:
            task_id = int(source['task'])
            if task_id not in ids:
                raise ValueError('A check-in refers to a missing task.')
            day = date.fromisoformat(source['day']).isoformat()
            slot = datetime.strptime(source['slot'], '%H:%M').strftime('%H:%M')
            key = task_id, day, slot
            if key in keys:
                raise ValueError('Duplicate check-in in import.')
            keys.add(key)
            checked = json.loads(source.get('checklist_done', '[]'))
            if not isinstance(checked, list) or len(checked) > 1000 or any(not isinstance(i, str) or len(i) > 64 for i in checked):
                raise ValueError('Invalid checklist check-in.')
            stamps = []
            for field in ('done', 'reminded', 'snoozed'):
                value = source.get(field)
                stamps.append(datetime.fromisoformat(value).isoformat(timespec='seconds') if value else None)
                if value and datetime.fromisoformat(value).tzinfo is not None:
                    raise ValueError('Check-in times must use local time without a timezone suffix.')
            elapsed = int(source.get('elapsed', 0))
            if not 0 <= elapsed <= 10**9:
                raise ValueError('Invalid tracking duration.')
            # Freeze an exported active timer at the saved elapsed value; never resume it during import.
            checks.append((*key, *stamps, elapsed, json.dumps(checked)))
        self.backup(self.path.parent / 'backups' / ('before-import-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.sqlite3'))
        with self.db:
            mapping = {old: self._write_task(task) for old, task in tasks}
            self.db.executemany('INSERT INTO checks(task,day,slot,done,reminded,snoozed,elapsed,checklist_done) VALUES(?,?,?,?,?,?,?,?)',
                                [(mapping[r[0]], *r[1:]) for r in checks])
        return len(tasks)
