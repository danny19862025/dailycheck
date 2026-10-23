"""Exercise the real GLib scheduler without requiring a desktop session."""
import time
import unittest
from unittest.mock import Mock, patch

import desktop


@unittest.skipUnless(desktop.GLib, 'Requires GObject bindings')
class BackgroundTests(unittest.TestCase):
    def test_startup_runs_once_and_periodic_checks_continue(self):
        class CountingService(desktop.Service):
            def tick(self):
                self.calls.append(time.monotonic())
                return desktop.GLib.SOURCE_CONTINUE

        service = CountingService.__new__(CountingService)
        service.calls = []
        initial_sources = []
        idle_add = desktop.GLib.idle_add

        def record_idle(callback):
            source_id = idle_add(callback)
            initial_sources.append(source_id)
            return source_id

        with patch.object(desktop.Gio, 'bus_own_name_on_connection'), \
             patch.object(desktop.Gio, 'bus_watch_name_on_connection'), \
             patch.object(desktop.GLib, 'idle_add', side_effect=record_idle):
            service.__init__(Mock(), connection=Mock())
        stop_source = desktop.GLib.timeout_add(6500, lambda: (service.loop.quit(), False)[1])
        started = time.process_time()
        try:
            service.loop.run()
            cpu = time.process_time() - started
            self.assertEqual(len(service.calls), 2, 'Expected one startup check and one periodic check')
            self.assertGreater(service.calls[1] - service.calls[0], 3.5)
            self.assertLess(cpu, 0.5, 'Background scheduler consumed CPU while idle')
            print(f'Background scheduler: {len(service.calls)} checks, {cpu:.4f}s CPU in 6.5s')
        finally:
            context = desktop.GLib.MainContext.default()
            for source_id in (*initial_sources, service.ticker, stop_source):
                source = context.find_source_by_id(source_id)
                if source is not None:
                    source.destroy()


if __name__ == '__main__':
    unittest.main()
