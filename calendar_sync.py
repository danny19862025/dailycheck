"""Read-only local/online iCalendar connections with a private offline cache."""
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import threading
import time as clock
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, HTTPSHandler, HTTPRedirectHandler, build_opener

MAX_BYTES = 10 * 1024 * 1024
REFRESH_SECONDS = 900


def libraries():
    dependencies = str(Path(__file__).with_name('.calendar-deps'))
    if dependencies not in sys.path:
        sys.path.insert(0, dependencies)
    try:
        from icalendar import Calendar, Event
        import recurring_ical_events
    except ImportError:
        raise ValueError('Calendar support needs installation. Run: /usr/bin/python3 -m pip install --target .calendar-deps -r requirements-calendar.txt') from None
    return Calendar, Event, recurring_ical_events


def init_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS calendar_sources (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
        location TEXT NOT NULL UNIQUE, payload BLOB, checked TEXT NOT NULL DEFAULT '',
        updated TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '')''')


def normalize_location(kind, location):
    location = str(location).strip()
    if kind == 'local':
        path = Path(location).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() != '.ics':
            raise ValueError('Choose an existing .ics calendar file.')
        return str(path)
    if kind != 'cloud':
        raise ValueError('Choose a local file or an online calendar feed.')
    parts = urlsplit(location)
    if parts.scheme == 'webcal':
        parts = parts._replace(scheme='https')
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
        raise ValueError('Paste an HTTPS or webcal calendar feed URL, without a username or password.')
    return urlunsplit(parts._replace(fragment=''))


class SecureRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        normalize_location('cloud', newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def read_source(source):
    if source['kind'] == 'local':
        stream = Path(source['location']).open('rb')
    else:
        url = normalize_location('cloud', source['location'])
        request = Request(url, headers={'User-Agent': 'DailyCheck/0.2', 'Accept': 'text/calendar'})
        stream = build_opener(HTTPSHandler(), SecureRedirect()).open(request, timeout=15)
    with stream:
        payload = stream.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ValueError('Calendar exceeds the 10 MiB limit.')
    Calendar, _, _ = libraries()
    try:
        calendar = Calendar.from_ical(payload)
        if calendar.name != 'VCALENDAR':
            raise ValueError()
        for event in calendar.walk('VEVENT'):
            if 'DTSTART' not in event or 'UID' not in event or event.errors:
                raise ValueError()
    except Exception:
        raise ValueError('The source did not return a valid iCalendar (.ics) calendar.') from None
    return payload


class Calendars:
    def __init__(self, db):
        self.db = db
        self._cache_key, self._cache = None, []

    def sources(self):
        return [dict(row) for row in self.db.execute('SELECT id,name,kind,location,checked,updated,error FROM calendar_sources ORDER BY id')]

    def add(self, name, kind, location):
        name = str(name).strip()
        if not name or len(name) > 100:
            raise ValueError('Use a calendar name of 1–100 characters.')
        location = normalize_location(kind, location)
        with self.db:
            try:
                return self.db.execute('INSERT INTO calendar_sources(name,kind,location) VALUES(?,?,?)', (name, kind, location)).lastrowid
            except sqlite3.IntegrityError:
                raise ValueError('This calendar is already connected.') from None

    def remove(self, source_id):
        with self.db:
            self.db.execute('DELETE FROM calendar_sources WHERE id=?', (source_id,))

    def events(self, first, last):
        """Occurrences overlapping [first, last), converted to the desktop time zone."""
        if not 0 < (last - first).days <= 62:
            raise ValueError('Choose a calendar range of at most 62 days.')
        sources = self.db.execute('SELECT id,name,payload FROM calendar_sources WHERE payload IS NOT NULL ORDER BY id').fetchall()
        key = (first, last, tuple(tuple(row) for row in sources))
        if key == self._cache_key:
            return self._cache
        events = []
        if sources:
            Calendar, _, recurring = libraries()
            begin = datetime.combine(first, time.min).astimezone()
            finish = datetime.combine(last, time.min).astimezone()
            for source in sources:
                calendar = Calendar.from_ical(source['payload'])
                for event in recurring.of(calendar).between(begin, finish):
                    if str(event.get('STATUS', '')).upper() == 'CANCELLED':
                        continue
                    start = event.decoded('DTSTART')
                    all_day = not isinstance(start, datetime)
                    end = event.decoded('DTEND', None)
                    if end is None:
                        end = start + event.decoded('DURATION', timedelta(days=1) if all_day else timedelta())
                    if all_day:
                        start = datetime.combine(start, time.min)
                        end = datetime.combine(end, time.min)
                    else:
                        if start.tzinfo: start = start.astimezone().replace(tzinfo=None)
                        if end.tzinfo: end = end.astimezone().replace(tzinfo=None)
                    events.append(dict(source=source['id'], calendar=source['name'], uid=str(event.get('UID', '')),
                                       title=str(event.get('SUMMARY', 'Untitled event')), description=str(event.get('DESCRIPTION', '')),
                                       location=str(event.get('LOCATION', '')), start=start, end=end, all_day=all_day))
        self._cache_key = key
        self._cache = sorted(events, key=lambda event: (event['start'], event['calendar'], event['uid']))
        return self._cache


def on_day(events, day):
    begin = datetime.combine(day, time.min)
    finish = begin + timedelta(days=1)
    return [event for event in events if event['start'] < finish and (event['end'] > begin or event['start'] == event['end'] and event['start'] >= begin)]


def sync_sources(path, force=False):
    """Use a separate SQLite connection; never share Tk/service connections across threads."""
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        for source in Calendars(db).sources():
            now = datetime.now()
            if not force and source['checked'] and (now - datetime.fromisoformat(source['checked'])).total_seconds() < REFRESH_SECONDS:
                continue
            try:
                payload = read_source(source)
                with db:
                    db.execute('UPDATE calendar_sources SET payload=?,checked=?,updated=?,error=? WHERE id=?',
                               (payload, now.isoformat(timespec='seconds'), now.isoformat(timespec='seconds'), '', source['id']))
            except Exception as error:
                if isinstance(error, HTTPError):
                    message = f'Calendar server returned HTTP {error.code}. Check the feed link and access.'
                elif isinstance(error, ValueError):
                    message = str(error)
                elif isinstance(error, OSError) and source['kind'] == 'local':
                    message = 'Local calendar file could not be read. Reconnect it if it has moved.'
                else:
                    message = 'Calendar could not be downloaded. Check your connection and feed link.'
                with db:
                    # Preserve the last good cache when a source is temporarily unavailable.
                    db.execute('UPDATE calendar_sources SET checked=?,error=? WHERE id=?', (now.isoformat(timespec='seconds'), message, source['id']))
    finally:
        db.close()


class SyncWorker:
    def __init__(self, path):
        self.path, self.thread, self.last_started, self.error = path, None, -REFRESH_SECONDS, ''

    def start(self, force=False):
        if self.thread and self.thread.is_alive(): return False
        if not force and clock.monotonic() - self.last_started < REFRESH_SECONDS: return False
        self.last_started = clock.monotonic()
        self.error = ''

        def work():
            try: sync_sources(self.path, force)
            except Exception: self.error = 'Calendar refresh failed. Try Refresh calendars again.'

        self.thread = threading.Thread(target=work, daemon=True, name='calendar-refresh')
        self.thread.start()
        return True


def export_month(store, month):
    Calendar, Event, _ = libraries()
    calendar = Calendar()
    calendar.add('prodid', '-//Daily Check//Calendar Export//EN')
    calendar.add('version', '2.0')
    calendar.add('x-wr-calname', 'Daily Check')
    day = month.replace(day=1)
    while day.month == month.month:
        for row in store.rows(day.isoformat()):
            if row['occurrence'] != day.isoformat(): continue
            event = Event()
            event.add('uid', f'dailycheck-{row["id"]}-{day.isoformat()}-{row["slot"].replace(":", "")}@dailycheck.local')
            event.add('dtstamp', datetime.now(timezone.utc))
            event.add('dtstart', datetime.fromisoformat(row['due']))
            event.add('dtend', datetime.fromisoformat(row['due']) + timedelta(minutes=15))
            event.add('summary', row['title'])
            event.add('description', row['description'])
            event.add('categories', [row['category']])
            calendar.add_component(event)
        day += timedelta(days=1)
    return calendar.to_ical()
