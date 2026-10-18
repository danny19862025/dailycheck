from datetime import datetime, timedelta
from pathlib import Path
import os
import tempfile
import unittest
from store import Store


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'tasks.db')
        self.addCleanup(self.store.db.close)

    def test_due_repeat_completion_and_next_day(self):
        now = datetime(2026, 9, 28, 9, 0)
        task = self.store.save('Walk', 'Walk outside', '2026-09-28', '09:00', True)
        self.assertEqual(self.store.pending(now - timedelta(seconds=1)), [])
        self.assertEqual(len(self.store.pending(now)), 1)
        self.store.notified(task, '2026-09-28', now)
        self.assertEqual(self.store.pending(now + timedelta(minutes=14)), [])
        self.assertEqual(len(self.store.pending(now + timedelta(minutes=15))), 1)
        self.store.complete(task, '2026-09-28', now)
        self.assertEqual(self.store.pending(now), [])
        self.assertEqual(len(self.store.pending(now + timedelta(days=1))), 1)
        self.store.undo(task, '2026-09-28')
        self.assertEqual(len(self.store.pending(now)), 1)

    def test_missed_one_off_and_validation(self):
        task = self.store.save('Call', '', '2026-09-27', '12:00', False)
        self.assertEqual(len(self.store.pending(datetime(2026,9,28))), 1)
        self.store.complete(task, '2026-09-27')
        self.assertEqual(self.store.pending(datetime(2026,9,28)), [])
        with self.assertRaises(ValueError):
            self.store.save('Bad', '', '2026-09-28', '25:00', False)
        self.store.delete(task)
        self.assertEqual(self.store.rows('2026-09-27'), [])

    @unittest.skipUnless(os.environ.get('DISPLAY'), 'Requires display')
    def test_gui_save_check_in_and_reopen(self):
        from app import App
        app = App(self.store)
        self.addCleanup(app.destroy)
        app.withdraw()
        app.title_var.set('GUI test')
        app.description.insert('1.0', 'A temporary task')
        app.save()
        app.update()
        key = app.tree.get_children()[0]
        app.tree.selection_set(key)
        app.update()
        app.done()
        app.update()
        self.assertIn('done', app.tree.item(key, 'tags'))
        app.undo()
        app.update()
        self.assertNotIn('done', app.tree.item(key, 'tags'))
        app.title_var.set('Unsaved edit')
        app.tick()
        app.update()
        self.assertEqual(app.title_var.get(), 'Unsaved edit')
        self.assertEqual(self.store.rows(app.day.get())[0]['description'], 'A temporary task')


if __name__ == '__main__':
    unittest.main()
