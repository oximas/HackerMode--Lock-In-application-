"""
core.py — HackerMode backend
Keyboard hook, startup registry, Anki DB queries, Azkar/streak
persistence, and background watcher threads.
No Qt widgets — only QThread and pyqtSignal live here.
"""

import os
import sys
import json
import ctypes
import winreg
import threading
from ctypes import wintypes
from datetime import datetime, timedelta

from PyQt6.QtCore import QThread, pyqtSignal

# ─────────────────────────────────────────────
#  CONFIG  (edit these)
# ─────────────────────────────────────────────
REQUIRED_REVIEWS = 10         # cards reviewed today to unlock
POLL_INTERVAL_MS = 5000       # how often to check Anki (ms)
APP_NAME         = "HackerMode"
ANKI_PATH        = r"C:\Users\Mega Store\AppData\Local\Programs\Anki\anki.exe"
PLAY_SOUND       = True       # set False to disable victory sound

# Azkar task config
AZKAR_SOURCE_PATH = r"C:\Users\Mega Store\Desktop\اذكار الصباح.jpg"
AZKAR_LOCAL_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "azkar.jpg")

# Persistence
STREAK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "streak_data.json")


# ─────────────────────────────────────────────
#  WINDOWS LOW-LEVEL KEYBOARD HOOK
# ─────────────────────────────────────────────
WH_KEYBOARD_LL = 13
WM_KEYDOWN     = 0x0100
WM_SYSKEYDOWN  = 0x0104
VK_LWIN        = 0x5B
VK_RWIN        = 0x5C
VK_TAB         = 0x09

_hook_id = None

def _low_level_handler(nCode, wParam, lParam):
    if nCode >= 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
        vk = ctypes.cast(lParam, ctypes.POINTER(ctypes.c_ulong))[0]
        if vk in (VK_LWIN, VK_RWIN):
            return 1
        if wParam == WM_SYSKEYDOWN and vk == VK_TAB:
            return 1
    return ctypes.windll.user32.CallNextHookEx(_hook_id, nCode, wParam, lParam)

HOOKPROC = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
_callback_ref = HOOKPROC(_low_level_handler)   # keep alive

def install_hook():
    global _hook_id
    _hook_id = ctypes.windll.user32.SetWindowsHookExW(
        WH_KEYBOARD_LL, _callback_ref,
        ctypes.windll.kernel32.GetModuleHandleW(None), 0
    )

def remove_hook():
    if _hook_id:
        ctypes.windll.user32.UnhookWindowsHookEx(_hook_id)

def pump_messages():
    """Run message loop in background thread to keep hook alive."""
    msg = wintypes.MSG()
    while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
        ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))


# ─────────────────────────────────────────────
#  STARTUP REGISTRATION
# ─────────────────────────────────────────────
def register_startup():
    exe    = sys.executable
    script = os.path.abspath(__file__)
    value  = f'"{exe}" "{script}"'
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE
        )
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, value)
        winreg.CloseKey(key)
    except Exception as e:
        print(f"[startup] registry write failed: {e}")

def unregister_startup():
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE
        )
        winreg.DeleteValue(key, APP_NAME)
        winreg.CloseKey(key)
    except Exception:
        pass


# ─────────────────────────────────────────────
#  ANKI DB
# ─────────────────────────────────────────────
def get_anki_db_path() -> str | None:
    """Find Anki's collection SQLite file automatically."""
    base = os.path.join(os.environ.get("APPDATA", ""), "Anki2")
    if not os.path.isdir(base):
        return None
    for entry in os.listdir(base):
        candidate = os.path.join(base, entry, "collection.anki2")
        if os.path.isfile(candidate):
            return candidate
    return None

def get_reviews_today(db_path: str) -> int:
    """
    Count cards reviewed since Anki's day cutoff (4 AM).
    Copies all 3 SQLite WAL files into a temp dir, then forces a WAL
    checkpoint on the copy so that reviews written to the WAL (but not
    yet flushed to the main DB file) are visible to our query.
    """
    import sqlite3, shutil, tempfile
    if not db_path or not os.path.isfile(db_path):
        return 0
    try:
        now       = datetime.now()
        day_start = now.replace(hour=4, minute=0, second=0, microsecond=0)
        if now < day_start:
            day_start -= timedelta(days=1)
        day_start_ms = int(day_start.timestamp() * 1000)

        # Copy .anki2 + .anki2-wal + .anki2-shm into a temp dir together.
        # All three must be copied as a unit so the snapshot is consistent.
        tmp_dir = tempfile.mkdtemp()
        tmp_db  = os.path.join(tmp_dir, "col.anki2")
        shutil.copy2(db_path, tmp_db)
        for ext in ("-wal", "-shm"):
            src = db_path + ext
            if os.path.isfile(src):
                shutil.copy2(src, tmp_db + ext)

        try:
            # Open read-write so PRAGMA wal_checkpoint is allowed.
            conn = sqlite3.connect(tmp_db, timeout=2)
            # Merge any pending WAL frames into the main DB copy.
            # Without this, reviews in the WAL since the last Anki sync
            # are invisible to the query.
            conn.execute("PRAGMA wal_checkpoint(FULL)")
            count = conn.execute(
                "SELECT COUNT(*) FROM revlog WHERE id >= ?", (day_start_ms,)
            ).fetchone()[0]
            conn.close()
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        print(f"[AnkiDB] Reviews today: {count}")
        return count
    except Exception as e:
        print(f"[AnkiDB] read error: {e}")
        return 0


# ─────────────────────────────────────────────
#  AZKAR FILE OPS
# ─────────────────────────────────────────────
def ensure_azkar_image() -> bool:
    """Copy Azkar image from source to local project folder if needed."""
    if not os.path.isfile(AZKAR_LOCAL_PATH):
        if os.path.isfile(AZKAR_SOURCE_PATH):
            import shutil
            shutil.copy2(AZKAR_SOURCE_PATH, AZKAR_LOCAL_PATH)
            print(f"[Azkar] Copied image to {AZKAR_LOCAL_PATH}")
        else:
            print(f"[Azkar] Source image not found: {AZKAR_SOURCE_PATH}")
    return os.path.isfile(AZKAR_LOCAL_PATH)


# ─────────────────────────────────────────────
#  STREAK & TASK PERSISTENCE
# ─────────────────────────────────────────────
def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def load_streak_data() -> dict:
    try:
        with open(STREAK_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"completed_days": [], "last_streak": 0}

def save_streak_data(data: dict):
    try:
        with open(STREAK_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[Streak] save error: {e}")

def azkar_done_today() -> bool:
    """Return True if Azkar was already completed today."""
    return load_streak_data().get("azkar_last_done") == _today_str()

def mark_azkar_done():
    """Persist today's Azkar completion."""
    data = load_streak_data()
    data["azkar_last_done"] = _today_str()
    save_streak_data(data)
    print(f"[Azkar] marked done for {_today_str()}")

def unmark_azkar_done():
    """Clear today's Azkar completion."""
    data = load_streak_data()
    if data.get("azkar_last_done") == _today_str():
        data.pop("azkar_last_done")
        save_streak_data(data)
        print(f"[Azkar] unmarked for {_today_str()}")

def mark_today_complete():
    """Mark today as a completed day and return current streak count."""
    data  = load_streak_data()
    today = _today_str()
    if today not in data["completed_days"]:
        data["completed_days"].append(today)
        save_streak_data(data)
    return calculate_streak(data)

def calculate_streak(data: dict = None) -> int:
    """Count consecutive completed days ending today or yesterday."""
    if data is None:
        data = load_streak_data()
    completed = set(data.get("completed_days", []))
    streak    = 0
    day       = datetime.now().date()
    while day.strftime("%Y-%m-%d") in completed:
        streak += 1
        day -= timedelta(days=1)
    return streak


# ─────────────────────────────────────────────
#  AZKAR WINDOW WATCHER
# ─────────────────────────────────────────────
class AzkarWindowWatcher(QThread):
    task_complete = pyqtSignal()
    window_found  = pyqtSignal(int)   # emits HWND so UI can raise it

    VIEWER_EXES = {"photos.exe", "imagepreview.exe", "dllhost.exe",
                   "microsoft.photos.exe", "wwahost.exe"}

    def __init__(self, image_path: str):
        super().__init__()
        self._image_path = os.path.basename(image_path).lower()

    def _enum_viewer_hwnds(self) -> list:
        """Return all visible HWNDs belonging to known image viewer processes."""
        import psutil
        found = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _):
            if not ctypes.windll.user32.IsWindowVisible(hwnd):
                return True
            pid = ctypes.c_ulong(0)
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                name = psutil.Process(pid.value).name().lower()
                if name in self.VIEWER_EXES:
                    found.append(hwnd)
            except Exception:
                pass
            return True

        ctypes.windll.user32.EnumWindows(WNDENUMPROC(cb), 0)
        return found

    def _hwnd_alive(self, hwnd: int) -> bool:
        return bool(ctypes.windll.user32.IsWindow(hwnd))

    def run(self):
        import time

        before = set(self._enum_viewer_hwnds())

        target_hwnd = None
        for _ in range(30):
            time.sleep(0.2)
            after = set(self._enum_viewer_hwnds())
            new   = after - before
            if new:
                target_hwnd = next(iter(new))
                break

        if target_hwnd is None:
            print("[AzkarWatcher] no viewer window found — marking done after 3s")
            time.sleep(3)
            self.task_complete.emit()
            return

        print(f"[AzkarWatcher] watching HWND {target_hwnd}")
        self.window_found.emit(target_hwnd)

        while self._hwnd_alive(target_hwnd):
            time.sleep(0.4)

        print("[AzkarWatcher] viewer closed → task complete")
        self.task_complete.emit()


# ─────────────────────────────────────────────
#  ANKI WATCHER
# ─────────────────────────────────────────────
class AnkiWatcher(QThread):
    progress_update = pyqtSignal(int, int)
    task_complete   = pyqtSignal()
    already_done    = pyqtSignal(int)   # fired if goal already met on first poll

    def __init__(self):
        super().__init__()
        self._running = True
        self._db_path = get_anki_db_path()
        print(f"[AnkiDB] Using database: {self._db_path}")

    def run(self):
        first_poll = True
        while self._running:
            reviewed = get_reviews_today(self._db_path)
            self.progress_update.emit(reviewed, REQUIRED_REVIEWS)
            if reviewed >= REQUIRED_REVIEWS:
                if first_poll:
                    self.already_done.emit(reviewed)
                else:
                    self.task_complete.emit()
                break
            first_poll = False
            for _ in range(POLL_INTERVAL_MS // 200):
                if not self._running:
                    return
                self.msleep(200)

    def stop(self):
        self._running = False
