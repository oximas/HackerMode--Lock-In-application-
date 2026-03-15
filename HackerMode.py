"""
HackerMode - Productivity Lock Screen
Requires: pip install PyQt6 pywin32 requests
Run on Windows only.
"""

import sys
import os
import json
import subprocess
import threading
import winreg
import ctypes
from ctypes import wintypes
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QPushButton, QLabel, QProgressBar, QFrame
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QPropertyAnimation,
    QEasingCurve, QSize
)
from PyQt6.QtGui import (
    QFont, QColor, QPalette, QFontDatabase, QIcon,
    QPainter, QLinearGradient, QBrush, QPen
)

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
        # Block Win keys
        if vk in (VK_LWIN, VK_RWIN):
            return 1
        # Block Alt+Tab (Alt is active = wParam SYSKEYDOWN, key is Tab)
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
    exe = sys.executable
    script = os.path.abspath(__file__)
    value = f'"{exe}" "{script}"'
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
#  ANKI CONNECT WATCHER (background thread)
# ─────────────────────────────────────────────
def get_anki_db_path():
    """Find Anki's collection SQLite file automatically."""
    base = os.path.join(os.environ.get("APPDATA", ""), "Anki2")
    if not os.path.isdir(base):
        return None
    # Find the first profile folder that has a collection.anki2
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
    from datetime import datetime, timedelta
    if not db_path or not os.path.isfile(db_path):
        return 0
    try:
        now = datetime.now()
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
            # This is the critical step: without it, reviews recorded
            # in the WAL since the last Anki sync are invisible.
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


def ensure_azkar_image():
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
#  STREAK TRACKING
# ─────────────────────────────────────────────
STREAK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "streak_data.json")

def _today_str():
    from datetime import datetime
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
    data = load_streak_data()
    return data.get("azkar_last_done") == _today_str()

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
    data = load_streak_data()
    today = _today_str()
    if today not in data["completed_days"]:
        data["completed_days"].append(today)
        save_streak_data(data)
    return calculate_streak(data)

def calculate_streak(data: dict = None) -> int:
    """Count consecutive completed days ending today or yesterday."""
    from datetime import datetime, timedelta
    if data is None:
        data = load_streak_data()
    completed = set(data.get("completed_days", []))
    streak = 0
    day = datetime.now().date()
    while day.strftime("%Y-%m-%d") in completed:
        streak += 1
        day -= timedelta(days=1)
    return streak


# ─────────────────────────────────────────────
#  AZKAR WATCHER — tracks viewer window by HWND, not PID
# ─────────────────────────────────────────────
class AzkarWindowWatcher(QThread):
    task_complete  = pyqtSignal()
    window_found   = pyqtSignal(int)   # emits HWND so UI can raise it

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

        # Snapshot HWNDs before open
        before = set(self._enum_viewer_hwnds())

        # Wait up to 6s for a new viewer HWND to appear
        target_hwnd = None
        for _ in range(30):
            time.sleep(0.2)
            after = set(self._enum_viewer_hwnds())
            new = after - before
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

        # Poll until that specific window is destroyed
        while self._hwnd_alive(target_hwnd):
            time.sleep(0.4)

        print("[AzkarWatcher] viewer closed → task complete")
        self.task_complete.emit()


# ─────────────────────────────────────────────
#  ANKI WATCHER (background thread)
# ─────────────────────────────────────────────
class AnkiWatcher(QThread):
    progress_update = pyqtSignal(int, int)
    task_complete   = pyqtSignal()
    already_done    = pyqtSignal(int)   # fired if goal met before app started

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

# ─────────────────────────────────────────────
#  TASK ROW WIDGET
# ─────────────────────────────────────────────
class TaskRow(QFrame):
    def __init__(self, icon: str, name: str, description: str):
        super().__init__()
        self.setObjectName("taskRow")
        self._done = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 14)
        layout.setSpacing(16)

        self.icon_label = QLabel(icon)
        self.icon_label.setFont(QFont("Segoe UI Emoji", 22))
        self.icon_label.setFixedWidth(40)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        self.name_label = QLabel(name)
        self.name_label.setFont(QFont("Consolas", 13, QFont.Weight.Bold))
        self.name_label.setObjectName("taskName")

        self.desc_label = QLabel(description)
        self.desc_label.setFont(QFont("Consolas", 9))
        self.desc_label.setObjectName("taskDesc")

        text_col.addWidget(self.name_label)
        text_col.addWidget(self.desc_label)

        self.status_label = QLabel("PENDING")
        self.status_label.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        self.status_label.setObjectName("statusPending")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.status_label.setFixedWidth(90)

        self.mini_bar = QProgressBar()
        self.mini_bar.setRange(0, REQUIRED_REVIEWS)
        self.mini_bar.setValue(0)
        self.mini_bar.setFixedHeight(6)
        self.mini_bar.setFixedWidth(120)
        self.mini_bar.setTextVisible(False)
        self.mini_bar.setObjectName("miniBar")

        right_col = QVBoxLayout()
        right_col.setSpacing(4)
        right_col.addWidget(self.status_label)
        right_col.addWidget(self.mini_bar, alignment=Qt.AlignmentFlag.AlignRight)

        layout.addWidget(self.icon_label)
        layout.addLayout(text_col, stretch=1)
        layout.addLayout(right_col)

    def update_progress(self, value: int, total: int):
        self.mini_bar.setMaximum(total)
        self.mini_bar.setValue(min(value, total))
        self.desc_label.setText(f"{value}/{total} cards reviewed today")

    def mark_done(self):
        self._done = True
        self.status_label.setText("✓  DONE")
        self.status_label.setObjectName("statusDone")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self.mini_bar.setValue(self.mini_bar.maximum())
        self.setObjectName("taskRowDone")
        self.style().unpolish(self)
        self.style().polish(self)

# ─────────────────────────────────────────────
#  MAIN LOCK SCREEN WINDOW
# ─────────────────────────────────────────────
class LockScreen(QMainWindow):
    def __init__(self):
        super().__init__()
        self._soft_unlocked = False
        self._hard_unlocked = False
        self._setup_window()
        self._build_ui()
        self._apply_stylesheet()
        self._start_watcher()

    # ── Window setup ──────────────────────────
    def _setup_window(self):
        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool              # hides from taskbar
        )
        self.showFullScreen()

    # ── UI Layout ─────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Top bar ──
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(40, 28, 40, 0)

        mode_badge = QLabel("● HACKER MODE ACTIVE")
        mode_badge.setFont(QFont("Consolas", 10))
        mode_badge.setObjectName("modeBadge")

        self.clock_label = QLabel()
        self.clock_label.setFont(QFont("Consolas", 10))
        self.clock_label.setObjectName("clockLabel")
        self._update_clock()
        clock_timer = QTimer(self)
        clock_timer.timeout.connect(self._update_clock)
        clock_timer.start(1000)

        top_bar.addWidget(mode_badge)
        top_bar.addStretch()
        top_bar.addWidget(self.clock_label)
        root.addLayout(top_bar)
        root.addSpacing(60)

        # ── Hero text ──
        hero_col = QVBoxLayout()
        hero_col.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self.title_label = QLabel("SYSTEM LOCKED")
        self.title_label.setFont(QFont("Consolas", 42, QFont.Weight.Bold))
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setObjectName("titleLabel")

        subtitle = QLabel("Complete your objectives to regain control of your machine.")
        subtitle.setFont(QFont("Consolas", 12))
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setObjectName("subtitleLabel")

        hero_col.addWidget(self.title_label)
        hero_col.addSpacing(8)
        hero_col.addWidget(subtitle)
        root.addLayout(hero_col)
        root.addSpacing(50)

        # ── Task card ──
        card_wrapper = QHBoxLayout()
        card_wrapper.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        card = QFrame()
        card.setObjectName("taskCard")
        card.setFixedWidth(680)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(0, 16, 0, 16)
        card_layout.setSpacing(0)

        header = QLabel("  TODAY'S OBJECTIVES")
        header.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        header.setObjectName("cardHeader")
        header.setContentsMargins(20, 4, 20, 12)
        card_layout.addWidget(header)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setObjectName("divider")
        card_layout.addWidget(divider)
        card_layout.addSpacing(8)

        self.anki_row = TaskRow("🗃", "Anki Reviews", f"0/{REQUIRED_REVIEWS} cards reviewed today")
        card_layout.addWidget(self.anki_row)

        divider2 = QFrame()
        divider2.setFrameShape(QFrame.Shape.HLine)
        divider2.setObjectName("divider")
        card_layout.addWidget(divider2)

        self.azkar_row = TaskRow("🌙", "Morning Azkar", "Open the image and read your Azkar")
        card_layout.addWidget(self.azkar_row)
        card_layout.addSpacing(8)

        # Overall progress bar
        bar_wrapper = QHBoxLayout()
        bar_wrapper.setContentsMargins(20, 8, 20, 4)
        progress_label = QLabel("OVERALL PROGRESS")
        progress_label.setFont(QFont("Consolas", 8))
        progress_label.setObjectName("barLabel")
        self.overall_bar = QProgressBar()
        self.overall_bar.setRange(0, 2)   # 2 tasks total
        self.overall_bar.setValue(0)
        self.overall_bar.setTextVisible(False)
        self.overall_bar.setFixedHeight(8)
        self.overall_bar.setObjectName("overallBar")
        bar_wrapper.addWidget(progress_label)
        bar_wrapper.addWidget(self.overall_bar, stretch=1)
        card_layout.addLayout(bar_wrapper)

        card_wrapper.addWidget(card)
        root.addLayout(card_wrapper)
        root.addSpacing(20)

        # ── Streak display ──
        streak_row = QHBoxLayout()
        streak_row.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.streak_label = QLabel()
        self.streak_label.setFont(QFont("Consolas", 10))
        self.streak_label.setObjectName("streakLabel")
        self.streak_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._refresh_streak_label()
        streak_row.addWidget(self.streak_label)
        root.addLayout(streak_row)
        root.addSpacing(28)

        # ── Buttons ──
        btn_row = QHBoxLayout()
        btn_row.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        btn_row.setSpacing(16)

        self.anki_btn = QPushButton("⚡  Open Anki")
        self.anki_btn.setObjectName("ankiBtn")
        self.anki_btn.setFont(QFont("Consolas", 12, QFont.Weight.Bold))
        self.anki_btn.setFixedSize(200, 50)
        self.anki_btn.clicked.connect(self._open_anki)

        self.azkar_btn = QPushButton("🌙  Open Azkar")
        self.azkar_btn.setObjectName("azkarBtn")
        self.azkar_btn.setFont(QFont("Consolas", 12, QFont.Weight.Bold))
        self.azkar_btn.setFixedSize(200, 50)
        self.azkar_btn.clicked.connect(self._open_azkar)

        unlock_btn = QPushButton("🔓  Emergency Unlock")
        unlock_btn.setObjectName("unlockBtn")
        unlock_btn.setFont(QFont("Consolas", 10))
        unlock_btn.setFixedSize(200, 50)
        unlock_btn.clicked.connect(self._emergency_unlock)

        btn_row.addWidget(self.anki_btn)
        btn_row.addWidget(self.azkar_btn)
        btn_row.addWidget(unlock_btn)
        root.addLayout(btn_row)
        root.addSpacing(20)

        # ── "Enter Your System" button — hidden until ALL tasks done ──
        enter_row = QHBoxLayout()
        enter_row.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.enter_btn = QPushButton("✦  Enter Your System")
        self.enter_btn.setObjectName("closeBtn")
        self.enter_btn.setFont(QFont("Consolas", 12, QFont.Weight.Bold))
        self.enter_btn.setFixedSize(260, 52)
        self.enter_btn.setVisible(False)
        self.enter_btn.clicked.connect(self._do_close)
        enter_row.addWidget(self.enter_btn)
        root.addLayout(enter_row)

        root.addStretch()

        # ── Footer ──
        footer = QLabel("HackerMode v0.1  —  Omar's focus system")
        footer.setFont(QFont("Consolas", 8))
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer.setObjectName("footer")
        root.addWidget(footer)
        root.addSpacing(16)

    # ── Watcher ───────────────────────────────
    def _start_watcher(self):
        self._tasks_done    = 0
        self._anki_done     = False
        self._azkar_done    = False
        self._soft_unlocked = False   # any task done → Alt+F4 works
        self._hard_unlocked = False   # all tasks done → Enter button shows

        self.watcher = AnkiWatcher()
        self.watcher.progress_update.connect(self._on_progress)
        self.watcher.task_complete.connect(self._on_anki_complete)
        self.watcher.already_done.connect(self._on_already_done)
        self.watcher.start()

        # If Azkar was already completed today, mark it immediately
        if azkar_done_today():
            print("[Azkar] already done today — pre-ticking")
            QTimer.singleShot(0, self._on_azkar_already_done)

    def _refresh_streak_label(self):
        streak = calculate_streak()
        if streak == 0:
            self.streak_label.setText("🔥  No streak yet — start today")
        elif streak == 1:
            self.streak_label.setText("🔥  1 day streak — keep it going")
        else:
            self.streak_label.setText(f"🔥  {streak} day streak")

    def _on_progress(self, reviewed: int, required: int):
        self.anki_row.update_progress(reviewed, required)

    def _on_already_done(self, reviewed: int):
        if self._anki_done:
            return
        self.anki_row.update_progress(reviewed, REQUIRED_REVIEWS)
        self.anki_row.mark_done()
        self._on_anki_complete()

    def _on_anki_complete(self):
        if self._anki_done:
            return
        self._anki_done = True
        self.anki_row.mark_done()
        if hasattr(self, '_enforce_timer'):
            self._enforce_timer.stop()
        self._tasks_done += 1
        self.overall_bar.setValue(self._tasks_done)
        self._check_all_complete()

    def _on_azkar_already_done(self):
        """Called on startup when Azkar was already completed today."""
        self._on_azkar_complete()

    def _on_azkar_complete(self):
        """Called when Azkar image viewer is closed."""
        if self._azkar_done:
            return
        self._azkar_done = True
        mark_azkar_done()   # persist so reopening the app keeps it ticked
        self.azkar_row.mark_done()
        # Show untick option briefly, then allow manual override
        self.azkar_btn.setText("✓  Azkar Done")
        self.azkar_btn.setObjectName("azkarBtnDone")
        self.azkar_btn.style().unpolish(self.azkar_btn)
        self.azkar_btn.style().polish(self.azkar_btn)
        self.azkar_btn.clicked.disconnect()
        self.azkar_btn.clicked.connect(self._untick_azkar)
        self._tasks_done += 1
        self.overall_bar.setValue(self._tasks_done)
        self._check_all_complete()

    def _untick_azkar(self):
        """Allow user to manually undo the auto-tick."""
        if not self._azkar_done:
            return
        self._azkar_done = False
        unmark_azkar_done()  # clear persistence so it's required again
        self._tasks_done = max(0, self._tasks_done - 1)
        self.overall_bar.setValue(self._tasks_done)
        # Reset the row visually
        self.azkar_row.status_label.setText("PENDING")
        self.azkar_row.status_label.setObjectName("statusPending")
        self.azkar_row.status_label.style().unpolish(self.azkar_row.status_label)
        self.azkar_row.status_label.style().polish(self.azkar_row.status_label)
        self.azkar_row._done = False
        self.azkar_row.setObjectName("taskRow")
        self.azkar_row.style().unpolish(self.azkar_row)
        self.azkar_row.style().polish(self.azkar_row)
        # Reset button
        self.azkar_btn.setText("🌙  Open Azkar")
        self.azkar_btn.setObjectName("azkarBtn")
        self.azkar_btn.style().unpolish(self.azkar_btn)
        self.azkar_btn.style().polish(self.azkar_btn)
        self.azkar_btn.clicked.disconnect()
        self.azkar_btn.clicked.connect(self._open_azkar)

    def _check_all_complete(self):
        if self._tasks_done >= 1 and not self._soft_unlocked:
            # Stage 1: ANY task done → soft unlock (Alt+F4 works, title changes)
            self._soft_unlocked = True
            mark_today_complete()
            self._refresh_streak_label()
            self.watcher.stop()
            remove_hook()
            self.title_label.setText("SYSTEM UNLOCKED")
            self.title_label.setObjectName("titleUnlocked")
            self.title_label.style().unpolish(self.title_label)
            self.title_label.style().polish(self.title_label)

        if self._tasks_done >= 2 and not self._hard_unlocked:
            # Stage 2: ALL tasks done → show Enter button + play sound
            self._hard_unlocked = True
            QTimer.singleShot(600, self._show_enter_button)

    def _show_enter_button(self):
        self.enter_btn.setVisible(True)
        if PLAY_SOUND:
            threading.Thread(target=self._play_victory_sound, daemon=True).start()

    def _play_victory_sound(self):
        import winsound
        notes = [(523, 120), (659, 120), (784, 200)]  # C5, E5, G5
        for freq, duration in notes:
            winsound.Beep(freq, duration)

    # ── Actions ───────────────────────────────
    def _open_azkar(self):
        path = AZKAR_LOCAL_PATH if os.path.isfile(AZKAR_LOCAL_PATH) else AZKAR_SOURCE_PATH
        if not os.path.isfile(path):
            self.azkar_row.desc_label.setText("⚠  Image not found")
            return
        try:
            os.startfile(path)
            self.azkar_row.desc_label.setText("Reading... close the image when done")
            self._azkar_watcher = AzkarWindowWatcher(path)
            self._azkar_watcher.task_complete.connect(self._on_azkar_complete)
            self._azkar_watcher.window_found.connect(self._raise_viewer_window)
            self._azkar_watcher.start()
        except Exception as e:
            print(f"[Azkar] open error: {e}")
            self.azkar_row.desc_label.setText(f"⚠  Could not open image: {e}")

    def _raise_viewer_window(self, hwnd: int):
        """Bring the image viewer above the lock screen."""
        try:
            our_hwnd = int(self.winId())
            SW_SHOW = 5
            # Restore if minimised
            ctypes.windll.user32.ShowWindow(hwnd, SW_SHOW)
            # Place viewer just above our window in Z-order
            SWP_NOMOVE     = 0x0002
            SWP_NOSIZE     = 0x0001
            SWP_NOACTIVATE = 0x0010
            # First put our window below the viewer
            ctypes.windll.user32.SetWindowPos(
                our_hwnd, hwnd, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
            )
            # Then force viewer to foreground
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            print(f"[Azkar] raised HWND {hwnd} to foreground")
        except Exception as e:
            print(f"[Azkar] raise error: {e}")

    def _open_anki(self):
        self._anki_proc = None
        try:
            self._anki_proc = subprocess.Popen([ANKI_PATH])
        except FileNotFoundError:
            try:
                self._anki_proc = subprocess.Popen(["anki"])
            except Exception:
                return

        # Start Z-order enforcement: Anki always above lock screen
        self._enforce_timer = QTimer(self)
        self._enforce_timer.timeout.connect(self._enforce_z_order)
        self._enforce_timer.start(500)


    def _find_anki_hwnd(self):
        """Find Anki's main window by matching its exact process PID."""
        if not self._anki_proc:
            return None
        target_pid = self._anki_proc.pid
        found = []
        GetWindowThreadProcessId = ctypes.windll.user32.GetWindowThreadProcessId

        def enum_cb(hwnd, _):
            if ctypes.windll.user32.IsWindowVisible(hwnd):
                pid = ctypes.c_ulong(0)
                GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == target_pid:
                    found.append(hwnd)
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        ctypes.windll.user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
        return found[0] if found else None

    def _enforce_z_order(self):
        """Place our window just below Anki — only when Anki isn't already on top."""
        if self._soft_unlocked:
            if hasattr(self, '_enforce_timer'):
                self._enforce_timer.stop()
            return

        try:
            anki_hwnd = self._find_anki_hwnd()
            if not anki_hwnd:
                return  # Anki minimized/closed — lock screen is all they see

            # Only act if the foreground window is Anki
            # (avoids constant repositioning that causes flicker)
            foreground = ctypes.windll.user32.GetForegroundWindow()
            if foreground != anki_hwnd:
                return

            SWP_NOMOVE     = 0x0002
            SWP_NOSIZE     = 0x0001
            SWP_NOACTIVATE = 0x0010

            our_hwnd = int(self.winId())
            ctypes.windll.user32.SetWindowPos(
                our_hwnd, anki_hwnd, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
            )
        except Exception as e:
            print(f"[Z-order] {e}")

    def _unlock(self):
        """Emergency unlock — force quit regardless of task state."""
        self._soft_unlocked = True
        self.watcher.stop()
        remove_hook()
        QApplication.quit()

    def _emergency_unlock(self):
        self._unlock()

    def _do_close(self):
        """Called by Enter Your System button."""
        QApplication.quit()

    def _update_clock(self):
        from datetime import datetime
        self.clock_label.setText(datetime.now().strftime("%H:%M:%S  |  %a %d %b"))

    # ── Prevent close / minimize ──────────────
    def closeEvent(self, event):
        if self._soft_unlocked:
            event.accept()
        else:
            event.ignore()

    def keyPressEvent(self, event):
        if not self._soft_unlocked:
            event.ignore()

    # ── Stylesheet ────────────────────────────
    def _apply_stylesheet(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #0a0c10;
            }

            #modeBadge {
                color: #00ff88;
                letter-spacing: 2px;
            }
            #clockLabel {
                color: #4a5568;
                letter-spacing: 1px;
            }
            #titleLabel {
                color: #e2e8f0;
                letter-spacing: 6px;
            }
            #titleUnlocked {
                color: #00ff88;
                letter-spacing: 6px;
            }
            #subtitleLabel {
                color: #4a5568;
                letter-spacing: 1px;
            }

            /* Task card */
            #taskCard {
                background-color: #111318;
                border: 1px solid #1e2330;
                border-radius: 12px;
            }
            #cardHeader {
                color: #2d3748;
                letter-spacing: 3px;
            }
            #divider {
                color: #1e2330;
                background-color: #1e2330;
                border: none;
                height: 1px;
                max-height: 1px;
            }

            /* Task rows */
            #taskRow {
                background-color: transparent;
                border-radius: 8px;
                margin: 0 8px;
            }
            #taskRow:hover {
                background-color: #151922;
            }
            #taskRowDone {
                background-color: #0d1a14;
                border-radius: 8px;
                margin: 0 8px;
            }
            #taskName  { color: #cbd5e0; }
            #taskDesc  { color: #2d3748; }

            #statusPending {
                color: #f6ad55;
                letter-spacing: 2px;
            }
            #statusDone {
                color: #00ff88;
                letter-spacing: 2px;
            }

            /* Mini bar */
            #miniBar {
                background-color: #1a1f2e;
                border-radius: 3px;
                border: none;
            }
            #miniBar::chunk {
                background-color: #f6ad55;
                border-radius: 3px;
            }

            /* Overall bar */
            #barLabel {
                color: #2d3748;
                letter-spacing: 2px;
                margin-right: 12px;
            }
            #overallBar {
                background-color: #1a1f2e;
                border-radius: 4px;
                border: none;
            }
            #overallBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #00ff88, stop:1 #00d4aa);
                border-radius: 4px;
            }

            /* Buttons */
            #ankiBtn {
                background-color: #00ff88;
                color: #0a0c10;
                border: none;
                border-radius: 8px;
                letter-spacing: 1px;
            }
            #ankiBtn:hover {
                background-color: #00e87a;
            }
            #ankiBtn:pressed {
                background-color: #00c466;
            }

            #unlockBtn {
                background-color: transparent;
                color: #2d3748;
                border: 1px solid #1e2330;
                border-radius: 8px;
                letter-spacing: 1px;
            }
            #unlockBtn:hover {
                border-color: #e53e3e;
                color: #e53e3e;
            }
            #unlockBtn:pressed {
                background-color: #1a0a0a;
            }

            #footer {
                color: #1a2030;
                letter-spacing: 2px;
            }

            /* Celebration */
            #congratsLabel {
                color: #00ff88;
                letter-spacing: 4px;
            }
            #subCongrats {
                color: #4a5568;
                letter-spacing: 1px;
            }
            #closeBtn {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #00ff88, stop:1 #00d4aa);
                color: #0a0c10;
                border: none;
                border-radius: 8px;
                letter-spacing: 2px;
                font-weight: bold;
            }
            #closeBtn:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #00e87a, stop:1 #00c49a);
            }
            #closeBtn:pressed {
                background-color: #00a870;
            }

            /* Azkar button */
            #azkarBtn {
                background-color: #1a1040;
                color: #a78bfa;
                border: 1px solid #4c1d95;
                border-radius: 8px;
                letter-spacing: 1px;
            }
            #azkarBtn:hover {
                background-color: #2d1f60;
                border-color: #7c3aed;
            }
            #azkarBtn:pressed {
                background-color: #150d30;
            }
            #azkarBtnDone {
                background-color: #0d1a14;
                color: #00ff88;
                border: 1px solid #00ff88;
                border-radius: 8px;
                letter-spacing: 1px;
            }
            #azkarBtnDone:hover {
                background-color: #1a0a0a;
                color: #f6ad55;
                border-color: #f6ad55;
            }

            /* Streak */
            #streakLabel {
                color: #f6ad55;
                letter-spacing: 2px;
            }
        """)


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────
def main():
    # Register on startup
    register_startup()

    # Ensure Azkar image is available locally
    ensure_azkar_image()

    # Install keyboard hook + run message pump in background
    install_hook()
    hook_thread = threading.Thread(target=pump_messages, daemon=True)
    hook_thread.start()

    # Launch Qt app
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)

    window = LockScreen()
    window.show()

    exit_code = app.exec()
    remove_hook()
    os._exit(0)  # force-kill any lingering threads


if __name__ == "__main__":
    main()
