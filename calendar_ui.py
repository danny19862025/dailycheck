"""Calendar connection controls and a combined daily agenda for the Tk application."""
import calendar
from datetime import date, datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import webbrowser

from calendar_sync import Calendars, SyncWorker, export_month, on_day

PROVIDERS = {
    'Google Calendar': ('Google Calendar → Settings → select a calendar → Integrate calendar → Secret address in iCal format.', 'https://support.google.com/calendar/answer/37648'),
    'Notion Calendar': ('Connect the Google, Outlook, or iCloud calendar used in Notion Calendar using its iCalendar feed. Notion database views are not calendar feeds.', 'https://www.notion.com/help/create-a-notion-calendar-account'),
    'Outlook': ('Outlook on the web → Settings → Calendar → Shared calendars → Publish a calendar → copy the ICS link.', 'https://support.microsoft.com/en-us/office/share-your-calendar-in-outlook-on-the-web-7ecef8ae-139c-40d9-bae2-a23977ee58d5'),
    'iCloud': ('iCloud Calendar → calendar sharing → copy the published calendar link (webcal). Publishing makes that calendar accessible to people with the link.', 'https://support.apple.com/guide/icloud/share-a-calendar-mm6b1a9479/icloud'),
    'Other calendar feed': ('Paste a direct HTTPS or webcal iCalendar (.ics) subscription URL. Sign-in pages and CalDAV account URLs are not feeds.', 'https://icalendar.org/'),
}


class CalendarConnectionsMixin:
    def init_calendar_connections(self):
        self.calendars = Calendars(self.store.db)
        self.calendar_worker = SyncWorker(self.store.path)
        self.calendar_day = date.today()
        self.calendar_dialog = None
        self.calendar_poll_job = None

    def calendar_month_events(self):
        last = self.calendar_month + timedelta(days=calendar.monthrange(self.calendar_month.year, self.calendar_month.month)[1])
        try:
            return self.calendars.events(self.calendar_month, last)
        except Exception:
            self.calendar_connection_status.set('Calendar events could not be displayed. Open Connect calendars to check the connection.')
            return []

    def update_calendar_status(self):
        sources = self.calendars.sources()
        failures = sum(bool(source['error']) for source in sources)
        if not sources:
            text = 'No calendars connected · Connect a local file, Google, Outlook, iCloud, or a calendar used in Notion.'
        else:
            text = f'{len(sources)} calendar(s) connected · Read-only · Refresh every 15 minutes'
            if failures: text += f' · {failures} need attention (cached events still shown)'
        self.calendar_connection_status.set(text)

    def connect_calendars(self):
        if self.calendar_dialog and self.calendar_dialog.winfo_exists():
            self.calendar_dialog.lift(); return
        window = self.calendar_dialog = tk.Toplevel(self)
        window.title('Connect calendars — Daily Check'); window.geometry('860x740'); window.minsize(780, 720)
        window.transient(self)
        frame = ttk.Frame(window, padding=20); frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Your calendars, together', font=('DejaVu Sans', 19, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='View connected events alongside your Daily Check tasks. Changes stay in the source calendar.\nUse “Create reminder” to turn an event into a Daily Check task.', wraplength=800, justify='left').pack(anchor='w', pady=(8, 12))
        self.connections_tree = ttk.Treeview(frame, columns=('kind', 'status'), show='tree headings', height=3, selectmode='browse', style='Calendar.Treeview')
        self.connections_tree.heading('#0', text='Calendar'); self.connections_tree.column('#0', width=220)
        self.connections_tree.heading('kind', text='Connection'); self.connections_tree.column('kind', width=110)
        self.connections_tree.heading('status', text='Status'); self.connections_tree.column('status', width=430)
        self.connections_tree.pack(fill='x')
        self.connections_tree.bind('<<TreeviewSelect>>', self.calendar_connection_details)
        actions = ttk.Frame(frame); actions.pack(fill='x', pady=8)
        ttk.Button(actions, text='Refresh calendars', command=lambda: self.refresh_calendars(force=True)).pack(side='left')
        ttk.Button(actions, text='Disconnect selected', command=self.disconnect_calendar).pack(side='left', padx=8)
        self.connection_message = tk.StringVar()
        ttk.Label(frame, textvariable=self.connection_message, wraplength=800, style='Muted.TLabel').pack(anchor='w', pady=(0, 8))
        local = ttk.LabelFrame(frame, text='Local calendar', padding=10); local.pack(fill='x', pady=(0, 10))
        ttk.Label(local, text='Choose an .ics file from your calendar app. File changes appear on refresh.').pack(side='left')
        ttk.Button(local, text='Choose local .ics…', command=self.add_local_calendar).pack(side='right')
        cloud = ttk.LabelFrame(frame, text='Cloud calendar', padding=10); cloud.pack(fill='x')
        self.calendar_provider = tk.StringVar(value='Google Calendar')
        provider = ttk.Combobox(cloud, textvariable=self.calendar_provider, values=list(PROVIDERS), state='readonly', width=25)
        provider.pack(anchor='w')
        self.calendar_provider_help = tk.StringVar()
        def provider_changed(_event=None): self.calendar_provider_help.set(PROVIDERS[self.calendar_provider.get()][0])
        provider.bind('<<ComboboxSelected>>', provider_changed); provider_changed()
        ttk.Label(cloud, textvariable=self.calendar_provider_help, wraplength=760, justify='left', style='Muted.TLabel').pack(anchor='w', pady=8)
        fields = ttk.Frame(cloud); fields.pack(fill='x')
        ttk.Label(fields, text='Name').grid(row=0, column=0, sticky='w', padx=(0, 8))
        self.calendar_name = tk.StringVar()
        ttk.Entry(fields, textvariable=self.calendar_name, width=45).grid(row=0, column=1, sticky='ew', pady=3)
        ttk.Label(fields, text='Feed URL').grid(row=1, column=0, sticky='w', padx=(0, 8))
        self.calendar_url = tk.StringVar()
        ttk.Entry(fields, textvariable=self.calendar_url, show='•').grid(row=1, column=1, sticky='ew', pady=3)
        fields.columnconfigure(1, weight=1)
        buttons = ttk.Frame(cloud); buttons.pack(fill='x', pady=(8, 0))
        ttk.Button(buttons, text='Connect cloud calendar', style='Primary.TButton', command=self.add_cloud_calendar).pack(side='left')
        ttk.Button(buttons, text='Provider help ↗', command=lambda: webbrowser.open(PROVIDERS[self.calendar_provider.get()][1])).pack(side='left', padx=8)
        ttk.Label(frame, text='Feed links and cached events are stored in your private local database. No cloud sign-in is required.\nConnections read events; use Export month to transfer Daily Check tasks to another calendar.', wraplength=800, justify='left', style='Muted.TLabel').pack(anchor='w', pady=(12, 0))
        self.populate_calendar_connections()

    def calendar_connection_details(self, _event=None):
        selected = self.connections_tree.selection()
        if not selected: return
        source = next((source for source in self.calendars.sources() if str(source['id']) == selected[0]), None)
        if source:
            self.connection_message.set(source['error'] or (f'Last updated: {source["updated"].replace("T", " ")}' if source['updated'] else 'Waiting for first refresh'))

    def populate_calendar_connections(self):
        self.update_calendar_status()
        if not self.calendar_dialog or not self.calendar_dialog.winfo_exists(): return
        selected = self.connections_tree.selection()
        self.connections_tree.delete(*self.connections_tree.get_children())
        for source in self.calendars.sources():
            status = source['error'] or (f'Updated {source["updated"].replace("T", " ")}' if source['updated'] else 'Waiting for first refresh')
            self.connections_tree.insert('', 'end', iid=str(source['id']), text=source['name'], values=('Local file' if source['kind'] == 'local' else 'Online feed', status))
        if selected and self.connections_tree.exists(selected[0]): self.connections_tree.selection_set(selected[0])

    def add_local_calendar(self):
        initial = Path.home() / '.local/share/evolution/calendar/system'
        path = filedialog.askopenfilename(parent=self.calendar_dialog, title='Connect a local calendar file', filetypes=[('iCalendar files', '*.ics')], initialdir=str(initial if initial.is_dir() else Path.home()))
        if not path: return
        try:
            self.calendars.add(Path(path).stem, 'local', path)
            self.refresh_calendars(force=True)
        except ValueError as error: messagebox.showerror('Calendar connection', str(error), parent=self.calendar_dialog)

    def add_cloud_calendar(self):
        try:
            self.calendars.add(self.calendar_name.get() or self.calendar_provider.get(), 'cloud', self.calendar_url.get())
            self.calendar_url.set(''); self.calendar_name.set('')
            self.refresh_calendars(force=True)
        except ValueError as error: messagebox.showerror('Calendar connection', str(error), parent=self.calendar_dialog)

    def disconnect_calendar(self):
        selected = self.connections_tree.selection()
        if not selected: return
        self.calendars.remove(int(selected[0]))
        self.populate_calendar_connections(); self.render_calendar()
        self.connection_message.set('Disconnected. The source calendar and Daily Check tasks were kept.')

    def refresh_calendars(self, force=False):
        self.populate_calendar_connections()
        if not self.calendars.sources(): return
        started = self.calendar_worker.start(force)
        if not started and not (self.calendar_worker.thread and self.calendar_worker.thread.is_alive()): return
        self.calendar_connection_status.set('Refreshing calendars… You can keep using Daily Check.')
        if self.calendar_dialog and self.calendar_dialog.winfo_exists(): self.connection_message.set('Refreshing…')
        if self.calendar_poll_job is None:
            self.calendar_poll_job = self.after(150, self.poll_calendar_refresh)

    def poll_calendar_refresh(self):
        self.calendar_poll_job = None
        if self.calendar_worker.thread and self.calendar_worker.thread.is_alive():
            self.calendar_poll_job = self.after(150, self.poll_calendar_refresh); return
        self.populate_calendar_connections()
        if self.calendar_dialog and self.calendar_dialog.winfo_exists():
            self.connection_message.set(self.calendar_worker.error or 'Refresh finished. Connection status is shown above.')
        self.render_calendar()

    def select_calendar_day(self, day):
        self.calendar_day = date.fromisoformat(day)
        self.render_calendar_agenda()

    def render_calendar_agenda(self):
        self.calendar_agenda_title.set(self.calendar_day.strftime('%A, %d %B %Y'))
        self.calendar_agenda.delete(*self.calendar_agenda.get_children())
        self.agenda_rows = {}
        events = self.calendar_month_events()
        items = []
        for event in on_day(events, self.calendar_day):
            label = 'All day' if event['all_day'] else event['start'].strftime('%H:%M')
            if event['start'].date() < self.calendar_day: label = 'Continues'
            items.append((event['start'], label, event['title'], event['calendar'], ('event', event)))
        for row in self.store.rows(self.calendar_day.isoformat()):
            items.append((datetime.fromisoformat(row['due']), row['slot'], row['title'], 'Daily Check · ' + ('Done' if row['done'] else 'Pending'), ('task', row)))
        for i, (_, when, title, source, item) in enumerate(sorted(items, key=lambda item: (item[0], item[2]))):
            key = str(i); self.agenda_rows[key] = item
            self.calendar_agenda.insert('', 'end', iid=key, values=(when, title, source))
        self.calendar_agenda_empty.set('No events or tasks for this day.' if not items else f'{len(items)} event(s) and task(s) · Double-click a row for details')

    def selected_agenda_item(self):
        selected = self.calendar_agenda.selection()
        return self.agenda_rows.get(selected[0]) if selected else None

    def calendar_event_details(self, _event=None):
        item = self.selected_agenda_item()
        if not item: return
        kind, event = item
        if kind == 'task':
            self.show_task(event['id'], event['occurrence'], event['slot']); return
        when = event['start'].strftime('%Y-%m-%d' if event['all_day'] else '%Y-%m-%d %H:%M')
        details = f'{event["calendar"]}\n{when}\n\n{event["location"]}\n\n{event["description"]}'
        messagebox.showinfo(event['title'], details, parent=self)

    def reminder_from_calendar(self):
        item = self.selected_agenda_item()
        if not item:
            self.feedback.set('Select a connected calendar event first.'); return
        kind, event = item
        if kind == 'task': self.calendar_event_details(); return
        self.new()
        self.title_var.set(event['title'][:300]); self.date_var.set(self.calendar_day.isoformat())
        self.time_var.set('09:00' if event['all_day'] else event['start'].strftime('%H:%M'))
        self.repeat.set('Once'); self.daily.set(False); self.category.set('Calendar')
        self.description.insert('1.0', (event['description'] + '\n\n' + event['location'] + '\nCalendar: ' + event['calendar']).strip())
        self.feedback.set('Review this event, then choose Add task to save a reminder. The source calendar stays separate.')

    def export_calendar_month(self):
        path = filedialog.asksaveasfilename(parent=self, title='Export Daily Check tasks for this month', defaultextension='.ics', initialfile=f'dailycheck-{self.calendar_month:%Y-%m}.ics', filetypes=[('iCalendar', '*.ics')])
        if not path: return
        try:
            payload = export_month(self.store, self.calendar_month)
            with open(path, 'wb') as output: output.write(payload)
            self.feedback.set('Month exported. Import this .ics file into your local or cloud calendar. This is a snapshot of Daily Check tasks.')
        except (OSError, ValueError) as error: self.error(error)
