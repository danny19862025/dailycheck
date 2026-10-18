"""Daily Check — task reminders and completion check-ins."""
from datetime import datetime, timedelta
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time
import tkinter as tk
from tkinter import ttk, messagebox
from store import Store

DATA = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'daily-check'


def reminders():
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / 'reminders.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        store = Store(DATA / 'tasks.sqlite3')
        while True:
            now = datetime.now()
            for row in store.pending(now):
                try:
                    result = subprocess.run(['notify-send', '--app-name=Daily Check', '--icon=appointment-soon',
                        '--', 'Time to check in: ' + row['title'],
                        f"Due {row['occurrence']} at {row['time']}\n{row['description']}\nOpen Daily Check to mark this task done."],
                        timeout=10, capture_output=True)
                    if result.returncode == 0:
                        store.notified(row['id'], row['occurrence'], now)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            time.sleep(20)


class App(tk.Tk):
    def __init__(self, store=None):
        super().__init__()
        self.title('Daily Check • Tasks & Reminders')
        self.geometry('1180x820')
        self.minsize(1060, 800)
        self.configure(bg='#f2f5fc')
        self.store = store or Store(DATA / 'tasks.sqlite3')
        self.edit_id = None
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('.', font=('DejaVu Sans', 10), background='#f2f5fc', foreground='#24324b')
        style.configure('TFrame', background='#f2f5fc')
        style.configure('Card.TFrame', background='white')
        style.configure('TLabel', background='#f2f5fc')
        style.configure('Card.TLabel', background='white')
        style.configure('Muted.TLabel', foreground='#62718a')
        style.configure('Section.TLabel', background='white', font=('DejaVu Sans', 16, 'bold'))
        style.configure('TButton', padding=(12, 6), background='#e8edf7', borderwidth=0)
        style.map('TButton', background=[('active', '#dce4f4')], foreground=[('disabled', '#8793a8')])
        for name, color, hover in [('Primary', '#5746d9', '#4432c4'), ('Success', '#177454', '#115d43')]:
            style.configure(name + '.TButton', background=color, foreground='white', font=('DejaVu Sans', 10, 'bold'))
            style.map(name + '.TButton', background=[('disabled', '#dbe1ed'), ('active', hover)], foreground=[('disabled', '#8793a8'), ('!disabled', 'white')])
        style.configure('Danger.TButton', foreground='#b8394d', background='#fff0f2')
        style.configure('TEntry', padding=6, fieldbackground='#f8faff', bordercolor='#dce3ef', lightcolor='#dce3ef', darkcolor='#dce3ef')
        style.configure('TCheckbutton', background='white', padding=(0, 8))
        style.map('TCheckbutton', background=[('active', 'white')])
        style.configure('Treeview', rowheight=48, background='white', fieldbackground='white', borderwidth=0, font=('DejaVu Sans', 10))
        style.configure('Treeview.Heading', background='#eef2fa', foreground='#62718a', padding=12, font=('DejaVu Sans', 10, 'bold'), relief='flat')
        style.map('Treeview', background=[('selected', '#e8e3ff')], foreground=[('selected', '#39278c')])
        style.configure('Daily.Horizontal.TProgressbar', background='#5746d9', troughcolor='#e5e9f4', borderwidth=0)
        frame = ttk.Frame(self, padding=24)
        frame.pack(fill='both', expand=True)
        hero = tk.Frame(frame, bg='#29265b', padx=24, pady=12)
        hero.pack(fill='x')
        tk.Label(hero, text='✓  DAILY CHECK', bg='#29265b', fg='#c5bfff', font=('DejaVu Sans', 10, 'bold')).pack(anchor='w')
        tk.Label(hero, text='A little progress, every day.', bg='#29265b', fg='white', font=('DejaVu Sans', 22, 'bold')).pack(anchor='w', pady=(5, 3))
        tk.Label(hero, text='Make a plan. Find your focus. Celebrate getting it done.', bg='#29265b', fg='#d1d4ef', font=('DejaVu Sans', 11)).pack(anchor='w')
        stats = ttk.Frame(frame)
        stats.pack(fill='x', pady=10)
        self.stat_vars = []
        for i, (label, bg, fg) in enumerate([('PLANNED', '#e9e6ff', '#5140b4'), ('COMPLETED', '#dcf4e9', '#166b4c'), ('NEEDS ATTENTION', '#fff0dc', '#995611')]):
            card = tk.Frame(stats, bg=bg, padx=18, pady=10)
            card.pack(side='left', fill='x', expand=True, padx=(0 if i == 0 else 12, 0))
            value = tk.StringVar(value='0')
            self.stat_vars.append(value)
            tk.Label(card, textvariable=value, bg=bg, fg=fg, font=('DejaVu Sans', 22, 'bold')).pack(side='left')
            tk.Label(card, text=label, bg=bg, fg=fg, font=('DejaVu Sans', 9, 'bold')).pack(side='left', padx=12)
        nav = ttk.Frame(frame)
        nav.pack(fill='x')
        self.day = tk.StringVar(value=datetime.now().date().isoformat())
        ttk.Button(nav, text='‹', width=3, command=lambda: self.move(-1)).pack(side='left')
        date_entry = ttk.Entry(nav, textvariable=self.day, width=12)
        date_entry.pack(side='left', padx=6)
        date_entry.bind('<Return>', lambda event: self.refresh())
        ttk.Button(nav, text='Go', command=self.refresh).pack(side='left')
        ttk.Button(nav, text='›', width=3, command=lambda: self.move(1)).pack(side='left', padx=6)
        ttk.Button(nav, text='Today', command=self.today).pack(side='left')
        self.summary = tk.StringVar()
        ttk.Label(nav, textvariable=self.summary, style='Muted.TLabel').pack(side='right')
        body = ttk.Frame(frame)
        left = ttk.Frame(body, style='Card.TFrame', padding=16)
        left.pack(side='left', fill='both', expand=True)
        top = ttk.Frame(left, style='Card.TFrame')
        top.pack(fill='x', pady=(0,12))
        ttk.Label(top, text='Your tasks', style='Section.TLabel').pack(side='left')
        ttk.Button(top, text='+ New task', command=self.new).pack(side='right')
        self.progress = ttk.Progressbar(left, style='Daily.Horizontal.TProgressbar', maximum=100)
        self.progress.pack(fill='x', pady=(0,12))
        table = ttk.Frame(left, style='Card.TFrame')
        self.tree = ttk.Treeview(table, columns=('time', 'task', 'status'), show='headings', selectmode='browse')
        for name, label, width in [('time','TIME',70), ('task','TASK',200), ('status','STATUS',170)]:
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=60)
        scrollbar = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.empty = tk.Label(self.tree, text='Your day is a fresh start.\n\nAdd a task on the right to get going →', bg='white', fg='#62718a', font=('DejaVu Sans', 12), justify='center')
        self.tree.tag_configure('done', foreground='#166b4c', background='#effaf4')
        self.tree.tag_configure('late', foreground='#995611', background='#fff6e9')
        self.tree.tag_configure('upcoming', foreground='#4d56a4', background='#f3f5ff')
        self.tree.bind('<<TreeviewSelect>>', self.select)
        actions = ttk.Frame(left, style='Card.TFrame')
        actions.pack(side='bottom', fill='x', pady=(12,0))
        table.pack(fill='both', expand=True)
        self.action_buttons = []
        for label, action, button_style in [('✓ Mark done', self.done, 'Success.TButton'), ('Undo', self.undo, 'TButton'), ('Delete', self.delete, 'Danger.TButton')]:
            button = ttk.Button(actions, text=label, command=action, style=button_style, state='disabled')
            button.pack(side='left', padx=(0,6))
            self.action_buttons.append(button)
        editor = ttk.Frame(body, style='Card.TFrame', padding=14)
        editor.pack(side='right', fill='y', padx=(16,0))
        self.editor_heading = tk.StringVar(value='Add a task')
        ttk.Label(editor, textvariable=self.editor_heading, style='Section.TLabel').pack(anchor='w', pady=(0,6))
        self.title_var = tk.StringVar()
        self.date_var = tk.StringVar(value=self.day.get())
        self.time_var = tk.StringVar(value='09:00')
        self.daily = tk.BooleanVar(value=True)
        ttk.Label(editor, text='What’s the task?', style='Card.TLabel').pack(anchor='w')
        self.title_entry = ttk.Entry(editor, textvariable=self.title_var, width=28)
        self.title_entry.pack(fill='x', pady=(3,6))
        ttk.Label(editor, text='Details (optional)', style='Card.TLabel').pack(anchor='w')
        self.description = tk.Text(editor, width=28, height=2, wrap='word', font=('DejaVu Sans',10), bg='#f8faff', fg='#24324b', relief='flat', highlightthickness=1, highlightbackground='#dce3ef', highlightcolor='#5746d9', padx=10, pady=4)
        self.description.pack(fill='x', pady=(3,6))
        ttk.Label(editor, text='Start date · YYYY-MM-DD', style='Card.TLabel').pack(anchor='w')
        ttk.Entry(editor, textvariable=self.date_var).pack(fill='x', pady=(5,5))
        shortcuts = ttk.Frame(editor, style='Card.TFrame')
        shortcuts.pack(fill='x', pady=(0,6))
        for label, offset in [('Today',0), ('Tomorrow',1)]:
            ttk.Button(shortcuts, text=label, command=lambda n=offset: self.date_var.set((datetime.now() + timedelta(days=n)).date().isoformat())).pack(side='left', padx=(0,6))
        ttk.Label(editor, text='Remind me at · 24-hour time', style='Card.TLabel').pack(anchor='w')
        ttk.Entry(editor, textvariable=self.time_var).pack(fill='x', pady=(5,4))
        ttk.Checkbutton(editor, text='Repeat every day', variable=self.daily).pack(anchor='w')
        self.save_button = ttk.Button(editor, text='+ Add task', command=self.save, style='Primary.TButton')
        self.save_button.pack(fill='x', pady=(8,0))
        self.feedback = tk.StringVar(value='Ready when you are. Add a task to plan your day.')
        ttk.Label(frame, textvariable=self.feedback, foreground='#5140b4').pack(side='bottom', anchor='w')
        ttk.Label(frame, text='● Reminders stay on after closing · Every 15 minutes until done · Computer must be awake', style='Muted.TLabel').pack(side='bottom', anchor='w', pady=(6,0))
        body.pack(fill='both', expand=True, pady=12)
        self.bind('<Control-n>', lambda event: self.new())
        self.bind('<Control-s>', lambda event: self.save())
        self.refresh()
        self.after(20000, self.tick)

    def valid_day(self):
        return datetime.strptime(self.day.get(), '%Y-%m-%d').date().isoformat()

    def move(self, step):
        try:
            self.day.set((datetime.fromisoformat(self.valid_day()) + timedelta(days=step)).date().isoformat())
            self.refresh()
        except ValueError:
            messagebox.showerror('Invalid date', 'Use YYYY-MM-DD.')

    def today(self):
        self.day.set(datetime.now().date().isoformat())
        self.refresh()

    def refresh(self):
        try:
            day = self.valid_day()
        except ValueError:
            messagebox.showerror('Invalid date', 'Use YYYY-MM-DD.')
            return
        selected = self.tree.selection()
        self.rows = {str(r['id']): r for r in self.store.rows(day)}
        self.tree.delete(*self.tree.get_children())
        completed = 0
        overdue = 0
        for key, r in self.rows.items():
            due = datetime.fromisoformat(day + 'T' + r['time'])
            if r['done']:
                completed += 1
                stamp = datetime.fromisoformat(r['done'])
                status = ('On time · ' if stamp <= due else 'Done late · ') + stamp.strftime('%H:%M')
                tag = 'done'
            else:
                status = 'Overdue' if datetime.now() > due else 'Upcoming'
                tag = 'late' if status == 'Overdue' else 'upcoming'
                overdue += int(status == 'Overdue')
            self.tree.insert('', 'end', iid=key, values=(r['time'], r['title'], status), tags=(tag,))
        if selected and selected[0] in self.rows:
            self.tree.selection_set(selected[0])
        self.summary.set(f'{datetime.fromisoformat(day):%A, %d %B}  ·  {completed} of {len(self.rows)} complete')
        for var, value in zip(self.stat_vars, (len(self.rows), completed, overdue)):
            var.set(str(value))
        self.progress['value'] = completed / len(self.rows) * 100 if self.rows else 0
        if self.rows:
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.5, anchor='center')
        self.update_actions()

    def update_actions(self):
        selected = self.tree.selection()
        row = self.rows.get(selected[0]) if selected else None
        for button in self.action_buttons:
            button.configure(state='normal' if row else 'disabled')
        if row:
            self.action_buttons[0].configure(state='disabled' if row['done'] else 'normal')
            self.action_buttons[1].configure(state='normal' if row['done'] else 'disabled')

    def select(self, event=None):
        self.update_actions()
        selected = self.tree.selection()
        if not selected:
            return
        r = self.rows[selected[0]]
        if self.edit_id == r['id']:
            return
        self.edit_id = r['id']
        self.editor_heading.set('Edit task')
        self.save_button.configure(text='Save changes')
        self.title_var.set(r['title'])
        self.date_var.set(r['day'])
        self.time_var.set(r['time'])
        self.daily.set(bool(r['daily']))
        self.description.delete('1.0','end')
        self.description.insert('1.0',r['description'])

    def new(self):
        self.edit_id = None
        self.editor_heading.set('Add a task')
        self.save_button.configure(text='+ Add task')
        self.title_entry.focus_set()
        self.tree.selection_remove(*self.tree.selection())
        self.title_var.set('')
        self.description.delete('1.0','end')
        self.date_var.set(self.day.get())
        self.time_var.set('09:00')
        self.daily.set(True)

    def save(self):
        try:
            self.store.save(self.title_var.get(), self.description.get('1.0','end'), self.date_var.get(), self.time_var.get(), self.daily.get(), self.edit_id)
        except ValueError as error:
            messagebox.showerror('Check task details', str(error))
            return
        self.day.set(self.date_var.get())
        self.new()
        self.refresh()
        self.feedback.set('Task saved. Its reminder is scheduled.')

    def done(self):
        selected = self.tree.selection()
        if selected:
            if self.valid_day() > datetime.now().date().isoformat():
                messagebox.showinfo('Future task', 'Check in on the day of the task.')
                return
            self.store.complete(int(selected[0]), self.valid_day())
            self.refresh()
            self.feedback.set('Done! Your completion time has been recorded.')

    def undo(self):
        selected = self.tree.selection()
        if selected:
            self.store.undo(int(selected[0]), self.valid_day())
            self.refresh()

    def delete(self):
        selected = self.tree.selection()
        if selected and messagebox.askyesno('Delete task', 'Delete this task and its check-in history, including all daily repeats?'):
            self.store.delete(int(selected[0]))
            self.new()
            self.refresh()

    def tick(self):
        # Refresh status without discarding unsaved editor text.
        self.refresh()
        self.after(20000, self.tick)


if __name__ == '__main__':
    if '--reminders' in sys.argv:
        reminders()
    else:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--reminders'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        App().mainloop()
