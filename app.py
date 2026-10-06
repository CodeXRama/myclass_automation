"""Portable GUI scheduler for live classes on myclass.lpu.in."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, ttk
from urllib.parse import urljoin

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import sync_playwright


APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
LOGIN_URL = "https://myclass.lpu.in"
TIMETABLE_URL_PART = "/secure/tla/m.jsp"
CREDENTIALS_FILE = APP_DIR / "credentials.json"


def chrome_path() -> str:
    locations = (
        Path(os.environ.get("PROGRAMFILES", ""))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google/Chrome/Application/chrome.exe",
    )
    for path in locations:
        if path.is_file():
            return str(path)
    raise FileNotFoundError("Google Chrome was not found on this laptop.")


def read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        path.write_text(json.dumps(default, indent=2), encoding="utf-8")
        return default
    with path.open(encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object.")
    return value


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def load_credentials() -> tuple[str, str]:
    data = read_json(CREDENTIALS_FILE, {"username": "", "password": ""})
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    if not username or not password:
        raise ValueError("Enter and save your username and password first.")
    return username, password


def event_times(event) -> tuple[datetime, datetime] | None:
    """Read the start and end times rendered by the website."""
    label = event.locator(".fc-time").get_attribute("data-full") or event.inner_text()
    matches = re.findall(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", label, re.IGNORECASE)
    if len(matches) < 2:
        return None

    def convert(value: tuple[str, str, str]) -> datetime:
        hour, minute, meridiem = int(value[0]), int(value[1]), value[2].upper()
        if meridiem == "PM" and hour != 12:
            hour += 12
        if meridiem == "AM" and hour == 12:
            hour = 0
        return datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)

    start, end = convert(matches[0]), convert(matches[1])
    if end <= start:
        end += timedelta(days=1)
    return start, end


def login(page: Page, username: str, password: str) -> None:
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    page.locator('input[name="i"], input[placeholder="Username"]').first.fill(username)
    page.locator('input[name="p"], input[placeholder="Password"]').first.fill(password)
    page.get_by_role("button", name="Login").click()
    page.wait_for_load_state("domcontentloaded")


def open_timetable(page: Page) -> None:
    page.wait_for_timeout(2500)
    if TIMETABLE_URL_PART not in page.url:
        page.get_by_text("View Classes/Meetings", exact=True).click()
        page.wait_for_url(f"**{TIMETABLE_URL_PART}**")
    page.wait_for_timeout(1500)


def read_website_schedule(page: Page) -> list[dict]:
    """Read today's timetable once from the website; no local schedule is used."""
    open_timetable(page)
    schedule = []
    events = page.locator(".fc-event")
    for index in range(events.count()):
        event = events.nth(index)
        times = event_times(event)
        href = event.get_attribute("href")
        if times is not None and href:
            schedule.append(
                {
                    "start": times[0],
                    "end": times[1],
                    "href": urljoin(page.url, href),
                    "title": event.get_attribute("title") or event.inner_text().strip(),
                }
            )
    return sorted(schedule, key=lambda item: item["start"])


def join_class(context: BrowserContext, entry: dict) -> tuple[Page, datetime]:
    meeting = context.new_page()
    meeting.goto(entry["href"], wait_until="domcontentloaded")
    join = meeting.locator(
        '#join, #joinClass, #joinMeeting, '
        '[id*="join" i], [class*="join" i], '
        'a:has-text("Join"), button:has-text("Join"), '
        'input[value*="Join" i]'
    ).first
    join.wait_for(state="visible", timeout=90_000)
    join.click()
    meeting.wait_for_timeout(3000)
    return meeting, entry["end"]


def return_to_classes(page: Page, username: str, password: str) -> None:
    """Return to the timetable, recovering the login session if necessary."""
    if page.is_closed():
        raise RuntimeError("The timetable tab was closed.")
    if page.locator('input[name="i"], input[placeholder="Username"]').count():
        login(page, username, password)
    open_timetable(page)


def join_with_recovery(
    context: BrowserContext,
    page: Page,
    entry: dict,
    username: str,
    password: str,
) -> tuple[Page, datetime]:
    """Retry a join once after returning through the classes page."""
    try:
        return join_class(context, entry)
    except Exception as first_error:
        page_to_close = context.pages[-1] if len(context.pages) > 1 else None
        if page_to_close is not None and not page_to_close.is_closed():
            page_to_close.close()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        login(page, username, password)
        open_timetable(page)
        try:
            return join_class(context, entry)
        except Exception as second_error:
            raise RuntimeError(
                f"Could not join the class after retry: {second_error}"
            ) from first_error


def leave_class(meeting: Page) -> None:
    """Leave the CodeTantra meeting before closing its tab."""
    leave = meeting.get_by_role("button", name=re.compile(r"^\s*Leave\s*$", re.IGNORECASE))
    if leave.count() == 0:
        leave = meeting.get_by_text(
            re.compile(r"^\s*Leave\s*$", re.IGNORECASE)
        )
    if leave.count() == 0:
        raise RuntimeError(
            "The meeting opened, but its Leave control could not be found."
        )
    leave.first.click()
    meeting.wait_for_timeout(1000)


def close_browser(context: BrowserContext, browser) -> None:
    try:
        context.close()
    except Exception as error:
        if "connection closed" not in str(error).lower():
            raise
    try:
        if browser.is_connected():
            browser.close()
    except Exception as error:
        if "connection closed" not in str(error).lower():
            raise


class Scheduler(threading.Thread):
    def __init__(self, log, stopped: threading.Event):
        super().__init__(daemon=True)
        self.log = log
        self.stopped = stopped

    def wait_until(self, target: datetime) -> bool:
        while not self.stopped.is_set():
            remaining = (target - datetime.now()).total_seconds()
            if remaining <= 0:
                return True
            self.log(
                f"Next class at {target:%I:%M %p}; sleeping "
                f"{int(remaining // 60)} minutes."
            )
            self.stopped.wait(min(remaining, 60))
        return False

    def run(self) -> None:
        try:
            username, password = load_credentials()
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=chrome_path(),
                    headless=False,
                    args=["--start-maximized"],
                )
                context = browser.new_context(no_viewport=True)
                page = context.new_page()
                try:
                    login(page, username, password)
                    self.log("Logged in successfully.")
                    while not self.stopped.is_set():
                        schedule = read_website_schedule(page)
                        now = datetime.now()
                        upcoming = [item for item in schedule if item["end"] > now]
                        self.log(
                            f"Read {len(schedule)} classes from today's website timetable."
                        )
                        for entry in upcoming:
                            if not self.wait_until(entry["start"]):
                                return
                            self.log(f"Opening: {entry['title']}")
                            meeting, end = join_with_recovery(
                                context, page, entry, username, password
                            )
                            self.log("Join action completed.")
                            self.wait_until(end)
                            if not meeting.is_closed():
                                self.log("Class time ended; clicking Leave.")
                                leave_class(meeting)
                                meeting.close()
                            self.log("Class ended.")
                            return_to_classes(page, username, password)
                            self.log("Returned to View Classes/Meetings.")
                        tomorrow = (datetime.now() + timedelta(days=1)).replace(
                            hour=0, minute=1, second=0, microsecond=0
                        )
                        self.log("No more classes today; sleeping until 12:01 AM.")
                        if not self.wait_until(tomorrow):
                            return
                        page.reload(wait_until="domcontentloaded")
                        if page.locator(
                            'input[name="i"], input[placeholder="Username"]'
                        ).count():
                            self.log("Session expired; logging in again.")
                            login(page, username, password)
                finally:
                    close_browser(context, browser)
        except Exception as error:
            self.log(f"ERROR: {error}")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LPU Class Scheduler")
        self.geometry("760x470")
        self.minsize(620, 400)
        self.stop_event = threading.Event()
        self.worker: Scheduler | None = None
        self.build()
        self.load_credentials()

    def build(self) -> None:
        credentials = ttk.LabelFrame(self, text="My Class credentials")
        credentials.pack(fill="x", padx=12, pady=12)
        ttk.Label(credentials, text="Username").grid(row=0, column=0, padx=8, pady=8)
        self.username = ttk.Entry(credentials, width=42)
        self.username.grid(row=0, column=1, padx=8, pady=8)
        ttk.Label(credentials, text="Password").grid(row=1, column=0, padx=8, pady=8)
        self.password = ttk.Entry(credentials, width=42, show="*")
        self.password.grid(row=1, column=1, padx=8, pady=8)
        ttk.Button(
            credentials, text="Save credentials", command=self.save_credentials
        ).grid(row=0, column=2, rowspan=2, padx=8)

        info = ttk.Label(
            self,
            text="The schedule is read automatically from the website. "
            "No timetable JSON is required.",
            wraplength=700,
        )
        info.pack(anchor="w", padx=12, pady=(0, 8))

        self.status = ttk.Label(self, text="Stopped", foreground="darkred")
        self.status.pack(anchor="w", padx=12)
        self.log_box = tk.Text(self, height=15, state="disabled")
        self.log_box.pack(fill="both", expand=True, padx=12, pady=8)
        buttons = ttk.Frame(self)
        buttons.pack(pady=8)
        ttk.Button(buttons, text="Start scheduler", command=self.start).pack(
            side="left", padx=6
        )
        ttk.Button(buttons, text="Stop scheduler", command=self.stop).pack(
            side="left", padx=6
        )
        self.protocol("WM_DELETE_WINDOW", self.close)

    def load_credentials(self) -> None:
        data = read_json(CREDENTIALS_FILE, {"username": "", "password": ""})
        self.username.insert(0, data.get("username", ""))
        self.password.insert(0, data.get("password", ""))

    def save_credentials(self) -> None:
        write_json(
            CREDENTIALS_FILE,
            {"username": self.username.get().strip(), "password": self.password.get()},
        )
        self.log("Credentials saved locally.")

    def log(self, message: str) -> None:
        self.after(0, self._log, message)

    def _log(self, message: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{datetime.now():%H:%M:%S}  {message}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            self.log("Scheduler is already running.")
            return
        try:
            self.save_credentials()
            load_credentials()
        except (ValueError, json.JSONDecodeError) as error:
            messagebox.showerror("Cannot start", str(error))
            return
        self.stop_event.clear()
        self.worker = Scheduler(self.log, self.stop_event)
        self.worker.start()
        self.status.configure(text="Running", foreground="darkgreen")
        self.log("Scheduler started.")

    def stop(self) -> None:
        self.stop_event.set()
        self.status.configure(text="Stopped", foreground="darkred")
        self.log("Stop requested.")

    def close(self) -> None:
        self.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
