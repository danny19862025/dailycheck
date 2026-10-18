#!/usr/bin/python3
"""Capture the actual Tk window with synthetic tasks using its X11 drawable only."""
import argparse
import ctypes as C
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App
from store import Store
import cairo


def capture(app, path):
    class XImage(C.Structure):
        _fields_ = [('width', C.c_int), ('height', C.c_int), ('xoffset', C.c_int), ('format', C.c_int), ('data', C.c_void_p), ('byte_order', C.c_int), ('bitmap_unit', C.c_int), ('bitmap_bit_order', C.c_int), ('bitmap_pad', C.c_int), ('depth', C.c_int), ('bytes_per_line', C.c_int), ('bits_per_pixel', C.c_int), ('red_mask', C.c_ulong), ('green_mask', C.c_ulong), ('blue_mask', C.c_ulong)]
    x = C.CDLL('libX11.so.6')
    x.XOpenDisplay.restype = C.c_void_p; x.XOpenDisplay.argtypes = [C.c_char_p]
    x.XGetImage.restype = C.POINTER(XImage); x.XGetImage.argtypes = [C.c_void_p, C.c_ulong, C.c_int, C.c_int, C.c_uint, C.c_uint, C.c_ulong, C.c_int]
    x.XDestroyImage.argtypes = [C.POINTER(XImage)]; x.XCloseDisplay.argtypes = [C.c_void_p]
    display = x.XOpenDisplay(None)
    assert display, 'An X11 / XWayland desktop is required'
    image = x.XGetImage(display, app.winfo_id(), 0, 0, app.winfo_width(), app.winfo_height(), C.c_ulong(-1).value, 2)
    assert image and image.contents.bits_per_pixel == 32 and image.contents.red_mask == 0xff0000
    info = image.contents
    pixels = bytearray(C.string_at(info.data, info.bytes_per_line * info.height))
    surface = cairo.ImageSurface.create_for_data(pixels, cairo.FORMAT_RGB24, info.width, info.height, info.bytes_per_line)
    surface.write_to_png(str(path)); surface.finish()
    x.XDestroyImage(image); x.XCloseDisplay(display)


parser = argparse.ArgumentParser(); parser.add_argument('--output', default='/tmp/dailycheck-preview'); args = parser.parse_args()
output = Path(args.output).resolve(); output.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='dailycheck-preview-') as directory:
    store = Store(Path(directory) / 'tasks.sqlite3')
    now = datetime.now(); today = now.date(); start = today - timedelta(days=4)
    health = store.save('Daily vitamins', 'Morning and evening routine\n- [ ] Drink a glass of water\n- [ ] Take vitamins', start.isoformat(), '08:00, 20:00', True, priority='High', category='Health', tags='routine, wellbeing')
    work = store.save('Finish the project proposal', 'Outline milestones and send a draft to the team.', today.isoformat(), '16:30', False, priority='High', category='Work', tags='project')
    home = store.save('Walk in the park', 'Fresh air and a little movement.', start.isoformat(), '07:30', True, priority='Low', category='Health')
    store.save('Plan meals for the week', 'Choose recipes and make a shopping list.', today.isoformat(), '18:00', False, priority='Med', category='Home', tags='food')
    for offset in range(4):
        day = start + timedelta(days=offset)
        for slot in ('08:00', '20:00'): store.complete(health, day.isoformat(), datetime.fromisoformat(day.isoformat() + 'T' + slot), slot=slot)
        store.complete(home, day.isoformat(), datetime.fromisoformat(day.isoformat() + 'T07:25'))
    store.complete(home, today.isoformat(), datetime.fromisoformat(today.isoformat() + 'T07:25'))
    store.set_settings(theme='Dark')
    app = App(store)
    key = f'{health}|{today}|08:00'
    app.tree.selection_set(key); app.select()
    screens = [('dailycheck.png', app.task_tab), ('calendar.png', app.calendar_tab), ('stats.png', app.stats_tab), ('settings.png', app.settings_tab)]
    index = [0]
    def step():
        if index[0] >= len(screens): app.destroy(); return
        name, page = screens[index[0]]; app.notebook.select(page); app.update_idletasks()
        app.after(300, lambda: save(name))
    def save(name):
        app.update_idletasks(); capture(app, output / name); index[0] += 1; app.after(100, step)
    app.after(400, step); app.mainloop(); store.db.close()
print('Captured real Daily Check task, calendar, stats, and settings windows with demo data:', output)
