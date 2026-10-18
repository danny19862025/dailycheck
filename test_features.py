"""Meaningful recurrence, migration, check-in, import, and GUI regressions."""
from contextlib import closing
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from store import Store, occurs


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'tasks.sqlite3')
        self.addCleanup(self.store.db.close)

    def save(self, **options):
        values = dict(title='Task', description='Details', day='2026-09-28', time='09:00', daily=True)
        values.update(options)
        return self.store.save(**values)

    def test_legacy_migration_keeps_history_and_pre_upgrade_backup(self):
        path = self.root / 'old/tasks.sqlite3'; path.parent.mkdir()
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""CREATE TABLE tasks(id INTEGER PRIMARY KEY,title TEXT,description TEXT,day TEXT,time TEXT,daily INTEGER);
                CREATE TABLE checks(task INTEGER,day TEXT,done TEXT,reminded TEXT,PRIMARY KEY(task,day));
                INSERT INTO tasks VALUES(7,'Legacy','Keep this','2026-09-28','09:00',1);
                INSERT INTO checks VALUES(7,'2026-09-28','2026-09-28T08:59:00','2026-09-28T08:30:00');""")
        old = Store(path); self.addCleanup(old.db.close)
        row = old.rows('2026-09-28')[0]
        self.assertEqual(row['id'], 7)
        self.assertEqual(row['slot'], '09:00')
        self.assertEqual(row['done'], '2026-09-28T08:59:00')
        self.assertEqual(old.task(7)['repeat_kind'], 'daily')
        backups = list((path.parent / 'backups').glob('before-upgrade-*.sqlite3'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute('SELECT done FROM checks').fetchone()[0], row['done'])
            self.assertNotIn('slot', {r[1] for r in db.execute('PRAGMA table_info(checks)')})

    def test_weekday_weekend_custom_interval_end_and_monthly_clamping(self):
        task = self.store.task(self.save(repeat_kind='weekly', weekdays=[0, 2, 4], end_day='2026-10-02'))
        self.assertEqual([occurs(task, '2026-09-' + d) for d in ('28', '29', '30')], [True, False, True])
        self.assertTrue(occurs(task, '2026-10-02')); self.assertFalse(occurs(task, '2026-10-05'))
        weekend = self.store.task(self.save(repeat_kind='weekly', weekdays=[5, 6]))
        self.assertFalse(occurs(weekend, '2026-10-02')); self.assertTrue(occurs(weekend, '2026-10-03'))
        every_three = self.store.task(self.save(interval=3))
        self.assertFalse(occurs(every_three, '2026-09-29')); self.assertTrue(occurs(every_three, '2026-10-01'))
        biweekly = self.store.task(self.save(repeat_kind='weekly', interval=2, weekdays=[0]))
        self.assertFalse(occurs(biweekly, '2026-10-05')); self.assertTrue(occurs(biweekly, '2026-10-12'))
        monthly = self.store.task(self.save(day='2028-01-31', repeat_kind='monthly'))
        self.assertTrue(occurs(monthly, '2028-02-29')); self.assertTrue(occurs(monthly, '2028-03-31'))
        self.assertFalse(occurs(monthly, '2028-03-29'))
        with self.assertRaises(ValueError): self.save(end_day='2026-09-27')
        with self.assertRaises(ValueError): self.save(repeat_kind='weekly', weekdays=[])

    def test_multiple_times_have_separate_completion_and_notifications(self):
        task = self.save(time='09:00, 18:00, 09:00')
        self.assertEqual(len(self.store.rows('2026-09-28')), 2)
        now = datetime(2026, 9, 28, 10)
        self.store.complete(task, '2026-09-28', now, slot='09:00')
        self.assertEqual(self.store.pending(now), [])
        self.assertEqual([r['slot'] for r in self.store.pending(now.replace(hour=19))], ['18:00'])
        self.store.notified(task, '2026-09-28', now.replace(hour=19), slot='18:00')
        self.assertEqual(self.store.pending(now.replace(hour=19, minute=14)), [])
        self.assertEqual(len(self.store.pending(now.replace(hour=19, minute=15))), 1)

    def test_snooze_survives_reopen_and_midnight_without_changing_other_slot(self):
        task = self.save(time='09:00, 18:00')
        now = datetime(2026, 9, 28, 10)
        self.assertEqual(self.store.snooze(task, '2026-09-28', '1h', now), now + timedelta(hours=1))
        self.assertEqual(self.store.pending(now + timedelta(minutes=59)), [])
        self.assertEqual(len(self.store.pending(now + timedelta(hours=1))), 1)
        self.store.snooze(task, '2026-09-28', 'tomorrow', now)
        tomorrow = now + timedelta(days=1)
        rows = self.store.pending(tomorrow)
        self.assertEqual({(r['occurrence'], r['slot']) for r in rows}, {('2026-09-28', '09:00'), ('2026-09-29', '09:00')})
        reopened = Store(self.store.path); self.addCleanup(reopened.db.close)
        self.assertEqual(len(reopened.pending(tomorrow)), 2)
        self.store.complete(task, '2026-09-28', tomorrow, slot='09:00')
        self.assertEqual(len(self.store.pending(tomorrow)), 1)

    def test_focus_window_snooze_and_lock_enforced_for_every_check_in(self):
        task = self.save(focus=True, focus_before=5, focus_after=30)
        due = datetime(2026, 9, 28, 9)
        with self.assertRaises(ValueError): self.store.complete(task, '2026-09-28', due - timedelta(minutes=6))
        with self.assertRaises(ValueError): self.store.complete(task, '2026-09-28', due + timedelta(minutes=31))
        with self.assertRaises(ValueError): self.store.complete(task, '2026-09-28', due, locked=True)
        self.store.snooze(task, '2026-09-28', '1h', due)
        with self.assertRaises(ValueError): self.store.complete(task, '2026-09-28', due)
        self.store.complete(task, '2026-09-28', due + timedelta(hours=1))
        self.assertIsNotNone(self.store.rows('2026-09-28')[0]['done'])

    def test_priorities_categories_search_and_combined_state_filters(self):
        a = self.save(title='Proposal', description='Quarterly figures', category='Work', priority='High', tags='report, urgent')
        self.save(title='Walk', category='Health', priority='Low')
        now = datetime(2026, 9, 28, 10)
        self.assertEqual([r['id'] for r in self.store.rows('2026-09-28', search='figures', category='Work', tag='urgent', now=now)], [a])
        self.assertEqual(len(self.store.rows('2026-09-28', state='Late', now=now)), 2)
        self.store.complete(a, '2026-09-28', now)
        self.assertEqual(len(self.store.rows('2026-09-28', state='Pending', now=now)), 1)
        self.assertEqual([r['id'] for r in self.store.rows('2026-09-28', state='Done', now=now)], [a])

    def test_checklists_reset_per_occurrence_and_survive_text_edits(self):
        task = self.save(description='Prepare\n- [ ] First\n- [ ] Second', time='09:00,18:00')
        definitions = self.store.task(task)['checklist']
        self.store.toggle_subtask(task, '2026-09-28', definitions[0]['id'], True, '09:00')
        rows = self.store.rows('2026-09-28')
        self.assertEqual(rows[0]['checklist_done'], [definitions[0]['id']]); self.assertEqual(rows[1]['checklist_done'], [])
        self.store.complete(task, '2026-09-28', datetime(2026, 9, 28, 10), slot='09:00')
        self.assertEqual(len(self.store.rows('2026-09-28')[0]['checklist_done']), 2)
        self.store.save('Task', '- [ ] First\n- [ ] Third', '2026-09-28', '09:00,18:00', True, task)
        self.assertEqual(self.store.task(task)['checklist'][0]['id'], definitions[0]['id'])
        self.assertEqual(self.store.rows('2026-09-29')[0]['checklist_done'], [])

    def test_timer_pause_restart_completion_and_one_active_timer(self):
        first, second = self.save(title='First'), self.save(title='Second')
        now = datetime(2026, 9, 28, 9)
        self.store.track(first, '2026-09-28', now=now)
        self.store.track(second, '2026-09-28', now=now + timedelta(seconds=50))
        first_row = self.store.occurrence(first, '2026-09-28')
        self.assertEqual(first_row['elapsed'], 50); self.assertIsNone(first_row['started'])
        self.store.track(second, '2026-09-28', running=False, now=now + timedelta(seconds=80))
        self.store.track(second, '2026-09-28', now=now + timedelta(seconds=100))
        self.store.complete(second, '2026-09-28', now + timedelta(seconds=120))
        row = self.store.occurrence(second, '2026-09-28')
        self.assertEqual(row['elapsed'], 50); self.assertIsNone(row['started'])

    def test_streaks_and_on_time_denominator_and_task_scope(self):
        task = self.save(day='2026-09-28', repeat_kind='weekly', weekdays=[0, 2, 4])
        self.store.complete(task, '2026-09-28', datetime(2026, 9, 28, 8, 59))
        self.store.complete(task, '2026-09-30', datetime(2026, 9, 30, 8, 59))
        stats = self.store.statistics(datetime(2026, 10, 2, 12), task_id=task)
        self.assertEqual(stats['current'], 2); self.assertEqual(stats['best'], 2)
        self.assertEqual(stats['week'], [2, 3]); self.assertEqual(stats['month'], [0, 1])
        self.assertEqual(self.store.statistics(datetime(2026, 10, 3, 12))['current'], 0)

    def test_export_import_json_csv_backups_and_atomic_rejection(self):
        task = self.save(title='Café', priority='High', category='Health', tags='meds, daily', description='- [ ] Water', time='09:00,18:00')
        self.store.complete(task, '2026-09-28', datetime(2026, 9, 28, 9), slot='09:00')
        path = self.root / 'export.json'; self.store.export_file(path)
        target = Store(self.root / 'imported/tasks.sqlite3'); self.addCleanup(target.db.close)
        self.assertEqual(target.import_file(path), 1)
        self.assertEqual(target.tasks()[0]['tags'], ['meds', 'daily'])
        self.assertIsNotNone(target.rows('2026-09-28')[0]['done'])
        self.assertEqual(len(list((target.path.parent / 'backups').glob('before-import-*.sqlite3'))), 1)
        csv_path = self.root / 'tasks.csv'; self.store.export_file(csv_path)
        self.assertEqual(target.import_file(csv_path), 1)
        self.assertEqual(target.tasks()[1]['priority'], 'High')
        invalid = json.loads(path.read_text()); invalid['tasks'][0]['priority'] = 'Invalid'
        path.write_text(json.dumps(invalid))
        before = len(target.tasks())
        with self.assertRaises(ValueError): target.import_file(path)
        self.assertEqual(len(target.tasks()), before)
        backup = self.store.auto_backup('2026-10-18')
        with closing(sqlite3.connect(backup)) as db: self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError): self.store.backup(self.store.path)
        with self.assertRaises(ValueError): self.store.export_file(self.store.path)
        path.write_text('[]')
        with self.assertRaises(ValueError): target.import_file(path)

    def test_retired_reminder_never_notifies_again(self):
        task = self.save()
        self.store.notified(task, '2026-09-28', datetime(2026, 9, 28, 9))
        self.store.save('Task', '', '2026-09-28', '18:00', True, task)
        self.assertEqual(self.store.pending(datetime(2026, 9, 28, 10)), [])
        with self.assertRaises(ValueError): self.store.occurrence(task, '2026-09-28', '09:00', allow_history=False)

    @unittest.skipUnless(os.environ.get('DISPLAY'), 'Requires display')
    def test_editor_filters_checklist_dark_calendar_and_stats(self):
        from app import App
        app = App(self.store); self.addCleanup(app.destroy)
        app.withdraw(); app.title_var.set('Morning meds'); app.category.set('Health'); app.priority.set('High')
        monday = date.today() + timedelta(days=(-date.today().weekday()) % 7)
        app.date_var.set(monday.isoformat())
        app.tags.set('medicine, routine'); app.time_var.set('08:00, 20:00'); app.repeat.set('Weekdays')
        app.description.insert('1.0', '- [ ] Drink water\n- [ ] Take tablet'); app.save(); app.update()
        task = self.store.tasks()[0]
        self.assertEqual(task['times'], ['08:00', '20:00']); self.assertEqual(task['weekdays'], [0, 1, 2, 3, 4])
        app.day.set(monday.isoformat()); app.refresh(); app.update()
        self.assertEqual(len(app.tree.get_children()), 2)
        app.deiconify(); app.update()
        for button in [*app.action_buttons, app.timer_button, app.save_button]:
            self.assertTrue(button.winfo_viewable())
            self.assertLess(button.winfo_rooty() + button.winfo_height(), app.winfo_rooty() + app.winfo_height())
        app.category_filter.set('Work'); app.refresh(); self.assertEqual(len(app.rows), 0)
        app.category_filter.set('Health'); app.search.set('tablet'); app.refresh(); self.assertEqual(len(app.rows), 2)
        key = app.tree.get_children()[0]; app.tree.selection_set(key); app.select(); app.update()
        boxes = [w for w in app.checklist_frame.winfo_children() if w.winfo_class() == 'TCheckbutton']
        self.assertEqual(len(boxes), 2); boxes[0].invoke(); app.update()
        row = self.store.occurrence(task['id'], monday.isoformat(), '08:00')
        self.assertEqual(len(row['checklist_done']), 1)
        app.title_var.set('Unsaved editor'); app.tick(); self.assertEqual(app.title_var.get(), 'Unsaved editor')
        app.theme_var.set('Dark'); app.save_settings(); self.assertEqual(app.colors['bg'], '#171923')
        app.notebook.select(app.calendar_tab); app.update(); self.assertGreater(len(app.month_grid.winfo_children()), 28)
        app.notebook.select(app.stats_tab); app.update(); self.assertIn('Current streak', app.stats_text.get())
        app.show_task(task['id'], monday.isoformat(), '20:00'); app.update(); self.assertEqual(app.edit_id, task['id'])
        self.assertGreaterEqual(app.time_entry.winfo_rooty(), app.editor_canvas.winfo_rooty())
        self.assertLess(app.time_entry.winfo_rooty() + app.time_entry.winfo_height(), app.editor_canvas.winfo_rooty() + app.editor_canvas.winfo_height())
