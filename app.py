"""Daily Check — local plans, flexible reminders, and completion check-ins."""
import argparse
import calendar
import configparser
from datetime import date, datetime, timedelta
import os
from pathlib import Path
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from store import Store, PRIORITIES
import desktop
from calendar_ui import CalendarConnectionsMixin
from calendar_sync import on_day

VERSION = '0.2.0'
DATA = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'daily-check'
REPEAT_LABELS = ('Once', 'Every day', 'Every N days', 'Weekly / selected days', 'Weekdays', 'Weekends', 'Monthly')


def duration(seconds):
    hours, minutes = divmod(int(seconds) // 60, 60)
    return f'{hours}h {minutes:02}m' if hours else f'{minutes}m {int(seconds) % 60:02}s'


def system_dark():
    config = configparser.ConfigParser(interpolation=None)
    try:
        config.read(Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'kdeglobals')
        color = config.get('Colors:Window', 'BackgroundNormal', fallback='')
        if color:
            rgb = [int(v) for v in color.split(',')[:3]]
            return sum(rgb) / 3 < 128
        scheme = config.get('General', 'ColorScheme', fallback='')
        if scheme:
            return 'dark' in scheme.lower()
        if desktop.Gio:
            source = desktop.Gio.SettingsSchemaSource.get_default()
            if source and source.lookup('org.gnome.desktop.interface', True):
                return desktop.Gio.Settings.new('org.gnome.desktop.interface').get_string('color-scheme') == 'prefer-dark'
    except (OSError, ValueError, configparser.Error):
        pass
    return False


class App(CalendarConnectionsMixin, tk.Tk):
    def __init__(self, store=None, integrate=False):
        super().__init__()
        self.store = store or Store(DATA / 'tasks.sqlite3')
        self.own_store = store is None
        self.integrate = integrate
        self.bridge = None
        self.edit_id, self.rows, self.role_widgets = None, {}, []
        self.ready, self.refresh_job = False, None
        self.refresh_revision, self.calendar_signature = None, None
        self.calendar_month = date.today().replace(day=1)
        self.init_calendar_connections()
        self.title(f'Daily Check {VERSION} • Plans & Reminders')
        self.geometry('1320x900')
        self.minsize(1080, 780)
        self.settings_values = self.store.settings()
        self.style = ttk.Style(self)
        self.style.theme_use('clam')
        self.apply_theme()
        shell = ttk.Frame(self, padding=18)
        shell.pack(fill='both', expand=True)
        hero = tk.Frame(shell, bg='#29265b', padx=20, pady=14)
        hero.pack(fill='x')
        tk.Label(hero, text='✓  DAILY CHECK', bg='#29265b', fg='#c5bfff', font=('DejaVu Sans', 10, 'bold')).pack(anchor='w')
        tk.Label(hero, text='A little progress, every day.', bg='#29265b', fg='white', font=('DejaVu Sans', 23, 'bold')).pack(anchor='w')
        tk.Label(hero, text='Plan your day. Keep your rhythm. Celebrate getting it done.', bg='#29265b', fg='#d1d4ef', font=('DejaVu Sans', 10)).pack(anchor='w')
        self.notebook = ttk.Notebook(shell)
        self.notebook.pack(fill='both', expand=True, pady=(12, 8))
        self.task_tab, self.calendar_tab, self.stats_tab, self.settings_tab = [ttk.Frame(self.notebook, padding=12) for _ in range(4)]
        for frame, title in zip((self.task_tab, self.calendar_tab, self.stats_tab, self.settings_tab), ('Tasks', 'Calendar', 'Streaks & stats', 'Settings & backup')):
            self.notebook.add(frame, text=title)
        self.make_tasks()
        self.make_calendar()
        self.make_stats()
        self.make_settings()
        self.feedback = tk.StringVar(value='Ready when you are. Add a task to plan your day.')
        self.notebook.pack_forget()
        footer = ttk.Frame(shell); footer.pack(side='bottom', fill='x')
        ttk.Label(footer, textvariable=self.feedback, wraplength=1100, style='Accent.TLabel').pack(anchor='w')
        self.service_status = tk.StringVar(value='Reminders run while the computer is awake. Closing the window keeps the tray service running.')
        ttk.Label(footer, textvariable=self.service_status, style='Muted.TLabel', wraplength=1100).pack(anchor='w', pady=(4, 0))
        self.notebook.pack(fill='both', expand=True, pady=(12, 8))
        self.bind('<Control-n>', lambda _: self.new())
        self.bind('<Control-s>', lambda _: self.save())
        self.bind('<Control-f>', lambda _: self.search_entry.focus_set())
        self.notebook.bind('<<NotebookTabChanged>>', self.tab_changed)
        self.protocol('WM_DELETE_WINDOW', self.close_window)
        self.ready = True
        self.refresh()
        self.after(5000, self.tick)
        self.after(1000, self.update_timers)
        self.after(300, self.refresh_calendars)
        if integrate:
            self.bridge = desktop.UiBridge(self)
            self.after(1200, self.refresh_service_status)

    def color_widget(self, widget, role='card'):
        self.role_widgets.append((widget, role))
        self.color_one(widget, role)
        return widget

    def color_one(self, widget, role):
        values = {'bg': self.colors['card' if role != 'canvas' else 'bg']}
        if isinstance(widget, (tk.Label, tk.Text, tk.Checkbutton, tk.Button)):
            values['fg'] = self.colors['text']
        if isinstance(widget, tk.Text):
            values.update(insertbackground=self.colors['text'], highlightbackground=self.colors['border'], highlightcolor=self.colors['accent'])
        if isinstance(widget, tk.Checkbutton):
            values.update(selectcolor=self.colors['card'], activebackground=self.colors['card'], activeforeground=self.colors['text'])
        widget.configure(**values)

    def apply_theme(self):
        mode = self.settings_values.get('theme', 'System')
        dark = mode == 'Dark' or (mode == 'System' and system_dark())
        self.colors = dict(bg='#171923', card='#222633', text='#edf0f7', muted='#aab2c8', border='#3b4257', accent='#b4a6ff', select='#423760') if dark else dict(bg='#f2f5fc', card='#ffffff', text='#24324b', muted='#62718a', border='#dce3ef', accent='#5746d9', select='#e8e3ff')
        c, style = self.colors, self.style
        self.configure(bg=c['bg'])
        style.configure('.', font=('DejaVu Sans', 10), background=c['bg'], foreground=c['text'])
        style.configure('TFrame', background=c['bg'])
        style.configure('Card.TFrame', background=c['card'])
        style.configure('TLabel', background=c['bg'], foreground=c['text'])
        style.configure('Card.TLabel', background=c['card'], foreground=c['text'])
        style.configure('Muted.TLabel', foreground=c['muted'])
        style.configure('Accent.TLabel', foreground=c['accent'])
        style.configure('Section.TLabel', background=c['card'], font=('DejaVu Sans', 15, 'bold'))
        style.configure('Big.TLabel', font=('DejaVu Sans', 26, 'bold'), foreground=c['accent'])
        style.configure('TButton', padding=(10, 7), background=c['card'], borderwidth=0)
        style.map('TButton', background=[('active', c['select'])], foreground=[('disabled', c['muted'])])
        for name, color, active in [('Primary', '#6750c8', '#5140a9'), ('Success', '#177454', '#115d43')]:
            style.configure(name + '.TButton', background=color, foreground='white', font=('DejaVu Sans', 10, 'bold'))
            style.map(name + '.TButton', background=[('disabled', c['border']), ('active', active)], foreground=[('!disabled', 'white')])
        style.configure('Danger.TButton', foreground='#f08090' if dark else '#b8394d')
        style.configure('TEntry', fieldbackground=c['card'], foreground=c['text'], insertcolor=c['text'], padding=5)
        style.configure('TCombobox', fieldbackground=c['card'], background=c['card'], foreground=c['text'], arrowcolor=c['text'], padding=4)
        style.map('TCombobox', fieldbackground=[('readonly', c['card'])], foreground=[('readonly', c['text'])], selectbackground=[('readonly', c['select'])])
        self.option_add('*TCombobox*Listbox.background', c['card'])
        self.option_add('*TCombobox*Listbox.foreground', c['text'])
        style.configure('TSpinbox', fieldbackground=c['card'], foreground=c['text'], arrowcolor=c['text'])
        style.configure('TCheckbutton', background=c['card'])
        style.map('TCheckbutton', background=[('active', c['card'])])
        for name in ('TNotebook', 'TNotebook.Tab', 'TEntry', 'TCombobox', 'TSpinbox', 'Treeview', 'Treeview.Heading'):
            style.configure(name, bordercolor=c['border'], lightcolor=c['border'], darkcolor=c['border'])
        style.configure('TNotebook', background=c['bg'], borderwidth=0)
        style.configure('TNotebook.Tab', padding=(15, 8), background=c['card'])
        style.map('TNotebook.Tab', background=[('selected', c['select'])])
        style.configure('Treeview', rowheight=47, background=c['card'], fieldbackground=c['card'], foreground=c['text'], borderwidth=0)
        style.configure('Treeview.Heading', padding=7, relief='flat', borderwidth=0, background=c['bg'], foreground=c['muted'], font=('DejaVu Sans', 9, 'bold'))
        style.map('Treeview', background=[('selected', c['select'])], foreground=[('selected', c['text'])])
        style.configure('TProgressbar', background='#8c76ed', troughcolor=c['border'], borderwidth=0)
        for widget, role in getattr(self, 'role_widgets', []):
            if widget.winfo_exists(): self.color_one(widget, role)
        if hasattr(self, 'snooze_menu'):
            self.snooze_menu.configure(bg=c['card'], fg=c['text'], activebackground=c['select'], activeforeground=c['text'])
        if hasattr(self, 'tree'):
            for priority, color in [('Low', '#68d5ad' if dark else '#177454'), ('Med', '#f0cb72' if dark else '#936115'), ('High', '#ff96a7' if dark else '#b8394d')]:
                self.tree.tag_configure(priority, foreground=color)
            for status, color in [('done', '#203a33' if dark else '#effaf4'), ('late', '#3d3027' if dark else '#fff4e3'), ('pending', c['card'])]:
                self.tree.tag_configure(status, background=color)

    def make_tasks(self):
        stats = ttk.Frame(self.task_tab)
        stats.pack(fill='x', pady=(0, 10))
        self.stat_vars = []
        for label in ('PLANNED', 'COMPLETED', 'NEEDS ATTENTION'):
            card = ttk.Frame(stats, style='Card.TFrame', padding=(14, 9))
            card.pack(side='left', fill='x', expand=True, padx=(0, 10))
            value = tk.StringVar(value='0'); self.stat_vars.append(value)
            ttk.Label(card, textvariable=value, style='Section.TLabel').pack(side='left')
            ttk.Label(card, text=label, style='Card.TLabel').pack(side='left', padx=10)
        nav = ttk.Frame(self.task_tab); nav.pack(fill='x')
        self.day = tk.StringVar(value=date.today().isoformat())
        ttk.Button(nav, text='‹', width=3, command=lambda: self.move(-1)).pack(side='left')
        entry = ttk.Entry(nav, textvariable=self.day, width=12); entry.pack(side='left', padx=5)
        entry.bind('<Return>', lambda _: self.refresh())
        ttk.Button(nav, text='Go', command=self.refresh).pack(side='left')
        ttk.Button(nav, text='›', width=3, command=lambda: self.move(1)).pack(side='left', padx=5)
        ttk.Button(nav, text='Today', command=self.today).pack(side='left')
        self.summary = tk.StringVar(); ttk.Label(nav, textvariable=self.summary, style='Muted.TLabel').pack(side='right')
        body = ttk.Panedwindow(self.task_tab, orient='horizontal'); body.pack(fill='both', expand=True, pady=(10, 0))
        left = ttk.Frame(body, style='Card.TFrame', padding=12)
        right = ttk.Frame(body, style='Card.TFrame')
        body.add(left, weight=3); body.add(right, weight=2)
        filters = ttk.Frame(left, style='Card.TFrame'); filters.pack(fill='x')
        ttk.Label(filters, text='Search', style='Card.TLabel').pack(side='left', padx=(0, 6))
        self.search = tk.StringVar(); self.state_filter = tk.StringVar(value='All'); self.category_filter = tk.StringVar(value='All'); self.tag_filter = tk.StringVar(value='All')
        self.search_entry = ttk.Entry(filters, textvariable=self.search)
        self.search_entry.pack(side='left', fill='x', expand=True, padx=(0, 8))
        self.state_box = ttk.Combobox(filters, textvariable=self.state_filter, values=('All', 'Done', 'Pending', 'Late'), width=9, state='readonly'); self.state_box.pack(side='right')
        ttk.Label(filters, text='Status', style='Card.TLabel').pack(side='right', padx=6)
        categories = ttk.Frame(left, style='Card.TFrame'); categories.pack(fill='x', pady=8)
        self.category_buttons = {}
        for name in ('All', 'Work', 'Health', 'Home'):
            button = ttk.Button(categories, text=name, command=lambda n=name: self.set_category(n)); button.pack(side='left', padx=(0, 4)); self.category_buttons[name] = button
        self.tag_box = ttk.Combobox(categories, textvariable=self.tag_filter, width=12, state='readonly'); self.tag_box.pack(side='right')
        ttk.Label(categories, text='Tag', style='Card.TLabel').pack(side='right', padx=5)
        other_category = ttk.Frame(left, style='Card.TFrame'); other_category.pack(fill='x', pady=(0, 6))
        ttk.Label(other_category, text='Category', style='Card.TLabel').pack(side='left', padx=(0, 6))
        self.category_box = ttk.Combobox(other_category, textvariable=self.category_filter, width=14, state='readonly'); self.category_box.pack(side='left')
        for variable in (self.search, self.state_filter, self.category_filter, self.tag_filter): variable.trace_add('write', lambda *_: self.queue_refresh())
        self.progress = ttk.Progressbar(left, maximum=100); self.progress.pack(fill='x', pady=(0, 8))
        table = ttk.Frame(left, style='Card.TFrame'); table.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(table, columns=('time', 'priority', 'task', 'category', 'status', 'tracked'), show='headings', selectmode='browse', height=5)
        for name, label, width in [('time', 'TIME', 62), ('priority', 'PRIORITY', 85), ('task', 'TASK', 180), ('category', 'CATEGORY', 92), ('status', 'STATUS', 120), ('tracked', 'TRACKED', 85)]:
            self.tree.heading(name, text=label); self.tree.column(name, width=width, minwidth=45, stretch=name in ('task', 'status'))
        scrollbar = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview); scrollbar.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=scrollbar.set); self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.select)
        self.empty = self.color_widget(tk.Label(self.tree, text='No matching tasks.\nAdd a task or change your filters.', font=('DejaVu Sans', 11), justify='center'))
        self.checklist_frame = ttk.Frame(left, style='Card.TFrame'); self.checklist_frame.pack(fill='x', pady=(8, 0))
        actions = ttk.Frame(left, style='Card.TFrame'); actions.pack(fill='x', pady=(8, 0))
        self.action_buttons = []
        for label, action, style in [('✓ Mark done', self.done, 'Success.TButton'), ('Undo', self.undo, 'TButton'), ('Delete', self.delete, 'Danger.TButton')]:
            button = ttk.Button(actions, text=label, command=action, style=style, state='disabled'); button.pack(side='left', padx=(0, 5)); self.action_buttons.append(button)
        ttk.Button(actions, text='+ New', command=self.new).pack(side='right')
        extras = ttk.Frame(left, style='Card.TFrame'); extras.pack(fill='x', pady=(6, 0))
        self.timer_button = ttk.Button(extras, text='▶ Start timer', command=self.toggle_timer); self.timer_button.pack(side='left', padx=(0, 6))
        snooze = ttk.Menubutton(extras, text='Snooze ▾'); snooze.pack(side='left')
        menu = tk.Menu(snooze, tearoff=False)
        for label, value in [('10 minutes', '10m'), ('1 hour', '1h'), ('Tomorrow at reminder time', 'tomorrow')]: menu.add_command(label=label, command=lambda v=value: self.snooze(v))
        snooze['menu'] = menu
        self.snooze_menu = menu
        self.focus_hint = tk.StringVar(); self.focus_label = ttk.Label(left, textvariable=self.focus_hint, wraplength=650, style='Card.TLabel')
        # Reserve action/checklist space before the expanding table.
        table.pack_forget(); self.checklist_frame.pack_forget(); actions.pack_forget(); extras.pack_forget()
        self.focus_label.pack(side='bottom', fill='x', pady=(5, 0))
        extras.pack(side='bottom', fill='x', pady=(6, 0))
        actions.pack(side='bottom', fill='x', pady=(8, 0))
        self.checklist_frame.pack(side='bottom', fill='x', pady=(8, 0))
        table.pack(fill='both', expand=True)
        self.make_editor(right)
        self.apply_theme()

    def make_editor(self, parent):
        buttons = ttk.Frame(parent, style='Card.TFrame', padding=(12, 8)); buttons.pack(side='bottom', fill='x')
        canvas = self.color_widget(tk.Canvas(parent, highlightthickness=0), 'canvas')
        self.editor_canvas = canvas
        scrollbar = ttk.Scrollbar(parent, orient='vertical', command=canvas.yview); scrollbar.pack(side='right', fill='y')
        canvas.pack(fill='both', expand=True); canvas.configure(yscrollcommand=scrollbar.set)
        editor = ttk.Frame(canvas, style='Card.TFrame', padding=14)
        self.editor_frame = editor
        window = canvas.create_window((0, 0), window=editor, anchor='nw')
        editor.bind('<Configure>', lambda _: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(window, width=e.width))
        def wheel(event):
            direction = -1 if event.num == 4 or getattr(event, 'delta', 0) > 0 else 1
            if isinstance(event.widget, tk.Text):
                top, bottom = event.widget.yview()
                if (direction < 0 and top > 0) or (direction > 0 and bottom < 1):
                    event.widget.yview_scroll(direction * 3, 'units')
                    return 'break'
            canvas.yview_scroll(direction, 'units')
            return 'break'
        def bind_wheel(widget):
            for event in ('<MouseWheel>', '<Button-4>', '<Button-5>'): widget.bind(event, wheel, add='+')
            for child in widget.winfo_children(): bind_wheel(child)
        self.editor_heading = tk.StringVar(value='Add a task')
        ttk.Label(editor, textvariable=self.editor_heading, style='Section.TLabel').pack(anchor='w', pady=(0, 10))
        self.title_var = tk.StringVar(); self.date_var = tk.StringVar(value=self.day.get()); self.time_var = tk.StringVar(value='09:00')
        self.daily = tk.BooleanVar(value=True)  # Legacy embedding compatibility; the editor uses repeat_kind.
        self.priority = tk.StringVar(value='Med'); self.category = tk.StringVar(value='Home'); self.tags = tk.StringVar()
        self.repeat = tk.StringVar(value='Every day'); self.interval = tk.IntVar(value=1); self.end_day = tk.StringVar()
        self.focus_mode = tk.BooleanVar(value=False); self.focus_before = tk.IntVar(value=0); self.focus_after = tk.IntVar(value=60)
        self.weekdays = [tk.BooleanVar(value=i < 5) for i in range(7)]
        def field(label, variable, values=None):
            ttk.Label(editor, text=label, style='Card.TLabel').pack(anchor='w', pady=(6, 3))
            widget = ttk.Combobox(editor, textvariable=variable, values=values) if values else ttk.Entry(editor, textvariable=variable)
            widget.pack(fill='x')
            return widget
        self.title_entry = field('What’s the task?', self.title_var)
        ttk.Label(editor, text='Details · use - [ ] for checklist items', style='Card.TLabel').pack(anchor='w', pady=(8, 3))
        self.description = self.color_widget(tk.Text(editor, height=4, width=28, wrap='word', font=('DejaVu Sans', 10), relief='flat', highlightthickness=1, padx=8, pady=6))
        self.description.pack(fill='x')
        field('Priority', self.priority, PRIORITIES).configure(state='readonly')
        self.editor_category = field('Category', self.category, ('Work', 'Health', 'Home'))
        field('Tags · comma separated', self.tags)
        field('Start date · YYYY-MM-DD', self.date_var)
        self.time_entry = field('Reminder times · 09:00, 18:00', self.time_var)
        field('Repeat', self.repeat, REPEAT_LABELS).configure(state='readonly')
        repeat_row = ttk.Frame(editor, style='Card.TFrame'); repeat_row.pack(fill='x', pady=(8, 3))
        ttk.Label(repeat_row, text='Every', style='Card.TLabel').pack(side='left')
        ttk.Spinbox(repeat_row, from_=1, to=365, width=5, textvariable=self.interval).pack(side='left', padx=6)
        ttk.Label(repeat_row, text='days / weeks / months', style='Card.TLabel').pack(side='left')
        weekdays = ttk.Frame(editor, style='Card.TFrame'); weekdays.pack(fill='x', pady=4)
        for label, variable in zip(('M', 'T', 'W', 'T', 'F', 'S', 'S'), self.weekdays): ttk.Checkbutton(weekdays, text=label, variable=variable).pack(side='left')
        ttk.Label(editor, text='Weekday choices apply to Weekly / selected days.', style='Card.TLabel', wraplength=340).pack(anchor='w')
        field('Repeat until · optional YYYY-MM-DD (inclusive)', self.end_day)
        ttk.Checkbutton(editor, text='Focus mode: restrict check-in to reminder window', variable=self.focus_mode).pack(anchor='w', pady=(12, 4))
        focus_row = ttk.Frame(editor, style='Card.TFrame'); focus_row.pack(fill='x')
        ttk.Label(focus_row, text='Minutes before', style='Card.TLabel').pack(side='left')
        ttk.Spinbox(focus_row, from_=0, to=1440, width=5, textvariable=self.focus_before).pack(side='left', padx=5)
        ttk.Label(focus_row, text='after', style='Card.TLabel').pack(side='left')
        ttk.Spinbox(focus_row, from_=1, to=10080, width=5, textvariable=self.focus_after).pack(side='left', padx=5)
        self.save_button = ttk.Button(buttons, text='+ Add task', command=self.save, style='Primary.TButton'); self.save_button.pack(fill='x', pady=(0, 4))
        ttk.Button(buttons, text='Clear / New task', command=self.new).pack(fill='x')
        bind_wheel(editor)

    def make_calendar(self):
        bar = ttk.Frame(self.calendar_tab); bar.pack(fill='x', pady=(0, 14))
        ttk.Button(bar, text='‹ Previous', command=lambda: self.move_month(-1)).pack(side='left')
        self.month_title = tk.StringVar(); ttk.Label(bar, textvariable=self.month_title, font=('DejaVu Sans', 18, 'bold')).pack(side='left', padx=20)
        ttk.Button(bar, text='Next ›', command=lambda: self.move_month(1)).pack(side='left')
        ttk.Button(bar, text='Connect calendars…', style='Primary.TButton', command=self.connect_calendars).pack(side='right')
        ttk.Button(bar, text='Export month…', command=self.export_calendar_month).pack(side='right', padx=8)
        self.calendar_connection_status = tk.StringVar()
        ttk.Label(self.calendar_tab, textvariable=self.calendar_connection_status, style='Muted.TLabel', wraplength=1150).pack(anchor='w', pady=(0, 8))
        self.update_calendar_status()
        agenda = ttk.Frame(self.calendar_tab); agenda.pack(side='bottom', fill='x')
        agenda_bar = ttk.Frame(agenda); agenda_bar.pack(fill='x', pady=(10, 4))
        self.calendar_agenda_title = tk.StringVar()
        ttk.Label(agenda_bar, textvariable=self.calendar_agenda_title, font=('DejaVu Sans', 12, 'bold')).pack(side='left')
        ttk.Button(agenda_bar, text='View tasks', command=lambda: self.open_day(self.calendar_day.isoformat())).pack(side='right')
        ttk.Button(agenda_bar, text='Create reminder', command=self.reminder_from_calendar).pack(side='right', padx=8)
        ttk.Button(agenda_bar, text='Details', command=self.calendar_event_details).pack(side='right')
        self.style.configure('Calendar.Treeview', rowheight=26)
        self.calendar_agenda = ttk.Treeview(agenda, columns=('time', 'title', 'calendar'), show='headings', height=3, selectmode='browse', style='Calendar.Treeview')
        for column, title, width in [('time', 'Time', 90), ('title', 'Event / task', 550), ('calendar', 'Calendar', 260)]:
            self.calendar_agenda.heading(column, text=title); self.calendar_agenda.column(column, width=width)
        self.calendar_agenda.pack(fill='x'); self.calendar_agenda.bind('<Double-1>', self.calendar_event_details)
        self.calendar_agenda_empty = tk.StringVar()
        ttk.Label(agenda, textvariable=self.calendar_agenda_empty, style='Muted.TLabel').pack(anchor='w', pady=(4, 0))
        self.month_grid = ttk.Frame(self.calendar_tab); self.month_grid.pack(fill='both', expand=True)

    def move_month(self, step):
        index = self.calendar_month.year * 12 + self.calendar_month.month - 1 + step
        self.calendar_month = date(index // 12, index % 12 + 1, 1)
        self.calendar_day = self.calendar_month
        self.render_calendar()

    def render_calendar(self):
        self.update_calendar_status()
        events = self.calendar_month_events()
        self.render_calendar_agenda()
        days_to_show = []
        now = datetime.now()
        for week, days in enumerate(calendar.monthcalendar(self.calendar_month.year, self.calendar_month.month), 1):
            for col, number in enumerate(days):
                if not number: continue
                day = self.calendar_month.replace(day=number).isoformat()
                rows = self.store.rows(day, now=now)
                done = sum(bool(r['done']) for r in rows)
                late = sum(self.store.status(r, now) == 'late' for r in rows)
                text = f'{number}\n{done}/{len(rows)} done' if rows else f'{number}\n—'
                if late: text += f'\n{late} late'
                event_count = len(on_day(events, date.fromisoformat(day)))
                if event_count: text = text.replace(str(number), f'{number} · {event_count} events', 1)
                bg = '#25483a' if rows and done == len(rows) else ('#483b2a' if late else self.colors['card'])
                fg = '#ffffff' if bg != self.colors['card'] else self.colors['text']
                days_to_show.append((week, col, day, text, bg, fg))
        signature = (self.calendar_month, self.colors['select'], self.colors['text'], tuple(days_to_show))
        if signature == self.calendar_signature:
            return
        self.calendar_signature = signature
        for child in self.month_grid.winfo_children(): child.destroy()
        self.month_title.set(self.calendar_month.strftime('%B %Y'))
        for col, name in enumerate(calendar.day_abbr):
            ttk.Label(self.month_grid, text=name, anchor='center').grid(row=0, column=col, sticky='ew', pady=5)
            self.month_grid.columnconfigure(col, weight=1, uniform='day')
        for week, days in enumerate(calendar.monthcalendar(self.calendar_month.year, self.calendar_month.month), 1):
            self.month_grid.rowconfigure(week, weight=1, uniform='week')
        for week, col, day, text, bg, fg in days_to_show:
            button = tk.Button(self.month_grid, text=text, bg=bg, fg=fg, relief='flat', font=('DejaVu Sans', 10), command=lambda d=day: self.select_calendar_day(d), activebackground=self.colors['select'], activeforeground=self.colors['text'])
            button.grid(row=week, column=col, sticky='nsew', padx=3, pady=3)

    def open_day(self, day):
        self.day.set(day); self.notebook.select(self.task_tab); self.refresh()

    def make_stats(self):
        ttk.Label(self.stats_tab, text='Keep your rhythm', font=('DejaVu Sans', 21, 'bold')).pack(anchor='w', pady=(5, 12))
        self.stats_task = tk.StringVar(value='All tasks')
        self.stats_box = ttk.Combobox(self.stats_tab, textvariable=self.stats_task, state='readonly', width=45)
        self.stats_box.pack(anchor='w'); self.stats_box.bind('<<ComboboxSelected>>', lambda _: self.render_stats())
        self.stats_text = tk.StringVar()
        ttk.Label(self.stats_tab, textvariable=self.stats_text, font=('DejaVu Sans', 17), justify='left').pack(anchor='w', pady=25)
        ttk.Label(self.stats_tab, text='A streak counts scheduled days with every reminder complete. Unscheduled days do not break it.\nToday stays in progress until midnight. On-time % counts all reminders already due, including missed ones.\nWeek starts Monday. Month means the current calendar month. History uses the current schedule (up to 10 years).', style='Muted.TLabel', wraplength=1000, justify='left').pack(anchor='w')

    def render_stats(self):
        task_id = int(self.stats_task.get().split(' · ', 1)[0][1:]) if self.stats_task.get() != 'All tasks' else None
        stats = self.store.statistics(task_id=task_id)
        week = f"{stats['week_percent']}% ({stats['week'][0]}/{stats['week'][1]})" if stats['week'][1] else 'No reminders due yet'
        month = f"{stats['month_percent']}% ({stats['month'][0]}/{stats['month'][1]})" if stats['month'][1] else 'No reminders due yet'
        self.stats_text.set(f"Current streak     {stats['current']} scheduled days\n\nBest streak           {stats['best']} scheduled days\n\nOn time this week     {week}\n\nOn time this month    {month}\n\nTracked time          {duration(stats['tracked'])}")

    def make_settings(self):
        frame = self.settings_tab
        ttk.Label(frame, text='Make it yours', font=('DejaVu Sans', 19, 'bold')).pack(anchor='w', pady=(0, 12))
        self.theme_var = tk.StringVar(value=self.settings_values['theme'])
        ttk.Label(frame, text='Appearance').pack(anchor='w')
        ttk.Combobox(frame, textvariable=self.theme_var, values=('System', 'Light', 'Dark'), state='readonly', width=20).pack(anchor='w', pady=(4, 12))
        self.sound_var = tk.StringVar(value=self.settings_values['sound'])
        ttk.Label(frame, text='Notification sound · blank = silent, system = desktop default, or select a file').pack(anchor='w')
        sound_row = ttk.Frame(frame); sound_row.pack(fill='x', pady=(4, 12))
        sounds = ['', 'system'] + [str(p) for p in sorted(Path('/usr/share/sounds/freedesktop/stereo').glob('*.oga'))]
        ttk.Combobox(sound_row, textvariable=self.sound_var, values=sounds, width=55).pack(side='left')
        ttk.Button(sound_row, text='Browse…', command=self.choose_sound).pack(side='left', padx=8)
        ttk.Button(sound_row, text='▶ Preview', command=lambda: desktop.play_sound(self.sound_var.get())).pack(side='left')
        self.tts_var = tk.BooleanVar(value=self.settings_values['tts']); self.lock_var = tk.BooleanVar(value=self.settings_values['lock_guard'])
        ttk.Checkbutton(frame, text='Read reminder titles aloud with local eSpeak / eSpeak NG', variable=self.tts_var).pack(anchor='w', pady=6)
        ttk.Checkbutton(frame, text='Block check-ins from notifications while the screen is locked', variable=self.lock_var).pack(anchor='w', pady=6)
        ttk.Button(frame, text='Apply settings', style='Primary.TButton', command=self.save_settings).pack(anchor='w', pady=(12, 20))
        ttk.Separator(frame).pack(fill='x')
        ttk.Label(frame, text='Your data, under your control', font=('DejaVu Sans', 16, 'bold')).pack(anchor='w', pady=(20, 10))
        exports = ttk.Frame(frame); exports.pack(anchor='w')
        for label, command in [('Export JSON', lambda: self.export_data('json')), ('Export CSV', lambda: self.export_data('csv')), ('Import…', self.import_data), ('Back up now…', self.backup_now)]:
            ttk.Button(exports, text=label, command=command).pack(side='left', padx=(0, 8))
        ttk.Label(frame, text='JSON includes tasks and check-in history. CSV includes task definitions. Imports add copies; existing tasks stay.\nA snapshot is saved before each import and schema upgrade. Automatic daily SQLite backups retain 30 days.\nBackups are stored beside tasks.sqlite3 in the backups folder. Timers keep running when the window closes.', style='Muted.TLabel', wraplength=1050, justify='left').pack(anchor='w', pady=14)
        actions = ttk.Frame(frame); actions.pack(anchor='w', pady=10)
        ttk.Button(actions, text='Pause / resume reminders', command=self.toggle_service).pack(side='left', padx=(0, 8))
        ttk.Button(actions, text='Quit app and reminders', style='Danger.TButton', command=self.quit_all).pack(side='left')

    def choose_sound(self):
        path = filedialog.askopenfilename(parent=self, title='Choose notification sound', filetypes=[('Audio', '*.wav *.oga *.ogg *.mp3 *.flac'), ('All files', '*')])
        if path: self.sound_var.set(path)

    def save_settings(self):
        try:
            self.store.set_settings(theme=self.theme_var.get(), sound=self.sound_var.get().strip(), tts=self.tts_var.get(), lock_guard=self.lock_var.get())
            self.settings_values = self.store.settings(); self.apply_theme(); self.feedback.set('Settings saved. Sound and speech apply to the next reminder.')
        except ValueError as error: self.error(error)

    def export_data(self, kind):
        path = filedialog.asksaveasfilename(parent=self, title='Export tasks', defaultextension='.' + kind, initialfile='dailycheck.' + kind, filetypes=[(kind.upper(), '*.' + kind)])
        if path:
            try: self.store.export_file(path); self.feedback.set('Export saved to ' + path)
            except (OSError, ValueError) as error: self.error(error)

    def import_data(self):
        path = filedialog.askopenfilename(parent=self, title='Import Daily Check export', filetypes=[('Daily Check exports', '*.json *.csv')])
        if not path: return
        if not messagebox.askyesno('Import task copies?', 'Import adds copies of tasks and their exported history. Existing tasks are kept. Importing twice creates duplicates.', parent=self): return
        try:
            count = self.store.import_file(path); self.refresh(); self.feedback.set(f'Imported {count} task copies. A pre-import backup was saved.')
        except (OSError, ValueError, TypeError, KeyError) as error: self.error(error)

    def backup_now(self):
        path = filedialog.asksaveasfilename(parent=self, title='Save SQLite backup', defaultextension='.sqlite3', initialfile=f'dailycheck-{date.today()}.sqlite3')
        if path:
            try: self.store.backup(path); self.feedback.set('Backup saved to ' + path)
            except (OSError, ValueError) as error: self.error(error)

    def valid_day(self):
        return date.fromisoformat(self.day.get()).isoformat()

    def move(self, step):
        try: self.day.set((date.fromisoformat(self.valid_day()) + timedelta(days=step)).isoformat()); self.refresh()
        except ValueError as error: self.error(error)

    def today(self):
        self.day.set(date.today().isoformat()); self.refresh()

    def set_category(self, category): self.category_filter.set(category)

    def queue_refresh(self):
        if not self.ready: return
        if self.refresh_job: self.after_cancel(self.refresh_job)
        self.refresh_job = self.after(150, self.refresh)

    def refresh(self):
        if self.refresh_job:
            self.after_cancel(self.refresh_job); self.refresh_job = None
        try:
            day = self.valid_day(); now = datetime.now()
            revision = self.current_revision(now)
            all_rows = self.store.rows(day, now=now)
            rows = self.store.rows(day, self.search.get(), self.state_filter.get(), self.category_filter.get(), self.tag_filter.get(), now)
        except ValueError as error: self.error(error); return
        selected = self.tree.selection()
        self.rows = {f"{r['id']}|{r['occurrence']}|{r['slot']}": r for r in rows}
        self.tree.delete(*self.tree.get_children())
        for key, row in self.rows.items():
            state = self.store.status(row, now)
            if row['done']:
                status = ('On time' if row['done'] <= row['due'] + ':00' else 'Done late') + ' · ' + datetime.fromisoformat(row['done']).strftime('%H:%M')
            elif row['snoozed']:
                status = 'Snoozed · ' + datetime.fromisoformat(row['snoozed']).strftime('%d %b %H:%M')
            else: status = 'Late' if state == 'late' else 'Pending'
            if row['occurrence'] != day: status += ' (from ' + row['occurrence'] + ')'
            self.tree.insert('', 'end', iid=key, values=(row['slot'], '● ' + row['priority'], row['title'], row['category'], status, duration(self.store.elapsed(row))), tags=(row['priority'], state))
        if selected and selected[0] in self.rows: self.tree.selection_set(selected[0])
        done = sum(bool(r['done']) for r in all_rows)
        late = sum(self.store.status(r, now) == 'late' for r in all_rows)
        self.summary.set(f'{date.fromisoformat(day):%a, %d %b} · {len(rows)} shown · {done}/{len(all_rows)} done')
        for variable, value in zip(self.stat_vars, (len(all_rows), done, late)): variable.set(str(value))
        self.progress['value'] = 100 * done / len(all_rows) if all_rows else 0
        if self.rows: self.empty.place_forget()
        else: self.empty.place(relx=.5, rely=.5, anchor='center')
        tasks = self.store.tasks()
        categories = sorted(set(t['category'] for t in tasks) | {'Work', 'Health', 'Home'})
        self.category_box.configure(values=['All', *categories]); self.editor_category.configure(values=categories)
        self.tag_box.configure(values=['All', *sorted({tag for t in tasks for tag in t['tags']})])
        for name, button in self.category_buttons.items(): button.configure(style='Primary.TButton' if self.category_filter.get() == name else 'TButton')
        values = ['All tasks'] + [f"#{t['id']} · {t['title']}" for t in tasks]
        self.stats_box.configure(values=values)
        if self.stats_task.get() not in values: self.stats_task.set('All tasks')
        self.update_actions(); self.render_checklist()
        self.tab_changed()
        self.refresh_revision = revision

    def current_revision(self, now=None):
        # data_version detects notification actions from the separate service;
        # total_changes detects edits on this connection. Due times use minutes.
        now = now or datetime.now()
        return (self.store.db.total_changes, self.store.db.execute('PRAGMA data_version').fetchone()[0],
                now.replace(second=0, microsecond=0))

    def selected_row(self):
        selected = self.tree.selection()
        return self.rows.get(selected[0]) if selected else None

    def update_actions(self):
        row = self.selected_row()
        for button in self.action_buttons: button.configure(state='normal' if row else 'disabled')
        self.timer_button.configure(state='normal' if row and not row['done'] else 'disabled', text='⏸ Pause timer' if row and row['started'] else '▶ Start timer')
        if row:
            self.action_buttons[0].configure(state='disabled' if row['done'] else 'normal')
            self.action_buttons[1].configure(state='normal' if row['done'] else 'disabled')
        self.focus_hint.set(f"Focus window: {row['focus_before']} min before / {row['focus_after']} min after reminder. Snoozing moves this window." if row and row['focus'] else '')

    def select(self, _event=None):
        row = self.selected_row(); self.update_actions(); self.render_checklist()
        if not row or self.edit_id == row['id']: return
        self.load_task(row['id'])

    def load_task(self, task_id):
        task = self.store.task(task_id)
        self.edit_id = task_id; self.editor_heading.set('Edit task'); self.save_button.configure(text='Save changes')
        self.title_var.set(task['title']); self.date_var.set(task['day']); self.time_var.set(', '.join(task['times']))
        self.description.delete('1.0', 'end'); self.description.insert('1.0', task['description'])
        self.priority.set(task['priority']); self.category.set(task['category']); self.tags.set(', '.join(task['tags']))
        kind = task['repeat_kind']
        label = {'once': 'Once', 'daily': 'Every day' if task['interval'] == 1 else 'Every N days', 'weekly': 'Weekly / selected days', 'monthly': 'Monthly'}[kind]
        if kind == 'weekly' and task['interval'] == 1 and task['weekdays'] == [0, 1, 2, 3, 4]: label = 'Weekdays'
        if kind == 'weekly' and task['interval'] == 1 and task['weekdays'] == [5, 6]: label = 'Weekends'
        self.repeat.set(label); self.interval.set(task['interval']); self.end_day.set(task['end_day']); self.daily.set(bool(task['daily']))
        for i, variable in enumerate(self.weekdays): variable.set(i in task['weekdays'])
        self.focus_mode.set(bool(task['focus'])); self.focus_before.set(task['focus_before']); self.focus_after.set(task['focus_after'])

    def new(self):
        self.notebook.select(self.task_tab)
        self.edit_id = None; self.editor_heading.set('Add a task'); self.save_button.configure(text='+ Add task')
        self.tree.selection_remove(*self.tree.selection()); self.title_var.set(''); self.description.delete('1.0', 'end')
        self.date_var.set(self.day.get()); self.time_var.set('09:00'); self.daily.set(True)
        self.priority.set('Med'); self.category.set('Home'); self.tags.set(''); self.repeat.set('Every day'); self.interval.set(1); self.end_day.set('')
        self.focus_mode.set(False); self.focus_before.set(0); self.focus_after.set(60)
        for i, variable in enumerate(self.weekdays): variable.set(i < 5)
        self.editor_canvas.yview_moveto(0)
        self.title_entry.focus_set(); self.update_actions(); self.render_checklist()

    def save(self):
        try:
            label = self.repeat.get()
            kind = 'once' if label == 'Once' else 'monthly' if label == 'Monthly' else 'weekly' if label in ('Weekly / selected days', 'Weekdays', 'Weekends') else 'daily'
            days = [i for i, var in enumerate(self.weekdays) if var.get()]
            if label == 'Weekdays': days = list(range(5))
            if label == 'Weekends': days = [5, 6]
            interval = 1 if label in ('Once', 'Every day', 'Weekdays', 'Weekends') else self.interval.get()
            task_id = self.store.save(self.title_var.get(), self.description.get('1.0', 'end'), self.date_var.get(), self.time_var.get(), kind != 'once', self.edit_id,
                            priority=self.priority.get(), category=self.category.get(), tags=self.tags.get(), repeat_kind=kind, interval=interval, weekdays=days, end_day=self.end_day.get().strip(),
                            focus=self.focus_mode.get(), focus_before=self.focus_before.get(), focus_after=self.focus_after.get())
        except (ValueError, tk.TclError) as error: self.error(error); return
        self.day.set(self.date_var.get()); self.new(); self.refresh(); self.feedback.set(f'Task #{task_id} saved. Each reminder time has its own check-in.')

    def act(self, callback, message):
        row = self.selected_row()
        if not row: return
        try:
            callback(row); self.refresh(); self.feedback.set(message)
        except ValueError as error: self.error(error)

    def done(self): self.act(lambda r: self.store.complete(r['id'], r['occurrence'], slot=r['slot'], locked=desktop.screen_locked() if self.integrate else False), 'Done! Completion time and tracked time saved.')
    def undo(self): self.act(lambda r: self.store.undo(r['id'], r['occurrence'], r['slot']), 'Check-in undone.')
    def snooze(self, value): self.act(lambda r: self.store.snooze(r['id'], r['occurrence'], value, slot=r['slot']), 'Reminder snoozed. Other reminder times keep their own schedule.')
    def toggle_timer(self): self.act(lambda r: self.store.track(r['id'], r['occurrence'], r['slot'], not bool(r['started'])), 'Timer updated. Only one timer runs at once.')

    def delete(self):
        row = self.selected_row()
        if row and messagebox.askyesno('Delete task?', 'Delete this task, every reminder time, and all its check-in history?', parent=self):
            self.store.delete(row['id']); self.new(); self.refresh()

    def render_checklist(self):
        for child in self.checklist_frame.winfo_children(): child.destroy()
        row = self.selected_row()
        if not row or not row['checklist']: return
        ttk.Label(self.checklist_frame, text='Checklist for this reminder', style='Card.TLabel').pack(anchor='w')
        for item in row['checklist'][:3]:
            variable = tk.BooleanVar(value=item['id'] in row['checklist_done'])
            def toggle(i=item['id'], v=variable, r=row):
                try: self.store.toggle_subtask(r['id'], r['occurrence'], i, v.get(), r['slot']); self.refresh()
                except ValueError as error: self.error(error)
            checkbox = ttk.Checkbutton(self.checklist_frame, text=item['text'], variable=variable, command=toggle, state='disabled' if row['done'] else 'normal')
            checkbox.variable = variable; checkbox.pack(anchor='w')
        if len(row['checklist']) > 3:
            ttk.Button(self.checklist_frame, text=f"View all {len(row['checklist'])} checklist items…", command=lambda: self.full_checklist(row)).pack(anchor='w')

    def full_checklist(self, row):
        window = tk.Toplevel(self); window.title('Checklist · ' + row['title']); window.geometry('520x450'); window.transient(self)
        canvas = tk.Canvas(window, bg=self.colors['card'], highlightthickness=0)
        scroll = ttk.Scrollbar(window, orient='vertical', command=canvas.yview); scroll.pack(side='right', fill='y')
        canvas.pack(fill='both', expand=True); canvas.configure(yscrollcommand=scroll.set)
        frame = ttk.Frame(canvas, style='Card.TFrame', padding=12); handle = canvas.create_window((0, 0), window=frame, anchor='nw')
        frame.bind('<Configure>', lambda _: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(handle, width=event.width))
        for item in row['checklist']:
            variable = tk.BooleanVar(value=item['id'] in row['checklist_done'])
            def toggle(i=item['id'], v=variable):
                try: self.store.toggle_subtask(row['id'], row['occurrence'], i, v.get(), row['slot']); self.refresh()
                except ValueError as error: self.error(error)
            button = ttk.Checkbutton(frame, text=item['text'], variable=variable, command=toggle, state='disabled' if row['done'] else 'normal')
            button.variable = variable; button.pack(anchor='w', pady=4)

    def update_timers(self):
        for key, row in self.rows.items():
            if row['started'] and self.tree.exists(key):
                self.tree.set(key, 'tracked', duration(self.store.elapsed(row)))
        self.after(1000, self.update_timers)

    def tick(self):
        self.store.auto_backup()
        self.refresh_calendars()
        if self.current_revision() != self.refresh_revision:
            self.refresh()
        elif self.notebook.select() == str(self.stats_tab) and self.store.db.execute('SELECT 1 FROM checks WHERE started IS NOT NULL LIMIT 1').fetchone():
            self.render_stats()
        self.after(5000, self.tick)

    def tab_changed(self, _event=None):
        if not self.ready: return
        page = self.notebook.select()
        if page == str(self.calendar_tab): self.render_calendar()
        elif page == str(self.stats_tab): self.render_stats()

    def show_task(self, task=0, day='', slot=''):
        self.deiconify(); self.lift(); self.notebook.select(self.task_tab)
        if task:
            try:
                self.search.set(''); self.category_filter.set('All'); self.state_filter.set('All'); self.tag_filter.set('All')
                self.day.set(day or date.today().isoformat()); self.refresh()
                key = f'{task}|{self.day.get()}|{slot or self.store.task(task)["times"][0]}'
                if self.tree.exists(key): self.tree.selection_set(key); self.tree.see(key)
                self.load_task(task); self.time_entry.focus_set()
                self.update_idletasks()
                self.editor_canvas.yview_moveto(max(0, (self.time_entry.winfo_y() - 40) / max(1, self.editor_frame.winfo_height())))
                self.feedback.set('Edit reminder times, then choose Save changes. This updates the task schedule.')
            except ValueError as error: self.error(error)
        else: self.search_entry.focus_set()

    def error(self, error):
        self.feedback.set(str(error)); messagebox.showerror('Daily Check', str(error), parent=self)

    def service_state(self):
        if not desktop.Gio: return None
        try: return desktop.call(desktop.bus(), desktop.SERVICE_NAME, '/org/dailycheck/Service', desktop.SERVICE_NAME, 'State').unpack()
        except desktop.GLib.Error: return None

    def refresh_service_status(self):
        state = self.service_state()
        if state:
            self.service_status.set(('Reminders paused' if state[0] else 'Reminders running') + (' · Tray icon active · Close the window to keep reminders running.' if state[1] else ' · This desktop has no active StatusNotifier tray host.'))
        elif not desktop.Gio: self.service_status.set('Basic reminders only. Install Python GObject bindings for notification buttons and a tray icon.')
        else: self.service_status.set('Reminder service is unavailable. Start app.py --reminders in a desktop session.')
        self.after(15000, self.refresh_service_status)

    def toggle_service(self):
        state = self.service_state()
        if state:
            desktop.call(desktop.bus(), desktop.SERVICE_NAME, '/org/dailycheck/Service', desktop.SERVICE_NAME, 'Pause', desktop.GLib.Variant('(b)', (not state[0],)))
            self.service_status.set('Reminders resumed.' if state[0] else 'Reminders paused.')
        else: self.feedback.set('No native reminder service is available.')

    def quit_all(self):
        if desktop.Gio:
            try: desktop.call(desktop.bus(), desktop.SERVICE_NAME, '/org/dailycheck/Service', desktop.SERVICE_NAME, 'Quit')
            except desktop.GLib.Error: pass
        self.destroy()

    def close_window(self):
        # The separately owned service and tray outlive the editor window.
        self.destroy()

    def destroy(self):
        if self.bridge: self.bridge.close(); self.bridge = None
        for job in self.tk.splitlist(self.tk.call('after', 'info')):
            self.after_cancel(job)
        if self.own_store: self.store.db.close(); self.own_store = False
        super().destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reminders', action='store_true')
    parser.add_argument('--edit', type=int, default=0)
    parser.add_argument('--day', default='')
    parser.add_argument('--slot', default='')
    parser.add_argument('--version', action='version', version='Daily Check ' + VERSION)
    args = parser.parse_args()
    if args.reminders:
        desktop.run_service(DATA); return
    if desktop.show_existing(args.edit, args.day, args.slot): return
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = DATA / 'reminders.log'
    with log.open('a') as output:
        os.chmod(log, 0o600)
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--reminders'], stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
    app = App(integrate=True)
    if args.edit: app.after(200, lambda: app.show_task(args.edit, args.day, args.slot))
    app.mainloop()


if __name__ == '__main__':
    main()
