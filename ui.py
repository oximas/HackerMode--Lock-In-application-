"""
ui.py — HackerMode UI
TaskRow widget and LockScreen main window.
Imports all logic from core.py — no SQLite, file I/O, or hooks here.
"""

import os
import subprocess
import threading
import ctypes
from ctypes import wintypes

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QPushButton, QLabel, QProgressBar, QFrame
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont

from core import (
    # config
    APP_NAME, ANKI_PATH, AZKAR_LOCAL_PATH, AZKAR_SOURCE_PATH,
    REQUIRED_REVIEWS, PLAY_SOUND,
    # hook
    remove_hook,
    # watchers
    AnkiWatcher, AzkarWindowWatcher,
    # persistence
    azkar_done_today, mark_azkar_done, unmark_azkar_done,
    mark_today_complete, calculate_streak,
)


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
        self.overall_bar.setRange(0, 2)
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
        self._soft_unlocked = False
        self._hard_unlocked = False

        self.watcher = AnkiWatcher()
        self.watcher.progress_update.connect(self._on_progress)
        self.watcher.task_complete.connect(self._on_anki_complete)
        self.watcher.already_done.connect(self._on_already_done)
        self.watcher.start()

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

    # ── Anki callbacks ────────────────────────
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

    # ── Azkar callbacks ───────────────────────
    def _on_azkar_already_done(self):
        """Called on startup when Azkar was already completed today."""
        self._on_azkar_complete()

    def _on_azkar_complete(self):
        """Called when Azkar image viewer is closed."""
        if self._azkar_done:
            return
        self._azkar_done = True
        mark_azkar_done()
        self.azkar_row.mark_done()
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
        """Allow user to manually undo the Azkar tick."""
        if not self._azkar_done:
            return
        self._azkar_done = False
        unmark_azkar_done()
        self._tasks_done = max(0, self._tasks_done - 1)
        self.overall_bar.setValue(self._tasks_done)
        self.azkar_row.status_label.setText("PENDING")
        self.azkar_row.status_label.setObjectName("statusPending")
        self.azkar_row.status_label.style().unpolish(self.azkar_row.status_label)
        self.azkar_row.status_label.style().polish(self.azkar_row.status_label)
        self.azkar_row._done = False
        self.azkar_row.setObjectName("taskRow")
        self.azkar_row.style().unpolish(self.azkar_row)
        self.azkar_row.style().polish(self.azkar_row)
        self.azkar_btn.setText("🌙  Open Azkar")
        self.azkar_btn.setObjectName("azkarBtn")
        self.azkar_btn.style().unpolish(self.azkar_btn)
        self.azkar_btn.style().polish(self.azkar_btn)
        self.azkar_btn.clicked.disconnect()
        self.azkar_btn.clicked.connect(self._open_azkar)

    # ── Unlock logic ──────────────────────────
    def _check_all_complete(self):
        if self._tasks_done >= 1 and not self._soft_unlocked:
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
            self._hard_unlocked = True
            QTimer.singleShot(600, self._show_enter_button)

    def _show_enter_button(self):
        self.enter_btn.setVisible(True)
        if PLAY_SOUND:
            threading.Thread(target=self._play_victory_sound, daemon=True).start()

    def _play_victory_sound(self):
        import winsound
        for freq, duration in [(523, 120), (659, 120), (784, 200)]:
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
            our_hwnd   = int(self.winId())
            SW_SHOW    = 5
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            ctypes.windll.user32.ShowWindow(hwnd, SW_SHOW)
            ctypes.windll.user32.SetWindowPos(
                our_hwnd, hwnd, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
            )
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
        """Place our window just below Anki — only when Anki is in the foreground."""
        if self._soft_unlocked:
            if hasattr(self, '_enforce_timer'):
                self._enforce_timer.stop()
            return
        try:
            anki_hwnd = self._find_anki_hwnd()
            if not anki_hwnd:
                return
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
            #ankiBtn:hover  { background-color: #00e87a; }
            #ankiBtn:pressed { background-color: #00c466; }

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
            #unlockBtn:pressed { background-color: #1a0a0a; }

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
            #closeBtn:pressed { background-color: #00a870; }

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
            #azkarBtn:pressed { background-color: #150d30; }

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
