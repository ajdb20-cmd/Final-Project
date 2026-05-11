import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, timezone, timedelta
import calendar
import threading #runs Google API calls in the background so the UI never freezes

# GOOGLE API IMPORTS
from google.oauth2.credentials import Credentials # handles OAuth2 credential objects
from google_auth_oauthlib.flow import InstalledAppFlow # runs the local browser sign-in flow for first-time auth
from google.auth.transport.requests import Request # builds the actual Calendar API client
from googleapiclient.discovery import build 
import os, pickle #os finds file paths; pickle saves/loads the auth token to disk


'''
CLASS 1 — GoogleCalendarService
Responsible for ALL communication with the Google Calendar API.
The UI classes never touch the API directly — they call methods on this class.
'''
class GoogleCalendarService:

    # SCOPES — tells Google which permissions this app is requesting
    # "auth/calendar" = full read/write access to the user's calendar
    SCOPES = ["https://www.googleapis.com/auth/calendar"]

    # _SCRIPT_DIR — resolves the absolute path of the folder this file lives in
    # used so credential/token files are always found regardless of where Python is run from
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

    #  __init__ 
    # Sets up file paths for credentials.json and token.pickle
    # If a bare filename is passed (not an absolute path), it anchors it next to the script
    # _service starts as None — only works after authenticate() clears
    def __init__(self, credentials_path: str = "credentials.json"):
        if not os.path.isabs(credentials_path):
            credentials_path = os.path.join(self._SCRIPT_DIR, credentials_path)
        self.credentials_path = credentials_path
        self.TOKEN_FILE = os.path.join(self._SCRIPT_DIR, "token.pickle")
        self._service = None  # stays None until authenticate() completes successfully

    # authenticate()
    # Handles OAuth2 Sign in 
    #   1. Returns False immediately if credentials.json is missing
    #   2. Loads a saved token from token.pickle if one exists (skips browser re-login)
    #   3. Refreshes the token silently if it's expired but still has a refresh_token
    #   4. Opens a browser sign-in window if no valid token exists at all
    #   5. Saves the new/refreshed token back to token.pickle for next time
    #   6. Builds self._service — the live API client used by all other methods
    def authenticate(self) -> bool:
        if not os.path.exists(self.credentials_path):
            return False

        creds = None
        if os.path.exists(self.TOKEN_FILE):
            with open(self.TOKEN_FILE, "rb") as f:
                creds = pickle.load(f)  # loads previously saved credentials

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())  # refresh without opening browser
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_path, self.SCOPES
                )
                creds = flow.run_local_server(port=0)  # opens browser for sign-in

            with open(self.TOKEN_FILE, "wb") as f:
                pickle.dump(creds, f)  # save token so next run skips the browser step

        self._service = build("calendar", "v3", credentials=creds)
        return True

    # is_authenticated (property)
    # Returns True if the API client is ready, False if authenticate() hasn't run yet
    @property
    def is_authenticated(self) -> bool:
        return self._service is not None

    # get_events()
    '''gets upcoming events from the user's Google Calendar
        timeMin=now makes sure that only future events are returned
        singleEvents=True expands recurring events into individual occurrences
        Filters out auto-generated birthday events (they showed up as duplicates)
        Each raw Google event dict is passed through _translator() before returning'''
    def get_events(self, max_results: int = 20) -> list[dict]:
        self._require_auth()
        now = datetime.now(timezone.utc).isoformat()
        result = (
            self._service.events()
            .list(
                calendarId="primary",
                timeMin=now,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        # filter out Google's auto-generated birthday events from Contacts <- had a bug here, this fixed it 
        # makes sure that the event type which is birthday is excluded from the list
        items = [e for e in result.get("items", []) if e.get("eventType") != "birthday"]
        return [self._translator(e) for e in items]

    # add_event()
    '''Sends a new event to Google Calendar through a POST (insert) request
    Builds the event body dict with title, description, start, and end times
    .insert() packages the body; .execute() actually fires the HTTP request
    Returns the newly created event passed through _translator() for UI use'''
    def add_event(self, title: str, start_dt: datetime, end_dt: datetime, description: str = "") -> dict:
        self._require_auth()
        body = {
            "summary":     title,
            "description": description,
            "start": {"dateTime": start_dt.isoformat()},
            "end":   {"dateTime": end_dt.isoformat()},
        }
        created = (
            self._service.events()
            .insert(calendarId="primary", body=body)
            .execute()
        )
        return self._translator(created)

    # delete_event() 
    # Permanently removes an event from Google Calendar by its unique event ID
    # No return value — just fires the delete request and lets the UI update itself
    def delete_event(self, event_id: str) -> None:
        self._require_auth()
        self._service.events().delete(
            calendarId="primary", eventId=event_id
        ).execute()

    # _translator()
    # Converts a raw Google API event dict (which has many nested fields) into a
    # clean, flat dict containing only the 5 fields the UI actually needs:
    #   id, summary (title), start, end, description
    # start/end fall back to the "date" field for all-day events (no time component)
    def _translator(self, raw: dict) -> dict:
        start = raw.get("start", {})
        end   = raw.get("end",   {})
        return {
            "id":          raw.get("id", ""),
            "summary":     raw.get("summary", "(No title)"),
            "start":       start.get("dateTime", start.get("date", "")),
            "end":         end.get("dateTime",   end.get("date", "")),
            "description": raw.get("description", ""),
        }

    # _require_auth()
    # Internal safety guard — raises a clear RuntimeError if any method is called
    # before the API client has been built. Prevents cryptic NoneType crashes.
    def _require_auth(self):
        if not self.is_authenticated:
            raise RuntimeError("Call authenticate() before using the service.")


# CLASS 2 — DateTimePicker
# I reusable tkinter Frame widget that renders a full date and time selector, (makes a cleaner UI)
class DateTimePicker(tk.Frame):

    MONTHS = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ]

    # __init__ 
    # Builds the full picker row with these widgets left-to-right:
    #   Label | Month dropdown | Day dropdown | Year spinbox | Hour spinbox | : | Minute spinbox | AM/PM toggle
    # All fields default to the current date and time on first render
    def __init__(self, parent, label: str, bg: str = "#f0f4f7", **kwargs):
        super().__init__(parent, bg=bg, **kwargs)
        now = datetime.now()

        # Section label w start and end
        tk.Label(self, text=label, font=("Helvetica", 10, "bold"),
                 bg=bg, fg="black", width=6, anchor="w").grid(row=0, column=0, padx=(0, 6))

        # Month
        self._month_var = tk.StringVar(value=self.MONTHS[now.month - 1])
        month_cb = ttk.Combobox(self, textvariable=self._month_var, values=self.MONTHS,
                                state="readonly", width=10, font=("Helvetica", 10))
        month_cb.grid(row=0, column=1, padx=3)
        month_cb.bind("<<ComboboxSelected>>", self._on_month_year_change)

        # Day
        self._day_var = tk.StringVar(value=str(now.day))
        self._day_cb  = ttk.Combobox(self, textvariable=self._day_var,
                                     state="readonly", width=4, font=("Helvetica", 10))
        self._day_cb.grid(row=0, column=2, padx=3)
        self._update_days()  # populate day list for the current month immediately

        # Year
        self._year_var = tk.StringVar(value=str(now.year))
        ttk.Spinbox(self, textvariable=self._year_var, from_=now.year, to=now.year + 10,
                    width=6, font=("Helvetica", 10),
                    command=self._on_month_year_change).grid(row=0, column=3, padx=(3, 12))

        # Hour 
        self._hour_var = tk.StringVar(value=str(int(now.strftime("%I"))))
        ttk.Spinbox(self, textvariable=self._hour_var, values=[str(h) for h in range(1, 13)],
                    wrap=True, width=3, font=("Helvetica", 10)).grid(row=0, column=5)

        tk.Label(self, text=":", bg=bg, font=("Helvetica", 11, "bold")).grid(row=0, column=6, padx=1)

        # Minute
        self._min_var = tk.StringVar(value=f"{(now.minute // 5) * 5:02d}")
        ttk.Spinbox(self, textvariable=self._min_var,
                    values=[f"{m:02d}" for m in range(0, 60, 5)],
                    wrap=True, width=3, font=("Helvetica", 10)).grid(row=0, column=7, padx=(0, 8))

        # AM/PM 
        self._ampm_var = tk.StringVar(value=now.strftime("%p"))
        for col, val in ((8, "AM"), (9, "PM")):
            tk.Radiobutton(self, text=val, variable=self._ampm_var, value=val,
                           font=("Helvetica", 10, "bold"), bg=bg, activebackground=bg,
                           selectcolor="#000000", indicatoron=False,
                           relief="groove", width=3, padx=2).grid(row=0, column=col, padx=(0, 1))

    # _on_month_year_change()
    # Event handler —> is used whenever month dropdown or year spinbox changes
    def _on_month_year_change(self, *_):
        self._update_days()

    # _update_days() 
    # Recalculates how many days are valid for the currently selected month and year
    # If the currently selected day is greater than the new max, it caps it to the max
    def _update_days(self):
        try:
            month_idx    = self.MONTHS.index(self._month_var.get()) + 1
            year         = int(self._year_var.get())
        except (ValueError, AttributeError):
            return

        days_in_month = calendar.monthrange(year, month_idx)[1]
        self._day_cb["values"] = [str(d) for d in range(1, days_in_month + 1)]

        try:
            current_day = int(self._day_var.get())
        except ValueError:
            current_day = 1
        self._day_var.set(str(min(current_day, days_in_month)))

    # get_datetime()
    # Reads all widget values and assembles a timezone-aware datetime object
    # Converts from 12-hour (AM/PM) to 24-hour internally before building datetime
    # Returns None and shows an error dialog if any field contains an invalid value
    def get_datetime(self) -> datetime | None:
        try:
            month  = self.MONTHS.index(self._month_var.get()) + 1
            day    = int(self._day_var.get())
            year   = int(self._year_var.get())
            hour   = int(self._hour_var.get())
            minute = int(self._min_var.get())
            ampm   = self._ampm_var.get()

            # 12-hour → 24-hour conversion
            if ampm == "AM" and hour == 12:
                hour = 0       # 12:xx AM = midnight = hour 0
            elif ampm == "PM" and hour != 12:
                hour += 12     # add 12 for all PM hours except 12:xx PM

            local_tz = datetime.now().astimezone().tzinfo
            return datetime(year, month, day, hour, minute, tzinfo=local_tz)

        except (ValueError, TypeError) as exc:
            messagebox.showerror("Invalid Date/Time", str(exc))
            return None

    # reset() 
    # Pre-fills all picker fields from a given datetime (defaults to right now)
    # Used to set the End picker 1 hour ahead of now when the form first loads
    def reset(self, dt: datetime | None = None):
        dt = dt or datetime.now()
        self._month_var.set(self.MONTHS[dt.month - 1])
        self._year_var.set(str(dt.year))
        self._update_days()
        self._day_var.set(str(dt.day))
        hour12 = dt.hour % 12 or 12  # convert 24-hour back to 12-hour for display
        self._hour_var.set(str(hour12))
        self._min_var.set(f"{(dt.minute // 5) * 5:02d}")
        self._ampm_var.set("AM" if dt.hour < 12 else "PM")


# CLASS 3 — EventsTab
# The full "Events" tab frame — holds the upcoming events list, the add-event
# form, and all the buttons. Talks to GoogleCalendarService for all data.
class EventsTab(tk.Frame):

    # __init__ 
    # Stores a reference to the calendar service, builds the UI, then kicks off
    # authentication on a background thread so the window appears immediately
    def __init__(self, parent, cal_service: GoogleCalendarService, bg: str = "#f0f4f7"):
        super().__init__(parent, bg=bg)
        self.cal     = cal_service
        self.BG      = bg
        self._events: list[dict] = []  # local cache of events loaded from Google

        self._build_ui()
        threading.Thread(target=self._authenticate, daemon=True).start()

    # _build_ui()
    # Lays out the entire Events tab in four sections:
    #   1. Status bar   — shows auth state, loading messages, and errors
    #   2. Events list  — scrollable Listbox of upcoming events; click to see details
    #   3. Add form     — title field, Start/End DateTimePickers, notes field
    #   4. Action buttons — Add Event, Refresh, Delete
    def _build_ui(self):
        # Status bar — dynamically updated text at the top of the tab
        self._status_var = tk.StringVar(value="Connecting to Google Calendar…")
        tk.Label(self, textvariable=self._status_var, font=("Helvetica", 10, "italic"),
                 bg=self.BG, fg="#007acc").pack(pady=(8, 0))

        # Scrollable listbox showing upcoming events fetched from Google Calendar
        list_frame = tk.Frame(self, bg=self.BG)
        list_frame.pack(pady=(6, 2), padx=10, fill="both", expand=True)
        self._listbox = tk.Listbox(list_frame, font=("Helvetica", 11),
                                   activestyle="none", width=55, height=7)
        self._listbox.pack(side="left", fill="both", expand=True)
        self._listbox.bind("<<ListboxSelect>>", self._on_select)  # show details on click
        sb = tk.Scrollbar(list_frame, command=self._listbox.yview)
        sb.pack(side="right", fill="y")
        self._listbox.config(yscrollcommand=sb.set)

        # Add Event form — grouped inside a LabelFrame for visual separation
        form = tk.LabelFrame(self, text=" Add Event ", font=("Helvetica", 10, "bold"),
                             bg=self.BG, fg="black", padx=8, pady=6)
        form.pack(padx=10, pady=(4, 4), fill="x")

        # Title row — plain text entry for the event name
        title_row = tk.Frame(form, bg=self.BG)
        title_row.pack(fill="x", pady=(0, 4))
        tk.Label(title_row, text="Title", font=("Helvetica", 10, "bold"),
                 bg=self.BG, width=5, anchor="w").pack(side="left")
        self._title_entry = tk.Entry(title_row, font=("Helvetica", 10), width=42)
        self._title_entry.pack(side="left", padx=(6, 0))

        # Start DateTimePicker — defaults to right now
        start_row = tk.Frame(form, bg=self.BG)
        start_row.pack(fill="x", pady=3)
        self._start_picker = DateTimePicker(start_row, label="Start", bg=self.BG)
        self._start_picker.pack(side="left")

        # End DateTimePicker — defaults to one hour from now
        end_row = tk.Frame(form, bg=self.BG)
        end_row.pack(fill="x", pady=3)
        self._end_picker = DateTimePicker(end_row, label="End", bg=self.BG)
        self._end_picker.reset(datetime.now() + timedelta(hours=1))
        self._end_picker.pack(side="left")

        # Notes row — optional description/details for the event
        desc_row = tk.Frame(form, bg=self.BG)
        desc_row.pack(fill="x", pady=(4, 0))
        tk.Label(desc_row, text="Notes", font=("Helvetica", 10, "bold"),
                 bg=self.BG, width=5, anchor="w").pack(side="left")
        self._desc_entry = tk.Entry(desc_row, font=("Helvetica", 10), width=42)
        self._desc_entry.pack(side="left", padx=(6, 0))

        # Action buttons row — Add, Refresh, Delete
        btn_frame = tk.Frame(self, bg=self.BG)
        btn_frame.pack(pady=(4, 8))
        btn_cfg = dict(font=("Helvetica", 11, "bold"), fg="black", padx=10)
        tk.Button(btn_frame, text="➕ Add Event", bg="#2cb766", activebackground="#27ae60",
                  command=self._add_event, **btn_cfg).pack(side="left", padx=5)
        tk.Button(btn_frame, text="🔄 Refresh", bg="#2980b9", activebackground="#1a6a9a",
                  command=self._refresh, **btn_cfg).pack(side="left", padx=5)
        tk.Button(btn_frame, text="🗑 Delete", bg="#c0392b", activebackground="#a93226",
                  command=self._delete_event, **btn_cfg).pack(side="left", padx=5)

    # _authenticate() 
    # Runs on a background thread — calls cal.authenticate() and updates the
    # status bar with the result. If successful, immediately loads events.
    # Catches any OAuth or network error and displays it without crashing.
    def _authenticate(self):
        try:
            success = self.cal.authenticate()
        except Exception as exc:
            self._set_status(f"⚠  Auth error: {exc}")
            return

        if success:
            self._set_status("Authenticated ✓  —  loading events…")
            self._load_events()
        else:
            self._set_status("⚠  credentials.json not found — place it next to this file and restart.")

    # _load_events() 
    # Fetches events from the API, stores them in self._events, then schedules
    # a listbox refresh on the main thread via self.after(0, ...)
    # self.after(0) is important — you can't update tkinter widgets from a background thread
    def _load_events(self):
        try:
            self._events = self.cal.get_events()
            self.after(0, self._refresh_listbox)
            self._set_status(f"Loaded {len(self._events)} upcoming event(s).")
        except Exception as exc:
            self._set_status(f"Error loading events: {exc}")

    #  _refresh() 
    # Called by the Refresh button — spawns a background thread to re-fetch events
    def _refresh(self):
        self._set_status("Refreshing…")
        threading.Thread(target=self._load_events, daemon=True).start()

    #  _add_event()
    # Called by the Add Event button:
    #   1. Guards against trying to write before auth is complete
    #   2. Validates that a title was entered
    #   3. Reads start/end datetimes from the pickers (returns None if invalid)
    #   4. Checks that end is after start
    #   5. Runs the actual API call on a background thread (_do_add)
    #   6. On success: appends to local cache, refreshes listbox, clears the form
    def _add_event(self):
        if not self.cal.is_authenticated:
            messagebox.showerror("Not Connected",
                "Google Calendar is not authenticated yet."
                "Check that credentials.json is in the same folder as this file, "
                "then restart the app and complete the browser sign-in.")
            return

        title       = self._title_entry.get().strip()
        description = self._desc_entry.get().strip()

        if not title:
            messagebox.showwarning("Missing Title", "Please enter an event title.")
            return

        start_dt = self._start_picker.get_datetime()
        end_dt   = self._end_picker.get_datetime()

        if start_dt is None or end_dt is None:
            return  # DateTimePicker already showed the error dialog

        if end_dt <= start_dt:
            messagebox.showerror("Invalid Range", "End must be after Start.")
            return

        def _do_add():
            try:
                event = self.cal.add_event(title, start_dt, end_dt, description)
                self._events.append(event)
                self.after(0, self._refresh_listbox)
                self._set_status(f"Event '{title}' added to Google Calendar ✓")
                self.after(0, lambda: self._title_entry.delete(0, tk.END))
                self.after(0, lambda: self._desc_entry.delete(0, tk.END))
            except Exception as exc:
                self._set_status(f"Failed to add event: {exc}")
                self.after(0, lambda e=exc: messagebox.showerror("Add Failed", str(e)))

        threading.Thread(target=_do_add, daemon=True).start()

    #  _delete_event() 
    # Gets the selected event index, confirms delete
    # then runs the API delete call on a background thread.
    # If event is removed it removes the event from the local cache and refreshes the listbox.
    def _delete_event(self):
        selection = self._listbox.curselection()
        if not selection:
            messagebox.showinfo("No Selection", "Please select an event to delete.")
            return

        idx   = selection[0]
        event = self._events[idx]

        if not messagebox.askyesno("Delete Event",
                f"Permanently delete '{event['summary']}' from Google Calendar?"):
            return

        def _do_delete():
            try:
                self.cal.delete_event(event["id"])
                self._events.pop(idx)
                self.after(0, self._refresh_listbox)
                self._set_status(f"Deleted '{event['summary']}' ✓")
            except Exception as exc:
                self._set_status(f"Failed to delete: {exc}")

        threading.Thread(target=_do_delete, daemon=True).start()

    # _refresh_listbox() 
    # Clears the listbox and re-renders every event in self._events
    # Formats the start time as "Mon DD  HH:MM AM/PM" for display
    # Falls back to showing just the date portion if the datetime can't be parsed
    def _refresh_listbox(self):
        self._listbox.delete(0, tk.END)
        for event in self._events:
            try:
                dt            = datetime.fromisoformat(event["start"])
                display_start = dt.strftime("%b %d  %I:%M %p")
            except ValueError:
                display_start = event["start"][:10]  # fallback: just show the date
            self._listbox.insert(tk.END, f"🗓️  {display_start}  —  {event['summary']}")

    # _on_select() 
    # Used when the user clicks an event in the listbox
    # Shows the event's description (truncated to 80 chars) in the status bar
    def _on_select(self, _event):
        selection = self._listbox.curselection()
        if not selection:
            return
        ev   = self._events[selection[0]]
        desc = ev["description"] or "(no description)"
        self._set_status(f"{ev['summary']}: {desc[:80]}")

    # _set_status() 
    # Thread-safe status bar updater — always schedules via self.after(0)
    # so it can safely be called from background threads without tkinter crashes
    def _set_status(self, msg: str):
        self.after(0, lambda: self._status_var.set(msg))


# 
# CLASS 4 — ToDoist (main app / root window)
# Creates the main window, sets up a ttk.Notebook with two tabs:
#   Tab 1 — To-Do list (local, in-memory tasks)
#   Tab 2 — Events (Google Calendar integration via EventsTab)
#
class ToDoist:

    #  __init__ 
    # Configures window 
    # Builds the Notebook and adds both tabs
    # tasks[] is the in-memory list that backs the To-Do tab
    def __init__(self, root):
        self.root = root
        self.root.title("To-Doist")
        self.root.geometry("600x620")
        self.root.resizable(False, False)  # fixed size — resizing would break the layout
        self.root.configure(bg="#f0f4f7")

        self.tasks = []  # stores {"task": str, "done": bool} dicts for the To-Do tab

        # Style the Notebook tabs
        style = ttk.Style()
        style.configure("TNotebook", background="#f0f4f7", borderwidth=0)
        style.configure("TNotebook.Tab", font=("Helvetica", 11, "bold"), padding=[12, 4])

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)

        # Tab 1 — To-Do (plain tkinter frame, no external dependencies)
        self.todo_frame = tk.Frame(self.notebook, bg="#f0f4f7")
        self.notebook.add(self.todo_frame, text="  ✅ To-Do  ")
        self._setup_todo_ui(self.todo_frame)

        # Tab 2 — Events (requires credentials.json next to the script)
        cal_service = GoogleCalendarService(credentials_path="credentials.json")
        self.events_tab = EventsTab(self.notebook, cal_service)
        self.notebook.add(self.events_tab, text="  🗓️ Events  ")

    # _setup_todo_ui() 
    # Builds the To-Do tab layout:
    #   - Title label at the top
    #   - Text entry + "Add Task" button
    #   - Scrollable task listbox
    #   - Info label (shows feedback messages like "Task added!")
    #   - Mark as Done / Delete Task / Clear All buttons
    def _setup_todo_ui(self, parent):
        tk.Label(parent, text="To-Doist", font=("Helvetica", 22, "bold"),
                 bg="#f0f4f7", fg="#333").pack(pady=10)

        input_frame = tk.Frame(parent, bg="#f0f4f7")
        input_frame.pack(pady=10)
        self.task_entry = tk.Entry(input_frame, font=("Helvetica", 12), width=30)
        self.task_entry.pack(side="left", padx=(0, 10))
        tk.Button(input_frame, text="Add Task", font=("Helvetica", 11, "bold"),
                  bg="#2cb766", fg="Black", activebackground="#27ae60", activeforeground="white",
                  padx=10, command=self.add_task).pack(side="left")

        list_frame = tk.Frame(parent, bg="#f0f4f7")
        list_frame.pack(pady=10, fill="both", expand=True)
        self.task_listbox = tk.Listbox(list_frame, font=("Helvetica", 12),
                                       width=45, height=10, activestyle="none")
        self.task_listbox.pack(side="left", fill="both", expand=True)
        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")
        self.task_listbox.config(yscrollcommand=scrollbar.set)
        scrollbar.config(command=self.task_listbox.yview)

        # Info label — gives the user text feedback after actions (add, delete, etc.)
        self.info_label = tk.Label(parent, text="", font=("Helvetica", 11),
                                   bg="#f0f4f7", fg="#007acc")
        self.info_label.pack(pady=5)

        button_frame = tk.Frame(parent, bg="#f0f4f7")
        button_frame.pack(pady=10)
        btn_cfg = dict(font=("Helvetica", 11, "bold"), fg="black", padx=10)
        tk.Button(button_frame, text="Mark as Done", bg="#2980b9",
                  activebackground="#2980b9", activeforeground="white",
                  command=self.mark_done, **btn_cfg).pack(side="left", padx=5)
        tk.Button(button_frame, text="Delete Task", bg="#c0392b",
                  activebackground="#c0392b", activeforeground="white",
                  command=self.delete_task, **btn_cfg).pack(side="left", padx=5)
        tk.Button(button_frame, text="Clear All", bg="#7f8c8d",
                  activebackground="#7f8c8d", activeforeground="white",
                  command=self.clear_all, **btn_cfg).pack(side="left", padx=5)

    # refresh_listbox() 
    # Clears and re-renders the To-Do listbox from self.tasks
    def refresh_listbox(self):
        self.task_listbox.delete(0, tk.END)
        for i, task in enumerate(self.tasks, start=1):
            status = "✅" if task["done"] else "❌"
            self.task_listbox.insert(tk.END, f"{i}. {task['task']} [{status}]")

    # add_task() 
    # Reads the text entry, makes sure it's not empty, appends a new task dict,
    # clears user input box, updates the info label, and refreshes the listbox
    def add_task(self):
        task_text = self.task_entry.get().strip()
        if not task_text:
            self.info_label.config(text="Please enter a task first")
            return
        self.tasks.append({"task": task_text, "done": False})
        self.task_entry.delete(0, tk.END)
        self.info_label.config(text=f"Task '{task_text}' added!")
        self.refresh_listbox()

    # get_selected_index() 
    # Shared helper — returns the index of the selected listbox item
    # Shows an info dialog and returns None if nothing is selected
    # Used by mark_done() and delete_task() to avoid duplicating selection logic
    def get_selected_index(self):
        selection = self.task_listbox.curselection()
        if not selection:
            messagebox.showinfo("No selection", "Please select a task first")
            return None
        return selection[0]

    # mark_done() 
    # Sets the selected task's "done" to True and refreshes the display
    def mark_done(self):
        index = self.get_selected_index()
        if index is None:
            return
        self.tasks[index]["done"] = True
        self.info_label.config(text="Task marked as done!")
        self.refresh_listbox()

    # delete_task() 
    # Removes the selected task from self.tasks and refreshes the listbox
    def delete_task(self):
        index = self.get_selected_index()
        if index is None:
            return
        removed = self.tasks.pop(index)
        self.info_label.config(text=f"Deleted Task: {removed['task']}")
        self.refresh_listbox()

    # clear_all()
    # Wipes all tasks after a confirmation dialog
    # Skips the dialog and shows a message if the list is already empty
    def clear_all(self):
        if not self.tasks:
            self.info_label.config(text="No tasks to clear")
            return
        if messagebox.askyesno("Clear All", "Are you sure you want to delete ALL TASKS?"):
            self.tasks.clear()
            self.refresh_listbox()
            self.info_label.config(text="All tasks cleared!")


# ENTRY POINT
# Creates the root tkinter window, hands it to ToDoist, and starts the event loop.
# mainloop() blocks here until the window is closed.
if __name__ == "__main__":
    root = tk.Tk()
    app = ToDoist(root)
    root.mainloop()