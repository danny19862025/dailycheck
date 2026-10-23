"""Calendar connections, real iCalendar recurrence, cache behavior, and GUI workflow."""
from datetime import date, datetime, timezone
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from calendar_sync import Calendars, SyncWorker, export_month, libraries, normalize_location, on_day, sync_sources
from store import Store

FEED = b'''BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Daily Check Test//EN\r
BEGIN:VEVENT\r
UID:meeting\r
DTSTART:20261023T090000\r
DTEND:20261023T100000\r
RRULE:FREQ=DAILY;COUNT=3\r
EXDATE:20261024T090000\r
SUMMARY:Team meeting\r
DESCRIPTION:First line\\nSecond line\\, with comma\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:holiday\r
DTSTART;VALUE=DATE:20261023\r
DTEND;VALUE=DATE:20261025\r
SUMMARY:Two day break\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:utc\r
DTSTART:20261023T230000Z\r
DTEND:20261024T000000Z\r
SUMMARY:UTC call\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:cancelled\r
DTSTART:20261023T110000\r
STATUS:CANCELLED\r
SUMMARY:Cancelled event\r
END:VEVENT\r
END:VCALENDAR\r
'''


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'tasks.sqlite3'); self.addCleanup(self.store.db.close)
        self.calendars = Calendars(self.store.db)
        self.file = self.root / 'Local calendar.ics'; self.file.write_bytes(FEED)

    def events(self): return self.calendars.events(date(2026, 10, 1), date(2026, 11, 1))

    def test_local_refresh_recurrence_exclusion_all_day_timezone_and_disconnect(self):
        source_id = self.calendars.add('Local', 'local', self.file)
        sync_sources(self.store.path)
        events = self.events()
        meetings = [event for event in events if event['uid'] == 'meeting']
        self.assertEqual([event['start'].day for event in meetings], [23, 25])
        self.assertEqual(meetings[0]['description'], 'First line\nSecond line, with comma')
        self.assertFalse(any(event['uid'] == 'cancelled' for event in events))
        self.assertTrue(any(event['uid'] == 'holiday' for event in on_day(events, date(2026, 10, 24))))
        self.assertFalse(any(event['uid'] == 'holiday' for event in on_day(events, date(2026, 10, 25))))
        utc = next(event for event in events if event['uid'] == 'utc')
        self.assertEqual(utc['start'], datetime(2026, 10, 23, 23, tzinfo=timezone.utc).astimezone().replace(tzinfo=None))
        with patch('calendar_sync.libraries', side_effect=AssertionError('Unchanged cache was parsed again')):
            self.assertIs(events, self.events())
        self.file.write_bytes(FEED.replace(b'Team meeting', b'Renamed meeting'))
        sync_sources(self.store.path, force=True)
        self.assertTrue(any(event['title'] == 'Renamed meeting' for event in self.events()))
        self.calendars.remove(source_id)
        self.assertEqual(self.events(), [])
        self.assertTrue(self.file.exists())

    def test_failed_download_preserves_cache_and_does_not_leak_secret(self):
        url = 'https://example.com/private-secret/basic.ics'
        self.calendars.add('Google', 'cloud', url)
        opener = Mock(); opener.open.return_value = io.BytesIO(FEED)
        with patch('calendar_sync.build_opener', return_value=opener): sync_sources(self.store.path, force=True)
        self.assertEqual(len(self.events()), 4)
        error = HTTPError(url, 403, 'secret', None, io.BytesIO())
        self.addCleanup(error.close)
        opener.open.side_effect = error
        with patch('calendar_sync.build_opener', return_value=opener): sync_sources(self.store.path, force=True)
        source = self.calendars.sources()[0]
        self.assertIn('403', source['error']); self.assertNotIn('private-secret', source['error'])
        self.assertEqual(len(self.events()), 4)
        self.assertTrue(source['updated'])

    def test_invalid_feed_retains_data_and_repeated_sync_is_throttled(self):
        self.calendars.add('Local', 'local', self.file)
        sync_sources(self.store.path)
        self.file.write_bytes(b'<html>Sign in</html>')
        with patch('calendar_sync.read_source', side_effect=AssertionError('Unexpected background refresh')):
            sync_sources(self.store.path)
        sync_sources(self.store.path, force=True)
        self.assertIn('valid iCalendar', self.calendars.sources()[0]['error'])
        self.assertEqual(len(self.events()), 4)
        self.file.unlink()
        sync_sources(self.store.path, force=True)
        self.assertIn('could not be read', self.calendars.sources()[0]['error'])

    def test_validation_duplicate_and_secure_webcal(self):
        self.assertEqual(normalize_location('cloud', 'webcal://example.com/calendar.ics'), 'https://example.com/calendar.ics')
        for location in ['http://example.com/a.ics', 'file:///tmp/a.ics', 'https://user:password@example.com/a.ics', 'not a URL']:
            with self.assertRaises(ValueError): self.calendars.add('Bad', 'cloud', location)
        self.calendars.add('Local', 'local', self.file)
        with self.assertRaisesRegex(ValueError, 'already connected'): self.calendars.add('Again', 'local', self.file)
        with self.assertRaises(ValueError): self.calendars.add('', 'local', self.file)

    def test_export_round_trips_multislot_and_unicode_without_external_events(self):
        self.store.save('Meeting, café; notes', 'First\nSecond', '2026-10-23', '09:00, 17:00', False)
        self.calendars.add('Local', 'local', self.file); sync_sources(self.store.path)
        payload = export_month(self.store, date(2026, 10, 1))
        Calendar, _, _ = libraries()
        events = Calendar.from_ical(payload).walk('VEVENT')
        self.assertEqual(len(events), 2)
        self.assertEqual(str(events[0]['SUMMARY']), 'Meeting, café; notes')
        self.assertEqual(str(events[0]['DESCRIPTION']), 'First\nSecond')
        self.assertEqual(len({str(event['UID']) for event in events}), 2)
        second = Calendar.from_ical(export_month(self.store, date(2026, 10, 1))).walk('VEVENT')
        self.assertEqual([str(event['UID']) for event in events], [str(event['UID']) for event in second])

    def test_worker_prevents_overlapping_refreshes(self):
        worker = SyncWorker(self.store.path)
        with patch('calendar_sync.threading.Thread') as thread:
            self.assertTrue(worker.start())
            thread.return_value.is_alive.return_value = True
            self.assertFalse(worker.start(force=True))
            self.assertEqual(thread.call_count, 1)

    @unittest.skipUnless(os.environ.get('DISPLAY'), 'Requires display')
    def test_gui_connection_agenda_reminder_and_export(self):
        from app import App
        app = App(self.store); self.addCleanup(app.destroy)
        app.calendar_month = date(2026, 10, 1)
        app.calendar_day = date(2026, 10, 23)
        app.notebook.select(app.calendar_tab); app.update()
        app.connect_calendars(); app.update()
        with patch('calendar_ui.filedialog.askopenfilename', return_value=str(self.file)):
            app.add_local_calendar()
        deadline = time.monotonic() + 5
        while app.calendar_poll_job is not None and time.monotonic() < deadline:
            app.update(); time.sleep(.02)
        self.assertIsNone(app.calendar_poll_job)
        self.assertIn('1 calendar(s) connected', app.calendar_connection_status.get())
        self.assertEqual(len(app.connections_tree.get_children()), 1)
        key = next(key for key, (kind, event) in app.agenda_rows.items() if kind == 'event' and event['uid'] == 'meeting')
        app.calendar_agenda.selection_set(key); app.reminder_from_calendar()
        self.assertEqual(app.title_var.get(), 'Team meeting')
        self.assertEqual(app.date_var.get(), '2026-10-23'); self.assertEqual(app.time_var.get(), '09:00')
        self.assertEqual(app.repeat.get(), 'Once'); self.assertIsNone(app.edit_id)
        self.assertEqual(self.store.tasks(), [])
        app.save(); self.assertEqual(len(self.store.tasks()), 1)
        target = self.root / 'export.ics'
        with patch('calendar_ui.filedialog.asksaveasfilename', return_value=str(target)):
            app.export_calendar_month()
        self.assertIn(b'Team meeting', target.read_bytes())
        app.calendar_provider.set('Notion Calendar'); app.calendar_url.set('webcal://example.com/feed.ics')
        with patch.object(app, 'refresh_calendars'): app.add_cloud_calendar()
        self.assertEqual(len(self.calendars.sources()), 2)
        self.assertEqual(app.calendar_url.get(), '')


if __name__ == '__main__': unittest.main()
