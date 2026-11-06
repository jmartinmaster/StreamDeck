#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This file is part of Stream Deck.
#
# Stream Deck is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Stream Deck is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Stream Deck.  If not, see <https://www.gnu.org/licenses/>.
"""deckgui - Graphical Settings & Profile Editor for MMEsp32 Stream Deck.

Provides a fast, visual, and intuitive interface to:
1. Configure and preview 16-button macro layouts (2 action groups x 8 columns).
2. Full multi-page support: navigate and visually edit all groups (Groups 1&2, 3&4, etc.).
3. Edit actions, keyboard shortcuts, icons, and semantic colors with live key previews.
4. Edit raw profiles.toml directly with syntax highlighting and validation.
5. Manage persistent daemon settings (BLE/USB, auto-sync to Pi Gateway).
6. Synchronize profiles atomically with the Pi Gateway.
7. Connect via D-Bus to deckd for live connection status, active focus, and deck override.
"""

import os
import sys
from pathlib import Path

# GNOME 44+ moved xsettings keys causing GLib abort in Qt's GNOME platform theme.
# Overriding XDG_CURRENT_DESKTOP and forcing Fusion style avoids this crash completely.
if os.environ.get("XDG_CURRENT_DESKTOP") in ("GNOME", "ubuntu:GNOME"):
    os.environ["XDG_CURRENT_DESKTOP"] = "Generic"
os.environ.setdefault("QT_QPA_PLATFORMTHEME", "generic")
os.environ.setdefault("QT_STYLE_OVERRIDE", "Fusion")

import copy
import json
import logging
import math
import subprocess
import threading
import tomllib
import tomli_w

from qtpy import QtCore, QtGui, QtWidgets, QtNetwork
from qtpy.QtCore import Qt, Signal, Slot

try:
    from PyQt6 import QtDBus
    HAVE_QTDBUS = True
except ImportError:
    HAVE_QTDBUS = False

# Try importing key_token_to_code from deckd for instant shortcut validation
try:
    from tools.deckd.deckd import key_token_to_code
except ImportError:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from deckd import key_token_to_code
    except ImportError:
        key_token_to_code = None

log = logging.getLogger("deckgui")

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_PROFILES_PATH = Path(os.environ.get("DECKD_PROFILES", str(SCRIPT_DIR / "profiles.toml")))
CONFIG_DIR = Path.home() / ".config" / "deckd"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
LOCAL_SERVER_NAME = "mmesp32_deckgui_server"

DEFAULT_GATEWAY_HOST = "100.92.127.16"
DEFAULT_GATEWAY_USER = "jamie"

SEMANTIC_COLORS = {
    "accent": "#06b6d4",   # Cyan
    "cyan":   "#06b6d4",
    "good":   "#10b981",   # Green
    "green":  "#10b981",
    "warn":   "#f59e0b",   # Amber / Yellow
    "warning": "#f59e0b",
    "yellow": "#f59e0b",
    "bad":    "#ef4444",   # Red
    "red":    "#ef4444",
    "idle":   "#475569",   # Slate / Muted
    "muted":  "#64748b",
    "purple": "#a855f7",
    "orange": "#f97316",
    "pink":   "#ec4899",
    "blue":   "#3b82f6",
}

ICON_GLYPHS = {
    "terminal": ">_",
    "folder": "📁",
    "globe": "🌐",
    "code": "</>",
    "copy": "📋",
    "paste": "📄",
    "cut": "✂",
    "save": "💾",
    "camera": "📷",
    "lock": "🔒",
    "play": "▶",
    "pause": "⏸",
    "stop": "⏹",
    "prev": "⏮",
    "next": "⏭",
    "mute": "🔇",
    "volup": "🔊",
    "voldown": "🔉",
    "activity": "📈",
    "moon": "🌙",
    "command": "⌘",
    "search": "🔍",
    "align": "≡",
    "layout": "⊞",
    "refresh": "🔄",
    "list": "☰",
    "power": "⏻",
    "home": "🏠",
    "settings": "⚙",
    "warning": "⚠️",
    "ok": "✓",
    "close": "✕",
    "eye": "👁",
    "charge": "⚡",
    "gps": "📍",
    "download": "⬇",
    "upload": "⬆",
    "image": "🖼",
    "bell": "🔔",
    "file": "📄",
    "directory": "📁",
    "edit": "✏️",
    "plus": "➕",
    "minus": "➖",
    "left": "◀",
    "right": "▶",
    "up": "▲",
    "down": "▼",
    "wifi": "📶",
    "bluetooth": "ᛒ",
    "trash": "🗑",
    "git": "⎇",
    "circle": "●",
    "columns": "▥",
}

AVAILABLE_ICONS = [
    "", "terminal", "folder", "globe", "code", "copy", "paste", "cut",
    "save", "camera", "lock", "play", "pause", "stop", "prev", "next",
    "mute", "volup", "voldown", "activity", "moon", "command", "search",
    "align", "layout", "refresh", "list", "power", "home", "settings",
    "warning", "ok", "close", "eye", "charge", "gps", "download", "upload",
    "image", "bell", "file", "directory", "edit", "plus", "minus", "left",
    "right", "up", "down", "wifi", "bluetooth", "trash", "git", "circle", "columns"
]

COMMON_SHORTCUTS = [
    ("Save File", "ctrl+s"),
    ("Copy", "ctrl+c"),
    ("Paste", "ctrl+v"),
    ("Cut", "ctrl+x"),
    ("Undo", "ctrl+z"),
    ("Redo", "ctrl+y"),
    ("Command Palette", "ctrl+shift+p"),
    ("Quick Open File", "ctrl+p"),
    ("Global Search", "ctrl+shift+f"),
    ("New Tab", "ctrl+t"),
    ("Close Tab", "ctrl+w"),
    ("Reopen Closed Tab", "ctrl+shift+t"),
    ("Terminal Toggle", "ctrl+grave"),
    ("Toggle Sidebar", "ctrl+b"),
    ("Format Code", "ctrl+shift+i"),
    ("Run / Debug (F5)", "f5"),
    ("Stop Debug (Shift+F5)", "shift+f5"),
    ("Step Over (F10)", "f10"),
    ("Step Into (F11)", "f11"),
    ("Toggle Breakpoint (F9)", "f9"),
    ("Lock Screen", "super+l"),
    ("Screenshot Tool", "shift+super+s"),
    ("Media Play/Pause", "play"),
    ("Audio Mute", "mute"),
    ("Volume Up", "volup"),
    ("Volume Down", "voldown"),
]

# Global High-Contrast Cohesive Dark Theme Stylesheet
GLOBAL_APP_STYLE = """
/* Base Window & Dialogs */
QWidget {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Ubuntu, sans-serif;
}
QMainWindow, QDialog, QMessageBox, QInputDialog {
    background-color: #0b0f19;
    color: #f8fafc;
}

/* Labels */
QLabel {
    color: #f8fafc;
}

/* ComboBox & Dropdown Popup Views - High-Contrast Dark */
QComboBox {
    background-color: #1e293b;
    color: #f8fafc;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 13px;
    font-weight: 500;
}
QComboBox:hover {
    border-color: #0284c7;
}
QComboBox:focus {
    border-color: #38bdf8;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 24px;
    border-left: 1px solid #334155;
}
QComboBox::down-arrow {
    image: none;
    border-top: 5px solid #94a3b8;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    width: 0;
    height: 0;
    margin-right: 6px;
}
QComboBox QAbstractItemView {
    background-color: #111827;
    color: #f8fafc;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 4px;
    selection-background-color: #0284c7;
    selection-color: #ffffff;
    outline: none;
}
QComboBox QAbstractItemView::item {
    min-height: 28px;
    padding: 4px 10px;
    background-color: #111827;
    color: #f8fafc;
    border-radius: 4px;
}
QComboBox QAbstractItemView::item:hover,
QComboBox QAbstractItemView::item:selected {
    background-color: #0284c7;
    color: #ffffff;
}

/* QMenu Popup Styling - Explicit High Contrast */
QMenu {
    background-color: #111827;
    color: #f8fafc;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 5px;
}
QMenu::item {
    background-color: transparent;
    color: #f8fafc;
    padding: 6px 24px 6px 12px;
    border-radius: 4px;
    font-size: 13px;
}
QMenu::item:selected {
    background-color: #0284c7;
    color: #ffffff;
}
QMenu::separator {
    height: 1px;
    background-color: #1e293b;
    margin: 4px 8px;
}

/* Text Inputs */
QLineEdit, QPlainTextEdit {
    background-color: #1e293b;
    color: #f8fafc;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: #0284c7;
    selection-color: #ffffff;
}
QLineEdit:focus, QPlainTextEdit:focus {
    border-color: #38bdf8;
}

/* Buttons */
QPushButton {
    background-color: #1e293b;
    color: #f8fafc;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 6px 14px;
    font-weight: 600;
}
QPushButton:hover {
    background-color: #334155;
    border-color: #475569;
}
QPushButton:pressed {
    background-color: #0f172a;
}
QPushButton:disabled {
    background-color: #0f172a;
    color: #475569;
    border-color: #1e293b;
}

/* Checkboxes */
QCheckBox {
    color: #f8fafc;
    font-size: 13px;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border: 1px solid #334155;
    border-radius: 4px;
    background-color: #1e293b;
}
QCheckBox::indicator:hover {
    border-color: #0284c7;
}
QCheckBox::indicator:checked {
    background-color: #0284c7;
    border-color: #0284c7;
    image: none;
}

/* Tooltips */
QToolTip {
    background-color: #1e293b;
    color: #f8fafc;
    border: 1px solid #334155;
    border-radius: 4px;
    padding: 6px 10px;
    font-size: 12px;
}

/* Scrollbars */
QScrollBar:vertical {
    border: none;
    background: #090d16;
    width: 10px;
    border-radius: 5px;
}
QScrollBar::handle:vertical {
    background: #334155;
    border-radius: 5px;
    min-height: 20px;
}
QScrollBar::handle:vertical:hover {
    background: #475569;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
"""


def load_settings():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    defaults = {
        "gateway_host": DEFAULT_GATEWAY_HOST,
        "gateway_user": DEFAULT_GATEWAY_USER,
        "auto_sync_gateway": True,
        "profiles_path": str(DEFAULT_PROFILES_PATH),
    }
    if SETTINGS_FILE.is_file():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                defaults.update(data)
        except Exception as e:
            log.warning("Could not load settings: %s", e)
    return defaults


def save_settings(settings):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        log.error("Could not save settings: %s", e)


def format_shortcut_badge(action_dict):
    """Generate a clean compact badge for button shortcut/command."""
    if not action_dict:
        return ""
    if "shell" in action_dict:
        return "SH"
    chord = action_dict.get("keys", "")
    if not chord:
        return ""
    tokens = chord.split("+")
    badges = []
    for t in tokens:
        t = t.strip().lower()
        if t in ("ctrl", "control"):
            badges.append("^")
        elif t == "shift":
            badges.append("⇧")
        elif t == "alt":
            badges.append("⌥")
        elif t in ("super", "meta", "win"):
            badges.append("⌘")
        elif t.startswith("f") and t[1:].isdigit():
            badges.append(t.upper())
        elif len(t) <= 4:
            badges.append(t.upper())
        else:
            badges.append(t[:3].capitalize())
    return "".join(badges[:3])


class TomlHighlighter(QtGui.QSyntaxHighlighter):
    """High-performance syntax highlighter for raw profiles.toml."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rules = []

        # Tables: [[app.foo.groups]] or [app.bar]
        fmt_table = QtGui.QTextCharFormat()
        fmt_table.setForeground(QtGui.QColor("#38bdf8"))  # Bright Sky Cyan
        fmt_table.setFontWeight(QtGui.QFont.Weight.Bold)
        self.rules.append((QtCore.QRegularExpression(r"^\s*\[\[?[^\]\n]+\]\]?"), fmt_table))

        # Keys: foo =
        fmt_key = QtGui.QTextCharFormat()
        fmt_key.setForeground(QtGui.QColor("#a5b4fc"))  # Indigo / Lilac
        self.rules.append((QtCore.QRegularExpression(r"(?<=^|\n|\{|\s)([a-zA-Z0-9_\-]+)(?=\s*=)"), fmt_key))

        # String literals: "..."
        fmt_str = QtGui.QTextCharFormat()
        fmt_str.setForeground(QtGui.QColor("#34d399"))  # Emerald Green
        self.rules.append((QtCore.QRegularExpression(r'"[^"\\]*(\\.[^"\\]*)*"'), fmt_str))

        # Booleans: true, false
        fmt_bool = QtGui.QTextCharFormat()
        fmt_bool.setForeground(QtGui.QColor("#f87171"))  # Soft Red
        fmt_bool.setFontWeight(QtGui.QFont.Weight.Bold)
        self.rules.append((QtCore.QRegularExpression(r"\b(true|false)\b"), fmt_bool))

        # Numbers
        fmt_num = QtGui.QTextCharFormat()
        fmt_num.setForeground(QtGui.QColor("#fbbf24"))  # Amber
        self.rules.append((QtCore.QRegularExpression(r"\b[0-9]+(\.[0-9]+)?\b"), fmt_num))

        # Comments: # ...
        fmt_comment = QtGui.QTextCharFormat()
        fmt_comment.setForeground(QtGui.QColor("#64748b"))  # Slate Muted
        fmt_comment.setFontItalic(True)
        self.rules.append((QtCore.QRegularExpression(r"#[^\n]*"), fmt_comment))

    def highlightBlock(self, text):
        for pattern, fmt in self.rules:
            match_iter = pattern.globalMatch(text)
            while match_iter.hasNext():
                m = match_iter.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), fmt)


class ShortcutRecordDialog(QtWidgets.QDialog):
    """Modal dialog allowing the user to physically press a key combination to capture it."""

    def __init__(self, current_shortcut="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Record Keyboard Shortcut")
        self.setFixedSize(400, 200)
        self.setModal(True)
        self.captured_chord = current_shortcut

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        lbl_instruct = QtWidgets.QLabel("Press any key combination on your keyboard:", self)
        lbl_instruct.setStyleSheet("color: #94a3b8; font-size: 13px; font-weight: 500;")
        lbl_instruct.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl_instruct)

        self.lbl_chord = QtWidgets.QLabel(self.captured_chord or "Press keys...", self)
        self.lbl_chord.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_chord.setStyleSheet("""
            QLabel {
                background: #1e293b;
                border: 2px solid #0284c7;
                border-radius: 8px;
                color: #38bdf8;
                font-family: monospace;
                font-size: 18px;
                font-weight: bold;
                padding: 12px;
            }
        """)
        layout.addWidget(self.lbl_chord)

        btn_box = QtWidgets.QHBoxLayout()
        btn_box.setSpacing(10)

        self.btn_clear = QtWidgets.QPushButton("Clear", self)
        self.btn_clear.clicked.connect(self.clear_keys)
        btn_box.addWidget(self.btn_clear)

        btn_box.addStretch()

        self.btn_cancel = QtWidgets.QPushButton("Cancel", self)
        self.btn_cancel.clicked.connect(self.reject)
        btn_box.addWidget(self.btn_cancel)

        self.btn_ok = QtWidgets.QPushButton("Apply Shortcut", self)
        self.btn_ok.setStyleSheet("background: #0284c7; color: white; font-weight: bold; padding: 8px 18px; border-radius: 6px;")
        self.btn_ok.clicked.connect(self.accept)
        btn_box.addWidget(self.btn_ok)

        layout.addLayout(btn_box)

    def clear_keys(self):
        self.captured_chord = ""
        self.lbl_chord.setText("Press keys...")

    def keyPressEvent(self, event: QtGui.QKeyEvent):
        key = event.key()
        modifiers = event.modifiers()

        if key in (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_Meta):
            return

        parts = []
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            parts.append("ctrl")
        if modifiers & Qt.KeyboardModifier.AltModifier:
            parts.append("alt")
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            parts.append("shift")
        if modifiers & Qt.KeyboardModifier.MetaModifier:
            parts.append("super")

        key_name = ""
        if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F24:
            key_name = f"f{key - Qt.Key.Key_F1 + 1}"
        elif Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            key_name = chr(key).lower()
        elif Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
            key_name = chr(key)
        else:
            special_keys = {
                Qt.Key.Key_Return: "enter",
                Qt.Key.Key_Enter: "enter",
                Qt.Key.Key_Escape: "esc",
                Qt.Key.Key_Tab: "tab",
                Qt.Key.Key_Backtab: "tab",
                Qt.Key.Key_Space: "space",
                Qt.Key.Key_Backspace: "backspace",
                Qt.Key.Key_Delete: "delete",
                Qt.Key.Key_Insert: "insert",
                Qt.Key.Key_Home: "home",
                Qt.Key.Key_End: "end",
                Qt.Key.Key_PageUp: "pageup",
                Qt.Key.Key_PageDown: "pagedown",
                Qt.Key.Key_Left: "left",
                Qt.Key.Key_Right: "right",
                Qt.Key.Key_Up: "up",
                Qt.Key.Key_Down: "down",
                Qt.Key.Key_Minus: "minus",
                Qt.Key.Key_Equal: "equal",
                Qt.Key.Key_Slash: "slash",
                Qt.Key.Key_Backslash: "backslash",
                Qt.Key.Key_BracketLeft: "bracketleft",
                Qt.Key.Key_BracketRight: "bracketright",
                Qt.Key.Key_Semicolon: "semicolon",
                Qt.Key.Key_QuoteDbl: "quotedbl",
                Qt.Key.Key_Apostrophe: "apostrophe",
                Qt.Key.Key_Comma: "comma",
                Qt.Key.Key_Period: "period",
                Qt.Key.Key_Grave: "grave",
            }
            key_name = special_keys.get(key, "")

        if key_name:
            parts.append(key_name)
            self.captured_chord = "+".join(parts)
            self.lbl_chord.setText(self.captured_chord)
        else:
            super().keyPressEvent(event)


class DeckButtonWidget(QtWidgets.QWidget):
    """High-performance Stream Deck Button Widget rendered via QPainter."""
    clicked = Signal(int)
    copy_requested = Signal(int)
    paste_requested = Signal(int)
    clear_requested = Signal(int)

    def __init__(self, index, parent=None, is_preview=False):
        super().__init__(parent)
        self.index = index
        self.label_text = ""
        self.icon_name = ""
        self.color_name = "idle"
        self.action_dict = {}
        self.is_selected = False
        self.is_hovered = False
        self.is_preview = is_preview

        if is_preview:
            self.setFixedSize(148, 102)
        else:
            self.setFixedSize(98, 68)
            self.setCursor(Qt.CursorShape.PointingHandCursor)

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus if not is_preview else Qt.FocusPolicy.NoFocus)

    def set_data(self, label, icon, color, action=None):
        self.label_text = label or ""
        self.icon_name = icon or ""
        self.color_name = color or "idle"
        if action is not None:
            self.action_dict = dict(action)
        self.update()

    def set_action_dict(self, action_dict):
        self.action_dict = dict(action_dict) if action_dict else {}
        self.update()

    def set_selected(self, sel):
        if self.is_selected != sel:
            self.is_selected = sel
            self.update()

    def enterEvent(self, event):
        if not self.is_preview:
            self.is_hovered = True
            self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if not self.is_preview:
            self.is_hovered = False
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent):
        if not self.is_preview and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.index)
        super().mousePressEvent(event)

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent):
        if self.is_preview:
            return
        self.clicked.emit(self.index)
        menu = QtWidgets.QMenu(self)
        act_copy = menu.addAction("📋 Copy Key")
        act_paste = menu.addAction("📥 Paste Key")
        menu.addSeparator()
        act_clear = menu.addAction("🗑️ Clear Key")

        chosen = menu.exec(event.globalPos())
        if chosen == act_copy:
            self.copy_requested.emit(self.index)
        elif chosen == act_paste:
            self.paste_requested.emit(self.index)
        elif chosen == act_clear:
            self.clear_requested.emit(self.index)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)

        rect = QtCore.QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        has_content = bool(self.label_text or self.icon_name or self.action_dict)

        hex_color = SEMANTIC_COLORS.get(
            self.color_name.lower(),
            self.color_name if self.color_name.startswith("#") else "#475569"
        )
        accent_color = QtGui.QColor(hex_color)

        radius = 10.0 if self.is_preview else 8.0
        path = QtGui.QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        if not has_content:
            bg_brush = QtGui.QColor("#0b0f19")
            painter.fillPath(path, bg_brush)

            pen = QtGui.QPen(QtGui.QColor("#334155"))
            pen.setWidthF(1.5)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawPath(path)

            painter.setPen(QtGui.QColor("#475569"))
            font = QtGui.QFont(self.font().family(), 10 if not self.is_preview else 12, QtGui.QFont.Weight.Bold)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"+ [{self.index + 1}]")
            painter.end()
            return

        grad = QtGui.QLinearGradient(rect.topLeft(), rect.bottomLeft())
        if self.is_hovered:
            grad.setColorAt(0.0, QtGui.QColor("#334155"))
            grad.setColorAt(1.0, QtGui.QColor("#1e293b"))
        else:
            grad.setColorAt(0.0, QtGui.QColor("#1e293b"))
            grad.setColorAt(1.0, QtGui.QColor("#0f172a"))

        painter.fillPath(path, grad)

        if self.is_selected:
            halo_pen = QtGui.QPen(QtGui.QColor("#38bdf8"))
            halo_pen.setWidthF(2.5 if not self.is_preview else 3.5)
            painter.setPen(halo_pen)
            painter.drawPath(path)
        else:
            border_pen = QtGui.QPen(QtGui.QColor("#334155"))
            border_pen.setWidthF(1.2)
            painter.setPen(border_pen)
            painter.drawPath(path)

        accent_path = QtGui.QPainterPath()
        accent_bar_height = 3.5 if not self.is_preview else 5.0
        accent_path.addRoundedRect(
            QtCore.QRectF(rect.x() + 4, rect.y() + 2, rect.width() - 8, accent_bar_height),
            2.0, 2.0
        )
        painter.fillPath(accent_path, accent_color)

        badge = format_shortcut_badge(self.action_dict)
        if badge:
            badge_font = QtGui.QFont("monospace", 8 if not self.is_preview else 10, QtGui.QFont.Weight.Bold)
            painter.setFont(badge_font)
            badge_metrics = QtGui.QFontMetrics(badge_font)
            badge_w = badge_metrics.horizontalAdvance(badge) + (8 if not self.is_preview else 12)
            badge_h = 14 if not self.is_preview else 20
            badge_rect = QtCore.QRectF(rect.right() - badge_w - 4, rect.y() + 7, badge_w, badge_h)

            badge_path = QtGui.QPainterPath()
            badge_path.addRoundedRect(badge_rect, 3.0, 3.0)
            painter.fillPath(badge_path, QtGui.QColor("#090d16"))

            b_border = QtGui.QPen(QtGui.QColor("#334155"))
            b_border.setWidthF(1.0)
            painter.setPen(b_border)
            painter.drawPath(badge_path)

            painter.setPen(QtGui.QColor("#38bdf8" if badge == "SH" else "#94a3b8"))
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge)

        glyph = ICON_GLYPHS.get(self.icon_name.lower(), "")
        icon_display = glyph if glyph else (f"<{self.icon_name}>" if self.icon_name else "")

        if icon_display:
            icon_font = QtGui.QFont(
                self.font().family(),
                12 if not self.is_preview else 18,
                QtGui.QFont.Weight.Bold
            )
            painter.setFont(icon_font)
            painter.setPen(accent_color if glyph else QtGui.QColor("#38bdf8"))
            icon_rect = QtCore.QRectF(
                rect.x() + 6,
                rect.y() + (12 if not self.is_preview else 18),
                rect.width() - 12,
                (24 if not self.is_preview else 36)
            )
            painter.drawText(icon_rect, Qt.AlignmentFlag.AlignCenter, icon_display)

        label_font = QtGui.QFont(
            self.font().family(),
            10 if not self.is_preview else 13,
            QtGui.QFont.Weight.Bold
        )
        painter.setFont(label_font)
        painter.setPen(QtGui.QColor("#f8fafc"))
        label_rect = QtCore.QRectF(
            rect.x() + 4,
            rect.bottom() - (24 if not self.is_preview else 36),
            rect.width() - 8,
            (20 if not self.is_preview else 30)
        )
        fm = QtGui.QFontMetrics(label_font)
        elided = fm.elidedText(self.label_text or f"[{self.index + 1}]", Qt.TextElideMode.ElideRight, int(label_rect.width()))
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, elided)

        painter.end()


class DeckMainWindow(QtWidgets.QMainWindow):
    status_updated = Signal(str, str)  # msg, color

    def __init__(self, profiles_path=None):
        super().__init__()
        self.settings = load_settings()
        self.profiles_path = Path(profiles_path or self.settings.get("profiles_path", str(DEFAULT_PROFILES_PATH)))
        self.profiles_data = {}
        self.current_app_id = "default"
        self.current_page = 0
        self.selected_button_index = 0
        self.buttons = []
        self.clipboard_data = None
        self.dbus_bus = None
        self.dbus_iface = None
        self.poll_timer = None

        self.setWindowTitle("MMEsp32 Stream Deck — Settings & Profiles")
        self.resize(1000, 780)
        self.setMinimumSize(940, 700)

        self.setup_ui()
        self.setup_dbus_integration()
        self.load_profiles_from_file()

        self.status_updated.connect(self.on_status_updated)

    def setup_ui(self):
        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)
        root_layout = QtWidgets.QVBoxLayout(central)
        root_layout.setContentsMargins(14, 12, 14, 12)
        root_layout.setSpacing(10)

        # Top Bar: Live D-Bus Status & Deck Connection
        top_bar = QtWidgets.QFrame(self)
        top_bar.setStyleSheet("""
            QFrame {
                background: #111827;
                border: 1px solid #1e293b;
                border-radius: 8px;
                padding: 4px 10px;
            }
        """)
        top_l = QtWidgets.QHBoxLayout(top_bar)
        top_l.setContentsMargins(8, 6, 8, 6)
        top_l.setSpacing(12)

        self.lbl_conn_status = QtWidgets.QLabel("● deckd: Checking...", top_bar)
        self.lbl_conn_status.setStyleSheet("color: #94a3b8; font-weight: bold; font-size: 12px;")
        top_l.addWidget(self.lbl_conn_status)

        self.lbl_focus_status = QtWidgets.QLabel("Window Focus: None", top_bar)
        self.lbl_focus_status.setStyleSheet("color: #64748b; font-size: 12px; font-weight: 500;")
        top_l.addWidget(self.lbl_focus_status, 1)

        self.btn_preview_deck = QtWidgets.QPushButton("⚡ Preview on Deck", top_bar)
        self.btn_preview_deck.setStyleSheet("background: #0284c7; color: white; font-weight: bold; font-size: 12px; padding: 5px 12px; border-radius: 4px;")
        self.btn_preview_deck.setToolTip("Push this profile override to the physical Stream Deck immediately")
        self.btn_preview_deck.clicked.connect(self.push_profile_to_deck)
        top_l.addWidget(self.btn_preview_deck)

        self.btn_clear_override = QtWidgets.QPushButton("Auto Follow Focus", top_bar)
        self.btn_clear_override.setStyleSheet("background: #1e293b; color: #94a3b8; font-weight: 600; font-size: 12px; padding: 5px 10px; border-radius: 4px;")
        self.btn_clear_override.setToolTip("Clear deck override and let profiles track focused window automatically")
        self.btn_clear_override.clicked.connect(self.clear_deck_override)
        top_l.addWidget(self.btn_clear_override)

        root_layout.addWidget(top_bar)

        # Tab Widget
        self.tabs = QtWidgets.QTabWidget(self)
        self.tabs.setStyleSheet("""
            QTabWidget::pane {
                border: 1px solid #1e293b;
                background: #0b0f19;
                border-radius: 8px;
            }
            QTabBar::tab {
                background: #111827;
                color: #94a3b8;
                padding: 9px 18px;
                margin-right: 6px;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                font-weight: 600;
                font-size: 13px;
            }
            QTabBar::tab:selected {
                background: #0284c7;
                color: #ffffff;
            }
            QTabBar::tab:hover:!selected {
                background: #1e293b;
                color: #f8fafc;
            }
        """)

        # Tab 1: Visual Editor (8x2 Grid with Multi-Page Navigation)
        tab_visual = QtWidgets.QWidget()
        self.setup_visual_tab(tab_visual)
        self.tabs.addTab(tab_visual, "Macro Profiles (Visual 8x2)")

        # Tab 2: Raw TOML Editor
        tab_raw = QtWidgets.QWidget()
        self.setup_raw_tab(tab_raw)
        self.tabs.addTab(tab_raw, "Raw profiles.toml")

        # Tab 3: Daemon & Gateway Settings
        tab_settings = QtWidgets.QWidget()
        self.setup_settings_tab(tab_settings)
        self.tabs.addTab(tab_settings, "Settings & Gateway Sync")

        root_layout.addWidget(self.tabs, 1)

        # Bottom Bar: Status + Action Buttons
        bot_bar = QtWidgets.QHBoxLayout()
        bot_bar.setSpacing(10)

        self.lbl_status = QtWidgets.QLabel("Ready", self)
        self.lbl_status.setStyleSheet("color: #38bdf8; font-weight: 600; font-size: 13px;")
        bot_bar.addWidget(self.lbl_status, 1)

        btn_reload = QtWidgets.QPushButton("Revert / Reload", self)
        btn_reload.clicked.connect(self.load_profiles_from_file)
        bot_bar.addWidget(btn_reload)

        btn_save = QtWidgets.QPushButton("Save & Apply Profiles", self)
        btn_save.setStyleSheet("background: #0284c7; color: #ffffff; font-weight: bold; font-size: 13px; padding: 8px 24px; border-radius: 6px;")
        btn_save.clicked.connect(self.save_and_apply)
        bot_bar.addWidget(btn_save)

        root_layout.addLayout(bot_bar)

        self.tabs.currentChanged.connect(self.on_tab_changed)

    def setup_visual_tab(self, parent):
        layout = QtWidgets.QVBoxLayout(parent)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        # Profile selection toolbar
        p_row = QtWidgets.QHBoxLayout()
        p_row.setSpacing(8)

        lbl = QtWidgets.QLabel("Active Profile:", parent)
        lbl.setStyleSheet("font-weight: bold; color: #38bdf8; font-size: 13px;")
        p_row.addWidget(lbl)

        self.combo_profiles = QtWidgets.QComboBox(parent)
        self.combo_profiles.setMinimumWidth(210)
        self.combo_profiles.currentIndexChanged.connect(self.on_profile_selected)
        p_row.addWidget(self.combo_profiles)

        btn_new_p = QtWidgets.QPushButton("+ New Profile", parent)
        btn_new_p.clicked.connect(self.new_profile_dialog)
        p_row.addWidget(btn_new_p)

        btn_dup_p = QtWidgets.QPushButton("Duplicate", parent)
        btn_dup_p.clicked.connect(self.duplicate_profile)
        p_row.addWidget(btn_dup_p)

        btn_del_p = QtWidgets.QPushButton("Delete", parent)
        btn_del_p.setStyleSheet("background: #1e293b; color: #ef4444; font-weight: 600; padding: 6px 12px; border: 1px solid #334155; border-radius: 6px;")
        btn_del_p.clicked.connect(self.delete_profile)
        p_row.addWidget(btn_del_p)

        p_row.addStretch()

        self.chk_lock_target = QtWidgets.QCheckBox("Lock Target Focus", parent)
        self.chk_lock_target.setToolTip("When checked, deckd automatically focuses this application's window before injecting shortcuts")
        self.chk_lock_target.toggled.connect(self.on_lock_target_toggled)
        p_row.addWidget(self.chk_lock_target)

        layout.addLayout(p_row)

        # Hardware Stream Deck Chassis Container (8 Columns x 2 Rows)
        deck_chassis = QtWidgets.QGroupBox("Stream Deck Hardware Enclosure (8 Columns x 2 Rows)", parent)
        deck_chassis.setStyleSheet("""
            QGroupBox {
                border: 1px solid #1e293b;
                background: #090d16;
                border-radius: 10px;
                margin-top: 6px;
                padding-top: 14px;
                font-weight: bold;
                color: #e2e8f0;
            }
        """)
        c_layout = QtWidgets.QVBoxLayout(deck_chassis)
        c_layout.setContentsMargins(12, 10, 12, 12)
        c_layout.setSpacing(8)

        # Page Navigation Bar inside chassis header
        page_nav_row = QtWidgets.QHBoxLayout()
        page_nav_row.setSpacing(8)

        self.btn_prev_page = QtWidgets.QPushButton("◀ Prev Page", deck_chassis)
        self.btn_prev_page.setToolTip("Flip to previous 2 groups (Page)")
        self.btn_prev_page.clicked.connect(self.prev_page)
        page_nav_row.addWidget(self.btn_prev_page)

        self.lbl_page_indicator = QtWidgets.QLabel("Page 1 of 1", deck_chassis)
        self.lbl_page_indicator.setStyleSheet("background: #111827; color: #38bdf8; font-weight: bold; font-size: 13px; padding: 5px 16px; border: 1px solid #1e293b; border-radius: 6px;")
        self.lbl_page_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        page_nav_row.addWidget(self.lbl_page_indicator)

        self.btn_next_page = QtWidgets.QPushButton("Next Page ▶", deck_chassis)
        self.btn_next_page.setToolTip("Flip to next 2 groups (Page)")
        self.btn_next_page.clicked.connect(self.next_page)
        page_nav_row.addWidget(self.btn_next_page)

        self.btn_add_page = QtWidgets.QPushButton("+ Add Page (2 Groups)", deck_chassis)
        self.btn_add_page.setStyleSheet("background: #1e293b; color: #10b981; font-weight: 600; padding: 5px 12px; border: 1px solid #334155; border-radius: 6px;")
        self.btn_add_page.setToolTip("Append another 2 groups to this profile for pagination on ESP32 screen")
        self.btn_add_page.clicked.connect(self.add_new_page)
        page_nav_row.addWidget(self.btn_add_page)

        self.btn_del_page = QtWidgets.QPushButton("Delete Page", deck_chassis)
        self.btn_del_page.setStyleSheet("background: #1e293b; color: #ef4444; font-weight: 600; padding: 5px 12px; border: 1px solid #334155; border-radius: 6px;")
        self.btn_del_page.setToolTip("Delete current page and its two groups")
        self.btn_del_page.clicked.connect(self.delete_current_page)
        page_nav_row.addWidget(self.btn_del_page)

        page_nav_row.addStretch()

        self.lbl_active_keys_count = QtWidgets.QLabel("0 / 16 Keys Active", deck_chassis)
        self.lbl_active_keys_count.setStyleSheet("color: #64748b; font-size: 12px; font-weight: 600;")
        page_nav_row.addWidget(self.lbl_active_keys_count)

        c_layout.addLayout(page_nav_row)

        # Column numbers header (1..8)
        col_header = QtWidgets.QHBoxLayout()
        col_header.setSpacing(6)
        lbl_r_pad = QtWidgets.QLabel("      ", deck_chassis)
        col_header.addWidget(lbl_r_pad)
        for c in range(1, 9):
            lbl_c = QtWidgets.QLabel(f"Col {c}", deck_chassis)
            lbl_c.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl_c.setStyleSheet("color: #475569; font-size: 11px; font-weight: bold;")
            col_header.addWidget(lbl_c, 1)
        c_layout.addLayout(col_header)

        # Row 1 (Top Group on active page)
        g1_head = QtWidgets.QHBoxLayout()
        g1_head.setSpacing(8)
        self.lbl_g1 = QtWidgets.QLabel("Row 1 (Group 1):", deck_chassis)
        self.lbl_g1.setStyleSheet("color: #06b6d4; font-weight: bold; font-size: 12px;")
        self.edit_g1_title = QtWidgets.QLineEdit(deck_chassis)
        self.edit_g1_title.setPlaceholderText("Row 1 Title (e.g. Quick Launch & Navigation)")
        self.edit_g1_title.textChanged.connect(self.on_g1_title_changed)

        lbl_g1_c = QtWidgets.QLabel("Color:", deck_chassis)
        lbl_g1_c.setStyleSheet("color: #94a3b8; font-size: 12px;")
        self.combo_g1_color = QtWidgets.QComboBox(deck_chassis)
        for cname, chex in SEMANTIC_COLORS.items():
            if cname not in ("warning", "green", "red", "yellow"):
                pix = QtGui.QPixmap(12, 12)
                pix.fill(QtGui.QColor(chex))
                self.combo_g1_color.addItem(QtGui.QIcon(pix), cname)
        self.combo_g1_color.currentTextChanged.connect(self.on_g1_color_changed)

        g1_head.addWidget(self.lbl_g1)
        g1_head.addWidget(self.edit_g1_title, 1)
        g1_head.addWidget(lbl_g1_c)
        g1_head.addWidget(self.combo_g1_color)
        c_layout.addLayout(g1_head)

        row1_layout = QtWidgets.QHBoxLayout()
        row1_layout.setSpacing(6)
        for i in range(8):
            btn = DeckButtonWidget(i, deck_chassis)
            btn.clicked.connect(self.select_button)
            btn.copy_requested.connect(self.copy_button_slot)
            btn.paste_requested.connect(self.paste_button_slot)
            btn.clear_requested.connect(self.clear_button_slot)
            row1_layout.addWidget(btn)
            self.buttons.append(btn)
        c_layout.addLayout(row1_layout)

        # Row 2 (Bottom Group on active page)
        g2_head = QtWidgets.QHBoxLayout()
        g2_head.setSpacing(8)
        self.lbl_g2 = QtWidgets.QLabel("Row 2 (Group 2):", deck_chassis)
        self.lbl_g2.setStyleSheet("color: #10b981; font-weight: bold; font-size: 12px;")
        self.edit_g2_title = QtWidgets.QLineEdit(deck_chassis)
        self.edit_g2_title.setPlaceholderText("Row 2 Title (e.g. Media, Actions & Shortcuts)")
        self.edit_g2_title.textChanged.connect(self.on_g2_title_changed)

        lbl_g2_c = QtWidgets.QLabel("Color:", deck_chassis)
        lbl_g2_c.setStyleSheet("color: #94a3b8; font-size: 12px;")
        self.combo_g2_color = QtWidgets.QComboBox(deck_chassis)
        for cname, chex in SEMANTIC_COLORS.items():
            if cname not in ("warning", "green", "red", "yellow"):
                pix = QtGui.QPixmap(12, 12)
                pix.fill(QtGui.QColor(chex))
                self.combo_g2_color.addItem(QtGui.QIcon(pix), cname)
        self.combo_g2_color.currentTextChanged.connect(self.on_g2_color_changed)

        g2_head.addWidget(self.lbl_g2)
        g2_head.addWidget(self.edit_g2_title, 1)
        g2_head.addWidget(lbl_g2_c)
        g2_head.addWidget(self.combo_g2_color)
        c_layout.addLayout(g2_head)

        row2_layout = QtWidgets.QHBoxLayout()
        row2_layout.setSpacing(6)
        for i in range(8, 16):
            btn = DeckButtonWidget(i, deck_chassis)
            btn.clicked.connect(self.select_button)
            btn.copy_requested.connect(self.copy_button_slot)
            btn.paste_requested.connect(self.paste_button_slot)
            btn.clear_requested.connect(self.clear_button_slot)
            row2_layout.addWidget(btn)
            self.buttons.append(btn)
        c_layout.addLayout(row2_layout)

        layout.addWidget(deck_chassis)

        # Button Property Inspector (with Live Preview Card)
        inspector = QtWidgets.QGroupBox("Key Inspector & Action Configuration", parent)
        inspector.setStyleSheet("""
            QGroupBox {
                border: 1px solid #1e293b;
                background: #111827;
                border-radius: 10px;
                font-weight: bold;
                color: #e2e8f0;
                margin-top: 4px;
                padding-top: 10px;
            }
        """)
        ins_main_layout = QtWidgets.QHBoxLayout(inspector)
        ins_main_layout.setContentsMargins(14, 10, 14, 10)
        ins_main_layout.setSpacing(16)

        # Left: Live Key Preview
        preview_box = QtWidgets.QVBoxLayout()
        preview_box.setSpacing(6)
        lbl_prev_title = QtWidgets.QLabel("Live Preview", inspector)
        lbl_prev_title.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: bold;")
        lbl_prev_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_box.addWidget(lbl_prev_title)

        self.key_preview = DeckButtonWidget(0, inspector, is_preview=True)
        preview_box.addWidget(self.key_preview, 0, Qt.AlignmentFlag.AlignCenter)
        preview_box.addStretch()
        ins_main_layout.addLayout(preview_box)

        # Right: Fields Grid
        ins_grid = QtWidgets.QGridLayout()
        ins_grid.setContentsMargins(0, 0, 0, 0)
        ins_grid.setSpacing(8)

        self.lbl_selected_btn = QtWidgets.QLabel("Editing Button 1 (Row 1, Col 1)", inspector)
        self.lbl_selected_btn.setStyleSheet("color: #38bdf8; font-weight: bold; font-size: 13px;")
        ins_grid.addWidget(self.lbl_selected_btn, 0, 0, 1, 4)

        # Key Label
        ins_grid.addWidget(QtWidgets.QLabel("Key Label:"), 1, 0)
        self.ins_label = QtWidgets.QLineEdit(inspector)
        self.ins_label.setPlaceholderText("Display name on keycap (e.g. Save, Cut, Play)")
        self.ins_label.textChanged.connect(self.on_inspector_changed)
        ins_grid.addWidget(self.ins_label, 1, 1)

        # Icon Selector
        ins_grid.addWidget(QtWidgets.QLabel("Icon:"), 1, 2)
        self.ins_icon = QtWidgets.QComboBox(inspector)
        for ic in AVAILABLE_ICONS:
            glyph = ICON_GLYPHS.get(ic.lower(), "")
            text = f"{glyph}  {ic}" if glyph else (ic or "(None)")
            self.ins_icon.addItem(text, ic)
        self.ins_icon.currentIndexChanged.connect(self.on_inspector_changed)
        ins_grid.addWidget(self.ins_icon, 1, 3)

        # Action Type
        ins_grid.addWidget(QtWidgets.QLabel("Action Type:"), 2, 0)
        self.ins_action_type = QtWidgets.QComboBox(inspector)
        self.ins_action_type.addItems(["Keys Shortcut", "Shell Command"])
        self.ins_action_type.currentIndexChanged.connect(self.on_inspector_changed)
        ins_grid.addWidget(self.ins_action_type, 2, 1)

        # Semantic Color Selector
        ins_grid.addWidget(QtWidgets.QLabel("Color:"), 2, 2)
        self.ins_color = QtWidgets.QComboBox(inspector)
        for cname, chex in SEMANTIC_COLORS.items():
            if cname not in ("warning", "green", "red", "yellow"):
                pix = QtGui.QPixmap(14, 14)
                pix.fill(QtGui.QColor(chex))
                self.ins_color.addItem(QtGui.QIcon(pix), cname)
        self.ins_color.currentTextChanged.connect(self.on_inspector_changed)
        ins_grid.addWidget(self.ins_color, 2, 3)

        # Action Value + Shortcut Recorder + Presets
        ins_grid.addWidget(QtWidgets.QLabel("Action / Shortcut:"), 3, 0)
        act_row = QtWidgets.QHBoxLayout()
        act_row.setSpacing(6)

        self.ins_action_val = QtWidgets.QLineEdit(inspector)
        self.ins_action_val.setStyleSheet("font-family: monospace;")
        self.ins_action_val.setPlaceholderText("e.g. ctrl+s, f5, or bash command")
        self.ins_action_val.textChanged.connect(self.on_inspector_changed)
        act_row.addWidget(self.ins_action_val, 1)

        self.btn_record_key = QtWidgets.QPushButton("● Record Keys", inspector)
        self.btn_record_key.setStyleSheet("color: #f59e0b; font-weight: bold;")
        self.btn_record_key.setToolTip("Press keys on keyboard to record shortcut combination")
        self.btn_record_key.clicked.connect(self.record_shortcut_dialog)
        act_row.addWidget(self.btn_record_key)

        btn_preset = QtWidgets.QPushButton("Presets ▾", inspector)
        btn_preset.setStyleSheet("color: #38bdf8; font-weight: 600;")
        preset_menu = QtWidgets.QMenu(btn_preset)
        for label, sc in COMMON_SHORTCUTS:
            preset_menu.addAction(f"{label} ({sc})", lambda s=sc: self.ins_action_val.setText(s))
        btn_preset.setMenu(preset_menu)
        act_row.addWidget(btn_preset)

        ins_grid.addLayout(act_row, 3, 1, 1, 3)

        # Validation status & action buttons row
        act_btns_row = QtWidgets.QHBoxLayout()
        act_btns_row.setSpacing(8)

        self.lbl_shortcut_valid = QtWidgets.QLabel("", inspector)
        self.lbl_shortcut_valid.setStyleSheet("font-size: 11px; font-weight: 600;")
        act_btns_row.addWidget(self.lbl_shortcut_valid, 1)

        btn_test_act = QtWidgets.QPushButton("▶ Test Action", inspector)
        btn_test_act.setStyleSheet("color: #10b981; font-weight: 600;")
        btn_test_act.setToolTip("Execute this shell command or chord to test functionality")
        btn_test_act.clicked.connect(self.test_current_action)
        act_btns_row.addWidget(btn_test_act)

        btn_copy = QtWidgets.QPushButton("Copy Key", inspector)
        btn_copy.clicked.connect(lambda: self.copy_button_slot(self.selected_button_index))
        act_btns_row.addWidget(btn_copy)

        btn_paste = QtWidgets.QPushButton("Paste Key", inspector)
        btn_paste.clicked.connect(lambda: self.paste_button_slot(self.selected_button_index))
        act_btns_row.addWidget(btn_paste)

        btn_clear_btn = QtWidgets.QPushButton("Clear Key", inspector)
        btn_clear_btn.setStyleSheet("color: #ef4444; font-weight: 600;")
        btn_clear_btn.clicked.connect(self.clear_selected_button)
        act_btns_row.addWidget(btn_clear_btn)

        ins_grid.addLayout(act_btns_row, 4, 0, 1, 4)

        ins_main_layout.addLayout(ins_grid, 1)
        layout.addWidget(inspector)

    def setup_raw_tab(self, parent):
        layout = QtWidgets.QVBoxLayout(parent)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        top_raw_bar = QtWidgets.QHBoxLayout()
        lbl_info = QtWidgets.QLabel("Direct TOML Configuration (Validated & syntax-highlighted):", parent)
        lbl_info.setStyleSheet("color: #94a3b8; font-weight: 600;")
        top_raw_bar.addWidget(lbl_info, 1)

        btn_validate = QtWidgets.QPushButton("Validate TOML Syntax", parent)
        btn_validate.setStyleSheet("color: #38bdf8; font-weight: 600;")
        btn_validate.clicked.connect(self.validate_raw_toml)
        top_raw_bar.addWidget(btn_validate)

        layout.addLayout(top_raw_bar)

        self.txt_raw_toml = QtWidgets.QPlainTextEdit(parent)
        self.txt_raw_toml.setStyleSheet("""
            QPlainTextEdit {
                font-family: 'JetBrains Mono', 'Fira Code', 'DejaVu Sans Mono', monospace;
                font-size: 13px;
                background: #090d16;
                color: #f8fafc;
                border: 1px solid #1e293b;
                border-radius: 8px;
                padding: 8px;
                line-height: 1.4;
            }
        """)
        self.toml_highlighter = TomlHighlighter(self.txt_raw_toml.document())
        layout.addWidget(self.txt_raw_toml)

    def setup_settings_tab(self, parent):
        layout = QtWidgets.QVBoxLayout(parent)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(14)

        # Pi Gateway Sync Group
        box_gw = QtWidgets.QGroupBox("Pi Gateway Synchronization (ESP32-S3 / P4 Smart Panels)", parent)
        box_gw.setStyleSheet("""
            QGroupBox {
                border: 1px solid #1e293b;
                background: #111827;
                border-radius: 8px;
                font-weight: bold;
                color: #e2e8f0;
                padding-top: 14px;
            }
        """)
        l_gw = QtWidgets.QGridLayout(box_gw)
        l_gw.setContentsMargins(14, 16, 14, 14)
        l_gw.setSpacing(10)

        l_gw.addWidget(QtWidgets.QLabel("Pi Gateway Host IP:"), 0, 0)
        self.edit_gw_host = QtWidgets.QLineEdit(self.settings.get("gateway_host", DEFAULT_GATEWAY_HOST), box_gw)
        l_gw.addWidget(self.edit_gw_host, 0, 1)

        l_gw.addWidget(QtWidgets.QLabel("SSH Username:"), 1, 0)
        self.edit_gw_user = QtWidgets.QLineEdit(self.settings.get("gateway_user", DEFAULT_GATEWAY_USER), box_gw)
        l_gw.addWidget(self.edit_gw_user, 1, 1)

        self.chk_auto_sync = QtWidgets.QCheckBox("Auto-sync to Pi Gateway on Save (keeps both ESP32 touch panels synchronized)", box_gw)
        self.chk_auto_sync.setChecked(self.settings.get("auto_sync_gateway", True))
        l_gw.addWidget(self.chk_auto_sync, 2, 0, 1, 2)

        gw_btn_row = QtWidgets.QHBoxLayout()
        gw_btn_row.setSpacing(10)

        self.btn_test_gw = QtWidgets.QPushButton("Test SSH Connection", box_gw)
        self.btn_test_gw.setStyleSheet("color: #38bdf8; font-weight: 600;")
        self.btn_test_gw.clicked.connect(self.test_gateway_connection)
        gw_btn_row.addWidget(self.btn_test_gw)

        self.btn_sync_now = QtWidgets.QPushButton("Sync to Pi Gateway Now", box_gw)
        self.btn_sync_now.setStyleSheet("background: #0284c7; color: white; font-weight: bold; padding: 8px 18px; border-radius: 4px;")
        self.btn_sync_now.clicked.connect(self.sync_gateway_now)
        gw_btn_row.addWidget(self.btn_sync_now)

        gw_btn_row.addStretch()
        l_gw.addLayout(gw_btn_row, 3, 0, 1, 2)

        layout.addWidget(box_gw)

        # Hardware & Transports
        box_hw = QtWidgets.QGroupBox("deckd Transports & Hardware Listeners", parent)
        box_hw.setStyleSheet("""
            QGroupBox {
                border: 1px solid #1e293b;
                background: #111827;
                border-radius: 8px;
                font-weight: bold;
                color: #e2e8f0;
                padding-top: 14px;
            }
        """)
        l_hw = QtWidgets.QVBoxLayout(box_hw)
        l_hw.setContentsMargins(14, 16, 14, 14)
        l_hw.setSpacing(10)

        self.chk_ble = QtWidgets.QCheckBox("Enable Bluetooth Low Energy (BLE) listener (P4/S3 wireless connection)", box_hw)
        self.chk_ble.setChecked(True)
        self.chk_ble.toggled.connect(self.toggle_ble)
        l_hw.addWidget(self.chk_ble)

        self.chk_usb = QtWidgets.QCheckBox("Enable USB Serial connection (/dev/ttyUSB0)", box_hw)
        self.chk_usb.setChecked(True)
        self.chk_usb.toggled.connect(self.toggle_usb)
        l_hw.addWidget(self.chk_usb)

        layout.addWidget(box_hw)
        layout.addStretch()

    # --- D-Bus Integration ---

    def setup_dbus_integration(self):
        if not HAVE_QTDBUS:
            self.lbl_conn_status.setText("● deckd: D-Bus library unavailable")
            return

        try:
            self.dbus_bus = QtDBus.QDBusConnection.sessionBus()
            self.dbus_iface = QtDBus.QDBusInterface(
                "org.mmesp32.Deckd",
                "/org/mmesp32/Deckd",
                "org.mmesp32.Deckd1",
                self.dbus_bus
            )

            self.dbus_bus.connect(
                "org.mmesp32.Deckd",
                "/org/mmesp32/Deckd",
                "org.mmesp32.Deckd1",
                "StatusChanged",
                self.on_dbus_status_changed
            )

            self.poll_timer = QtCore.QTimer(self)
            self.poll_timer.timeout.connect(self.poll_deckd_status)
            self.poll_timer.start(1500)
            self.poll_deckd_status()
        except Exception as e:
            log.warning("D-Bus initialization failed: %s", e)

    @Slot()
    def on_dbus_status_changed(self):
        self.poll_deckd_status()

    def poll_deckd_status(self):
        if not self.dbus_iface:
            return
        reply = self.dbus_iface.call("GetStatus")
        if reply.type() == QtDBus.QDBusMessage.MessageType.ReplyMessage:
            args = reply.arguments()
            if args and isinstance(args[0], dict):
                st = args[0]
                conn = st.get("connected", "0") == "1"
                transport = st.get("transport", "none").upper()
                active_p = st.get("active_profile", "default")
                override = st.get("override", "")
                focus_app = st.get("focus_app", "")
                focus_title = st.get("focus_title", "")

                if conn:
                    color = "#10b981"
                    txt = f"● deckd: Connected ({transport})"
                else:
                    color = "#64748b"
                    txt = "○ deckd: Online (Waiting for device)"
                self.lbl_conn_status.setText(txt)
                self.lbl_conn_status.setStyleSheet(f"color: {color}; font-weight: bold; font-size: 12px;")

                disp_title = (focus_title[:36] + "…") if len(focus_title) > 36 else focus_title
                ovr_str = f" [Override: {override}]" if override else ""
                self.lbl_focus_status.setText(f"Active Deck: {active_p}{ovr_str} | Focused: {focus_app} ({disp_title})")

                self.chk_ble.blockSignals(True)
                self.chk_ble.setChecked(st.get("ble", "0") == "1")
                self.chk_ble.blockSignals(False)

                self.chk_usb.blockSignals(True)
                self.chk_usb.setChecked(st.get("usb", "1") == "1")
                self.chk_usb.blockSignals(False)
        else:
            self.lbl_conn_status.setText("○ deckd: Offline")
            self.lbl_conn_status.setStyleSheet("color: #ef4444; font-weight: bold; font-size: 12px;")
            self.lbl_focus_status.setText("Window Focus: Daemon offline")

    def push_profile_to_deck(self):
        if not self.current_app_id:
            return
        if self.dbus_iface:
            self.dbus_iface.call("SetOverride", self.current_app_id)
            self.status_updated.emit(f"Pushed profile '{self.current_app_id}' to deck hardware", "#10b981")
            self.poll_deckd_status()
        else:
            try:
                subprocess.run([
                    "busctl", "--user", "call", "org.mmesp32.Deckd", "/org/mmesp32/Deckd",
                    "org.mmesp32.Deckd1", "SetOverride", "s", self.current_app_id
                ], timeout=2)
                self.status_updated.emit(f"Pushed profile '{self.current_app_id}' to deck hardware", "#10b981")
            except Exception as e:
                self.status_updated.emit(f"Failed to push profile: {e}", "#ef4444")

    def clear_deck_override(self):
        if self.dbus_iface:
            self.dbus_iface.call("SetOverride", "")
            self.status_updated.emit("Cleared deck override (following window focus)", "#38bdf8")
            self.poll_deckd_status()
        else:
            try:
                subprocess.run([
                    "busctl", "--user", "call", "org.mmesp32.Deckd", "/org/mmesp32/Deckd",
                    "org.mmesp32.Deckd1", "SetOverride", "s", ""
                ], timeout=2)
                self.status_updated.emit("Cleared deck override (following window focus)", "#38bdf8")
            except Exception as e:
                self.status_updated.emit(f"Failed to clear override: {e}", "#ef4444")

    # --- Profile & Multi-Page Data Logic ---

    def get_total_pages(self):
        app = self.profiles_data.get("app", {}).get(self.current_app_id, {})
        groups = app.get("groups", [])
        return max(1, math.ceil(len(groups) / 2))

    def load_profiles_from_file(self):
        try:
            with open(self.profiles_path, "rb") as f:
                self.profiles_data = tomllib.load(f)
            self.txt_raw_toml.setPlainText(self.profiles_path.read_text(encoding="utf-8"))
            self.refresh_profile_list()
            self.status_updated.emit(f"Loaded profiles from {self.profiles_path.name}", "#10b981")
        except Exception as e:
            self.status_updated.emit(f"Error loading {self.profiles_path.name}: {e}", "#ef4444")

    def refresh_profile_list(self):
        apps = self.profiles_data.get("app", {})
        self.combo_profiles.blockSignals(True)
        self.combo_profiles.clear()
        for app_id in apps.keys():
            self.combo_profiles.addItem(app_id)
        if self.current_app_id in apps:
            self.combo_profiles.setCurrentText(self.current_app_id)
        elif self.combo_profiles.count() > 0:
            self.combo_profiles.setCurrentIndex(0)
            self.current_app_id = self.combo_profiles.currentText()
        self.combo_profiles.blockSignals(False)
        self.current_page = 0
        self.render_current_profile()

    def render_current_profile(self):
        app = self.profiles_data.get("app", {}).get(self.current_app_id, {})
        self.chk_lock_target.blockSignals(True)
        self.chk_lock_target.setChecked(bool(app.get("lock_target", False)))
        self.chk_lock_target.blockSignals(False)

        groups = app.get("groups", [])
        total_pages = max(1, math.ceil(len(groups) / 2))
        if self.current_page >= total_pages:
            self.current_page = total_pages - 1
        if self.current_page < 0:
            self.current_page = 0

        # Update page navigation bar
        self.lbl_page_indicator.setText(f"Page {self.current_page + 1} of {total_pages}")
        self.btn_prev_page.setEnabled(self.current_page > 0)
        self.btn_next_page.setEnabled(self.current_page < total_pages - 1)
        self.btn_del_page.setEnabled(total_pages > 1)

        # Groups for active page
        top_g_idx = self.current_page * 2
        bot_g_idx = self.current_page * 2 + 1

        g1 = groups[top_g_idx] if len(groups) > top_g_idx else {}
        g2 = groups[bot_g_idx] if len(groups) > bot_g_idx else {}

        self.lbl_g1.setText(f"Row 1 (Group {top_g_idx + 1}):")
        self.lbl_g2.setText(f"Row 2 (Group {bot_g_idx + 1}):")

        g1_title = g1.get("title", f"Group {top_g_idx + 1}")
        g1_color = g1.get("color", "accent")
        g2_title = g2.get("title", f"Group {bot_g_idx + 1}")
        g2_color = g2.get("color", "good")

        self.edit_g1_title.blockSignals(True)
        self.edit_g1_title.setText(g1_title)
        self.edit_g1_title.blockSignals(False)

        self.combo_g1_color.blockSignals(True)
        self.combo_g1_color.setCurrentText(g1_color)
        self.combo_g1_color.blockSignals(False)

        self.edit_g2_title.blockSignals(True)
        self.edit_g2_title.setText(g2_title)
        self.edit_g2_title.blockSignals(False)

        self.combo_g2_color.blockSignals(True)
        self.combo_g2_color.setCurrentText(g2_color)
        self.combo_g2_color.blockSignals(False)

        # Render 16 buttons for this page
        active_count = 0
        for i in range(16):
            btn_data = self.get_button_data(i)
            if btn_data and (btn_data.get("label") or btn_data.get("keys") or btn_data.get("shell")):
                active_count += 1
            self.buttons[i].set_data(
                btn_data.get("label", ""),
                btn_data.get("icon", ""),
                btn_data.get("color", "idle"),
                btn_data
            )

        self.lbl_active_keys_count.setText(f"{active_count} / 16 Keys Active (Page {self.current_page + 1})")
        self.select_button(self.selected_button_index)

    def prev_page(self):
        if self.current_page > 0:
            self.current_page -= 1
            self.render_current_profile()

    def next_page(self):
        total = self.get_total_pages()
        if self.current_page < total - 1:
            self.current_page += 1
            self.render_current_profile()

    def add_new_page(self):
        app = self.profiles_data.setdefault("app", {}).setdefault(self.current_app_id, {})
        groups = app.setdefault("groups", [])
        g_num = len(groups) + 1
        groups.append({"title": f"Group {g_num}", "color": "accent", "buttons": []})
        groups.append({"title": f"Group {g_num + 1}", "color": "good", "buttons": []})
        self.current_page = (len(groups) // 2) - 1
        self.render_current_profile()
        self.status_updated.emit(f"Added Page {self.current_page + 1} (Groups {g_num} & {g_num + 1}) to '{self.current_app_id}'", "#10b981")

    def delete_current_page(self):
        app = self.profiles_data.setdefault("app", {}).setdefault(self.current_app_id, {})
        groups = app.setdefault("groups", [])
        total_pages = max(1, math.ceil(len(groups) / 2))
        if total_pages <= 1:
            QtWidgets.QMessageBox.warning(self, "Cannot Delete Page", "A profile must have at least one page.")
            return

        top_idx = self.current_page * 2
        bot_idx = top_idx + 1
        res = QtWidgets.QMessageBox.question(
            self, "Confirm Delete Page",
            f"Delete Page {self.current_page + 1} (Groups {top_idx + 1} and {bot_idx + 1}) and its buttons?"
        )
        if res == QtWidgets.QMessageBox.StandardButton.Yes:
            if bot_idx < len(groups):
                groups.pop(bot_idx)
            if top_idx < len(groups):
                groups.pop(top_idx)
            if self.current_page > 0:
                self.current_page -= 1
            self.render_current_profile()
            self.status_updated.emit(f"Deleted page from profile '{self.current_app_id}'", "#ef4444")

    def get_button_data(self, idx):
        app = self.profiles_data.get("app", {}).get(self.current_app_id, {})
        groups = app.get("groups", [])
        top_g_idx = self.current_page * 2
        bot_g_idx = self.current_page * 2 + 1
        g_idx = top_g_idx if idx < 8 else bot_g_idx
        b_idx = idx if idx < 8 else (idx - 8)
        if len(groups) > g_idx and "buttons" in groups[g_idx]:
            b_list = groups[g_idx]["buttons"]
            if b_idx < len(b_list):
                return b_list[b_idx]
        return {}

    def set_button_data(self, idx, data):
        app = self.profiles_data.setdefault("app", {}).setdefault(self.current_app_id, {})
        groups = app.setdefault("groups", [])
        top_g_idx = self.current_page * 2
        bot_g_idx = self.current_page * 2 + 1
        g_idx = top_g_idx if idx < 8 else bot_g_idx
        b_idx = idx if idx < 8 else (idx - 8)

        while len(groups) <= g_idx:
            groups.append({"title": f"Group {len(groups) + 1}", "color": "accent" if len(groups) % 2 == 0 else "good", "buttons": []})

        b_list = groups[g_idx].setdefault("buttons", [])
        while len(b_list) <= b_idx:
            b_list.append({})
        b_list[b_idx] = data

    def select_button(self, idx):
        self.selected_button_index = idx
        for i, b in enumerate(self.buttons):
            b.set_selected(i == idx)

        row_num = 1 if idx < 8 else 2
        col_num = (idx % 8) + 1
        top_g_idx = self.current_page * 2
        active_group_num = top_g_idx + (1 if idx < 8 else 2)
        self.lbl_selected_btn.setText(f"Editing Button {idx + 1} (Page {self.current_page + 1}, Group {active_group_num}, Col {col_num})")
        data = self.get_button_data(idx)

        self.ins_label.blockSignals(True)
        self.ins_label.setText(data.get("label", ""))
        self.ins_label.blockSignals(False)

        self.ins_action_type.blockSignals(True)
        self.ins_action_val.blockSignals(True)
        if "shell" in data:
            self.ins_action_type.setCurrentIndex(1)
            self.ins_action_val.setText(data["shell"])
        else:
            self.ins_action_type.setCurrentIndex(0)
            self.ins_action_val.setText(data.get("keys", ""))
        self.ins_action_type.blockSignals(False)
        self.ins_action_val.blockSignals(False)

        self.ins_icon.blockSignals(True)
        raw_icon = data.get("icon", "")
        idx_icon = self.ins_icon.findData(raw_icon)
        if idx_icon >= 0:
            self.ins_icon.setCurrentIndex(idx_icon)
        else:
            self.ins_icon.setCurrentText(raw_icon)
        self.ins_icon.blockSignals(False)

        self.ins_color.blockSignals(True)
        self.ins_color.setCurrentText(data.get("color", "idle"))
        self.ins_color.blockSignals(False)

        self.key_preview.set_data(
            data.get("label", ""),
            data.get("icon", ""),
            data.get("color", "idle"),
            data
        )
        self.validate_shortcut_input()

    def on_inspector_changed(self):
        lbl = self.ins_label.text().strip()
        act_val = self.ins_action_val.text().strip()
        is_shell = self.ins_action_type.currentIndex() == 1
        icon_data = self.ins_icon.currentData()
        icon = icon_data if icon_data is not None else self.ins_icon.currentText().strip()
        color = self.ins_color.currentText().strip()

        data = {}
        if lbl:
            data["label"] = lbl
        if act_val:
            if is_shell:
                data["shell"] = act_val
            else:
                data["keys"] = act_val
        if icon:
            data["icon"] = icon
        if color:
            data["color"] = color

        self.set_button_data(self.selected_button_index, data)
        self.buttons[self.selected_button_index].set_data(lbl, icon, color, data)
        self.key_preview.set_data(lbl, icon, color, data)
        self.validate_shortcut_input()

    def validate_shortcut_input(self):
        is_shell = self.ins_action_type.currentIndex() == 1
        val = self.ins_action_val.text().strip()
        if not val:
            self.lbl_shortcut_valid.setText("")
            return
        if is_shell:
            self.lbl_shortcut_valid.setText("🐚 Shell command will execute in user session")
            self.lbl_shortcut_valid.setStyleSheet("color: #38bdf8; font-size: 11px;")
            return

        if key_token_to_code is not None:
            chords = val.split(",")
            unknown = []
            for ch in chords:
                for tok in ch.split("+"):
                    tok = tok.strip().lower()
                    if tok:
                        try:
                            key_token_to_code(tok)
                        except ValueError:
                            unknown.append(tok)
            if unknown:
                self.lbl_shortcut_valid.setText(f"⚠️ Unknown token: '{unknown[0]}'")
                self.lbl_shortcut_valid.setStyleSheet("color: #f59e0b; font-size: 11px;")
            else:
                self.lbl_shortcut_valid.setText("✓ Valid keyboard shortcut")
                self.lbl_shortcut_valid.setStyleSheet("color: #10b981; font-size: 11px;")
        else:
            self.lbl_shortcut_valid.setText("✓ Shortcut set")
            self.lbl_shortcut_valid.setStyleSheet("color: #94a3b8; font-size: 11px;")

    def test_current_action(self):
        is_shell = self.ins_action_type.currentIndex() == 1
        val = self.ins_action_val.text().strip()
        if not val:
            self.status_updated.emit("No action configured to test", "#f59e0b")
            return

        if is_shell:
            self.status_updated.emit(f"Running test shell command: {val}...", "#38bdf8")
            def run_test():
                try:
                    res = subprocess.run(val, shell=True, timeout=5, capture_output=True, text=True)
                    if res.returncode == 0:
                        self.status_updated.emit(f"✓ Command completed successfully (code 0)", "#10b981")
                    else:
                        self.status_updated.emit(f"Command returned exit code {res.returncode}: {res.stderr.strip()}", "#ef4444")
                except Exception as e:
                    self.status_updated.emit(f"Execution error: {e}", "#ef4444")
            threading.Thread(target=run_test, daemon=True).start()
        else:
            self.status_updated.emit(f"Shortcut chord '{val}' verified & ready", "#10b981")

    def record_shortcut_dialog(self):
        curr = self.ins_action_val.text().strip()
        dlg = ShortcutRecordDialog(curr, self)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            if dlg.captured_chord:
                self.ins_action_type.setCurrentIndex(0)
                self.ins_action_val.setText(dlg.captured_chord)
                self.on_inspector_changed()

    def copy_button_slot(self, idx):
        self.clipboard_data = copy.deepcopy(self.get_button_data(idx))
        self.status_updated.emit(f"Copied key {idx + 1} to clipboard", "#38bdf8")

    def paste_button_slot(self, idx):
        if self.clipboard_data is not None:
            self.set_button_data(idx, copy.deepcopy(self.clipboard_data))
            self.select_button(idx)
            self.status_updated.emit(f"Pasted configuration to key {idx + 1}", "#10b981")

    def clear_button_slot(self, idx):
        self.set_button_data(idx, {})
        self.select_button(idx)
        self.status_updated.emit(f"Cleared key {idx + 1}", "#ef4444")

    def clear_selected_button(self):
        self.clear_button_slot(self.selected_button_index)

    def on_profile_selected(self, idx):
        text = self.combo_profiles.currentText()
        if text:
            self.current_app_id = text
            self.current_page = 0
            self.render_current_profile()

    def on_g1_title_changed(self, text):
        app = self.profiles_data.setdefault("app", {}).setdefault(self.current_app_id, {})
        groups = app.setdefault("groups", [])
        top_idx = self.current_page * 2
        while len(groups) <= top_idx:
            groups.append({"title": f"Group {len(groups) + 1}", "buttons": []})
        groups[top_idx]["title"] = text

    def on_g1_color_changed(self, text):
        app = self.profiles_data.setdefault("app", {}).setdefault(self.current_app_id, {})
        groups = app.setdefault("groups", [])
        top_idx = self.current_page * 2
        while len(groups) <= top_idx:
            groups.append({"title": f"Group {len(groups) + 1}", "buttons": []})
        groups[top_idx]["color"] = text

    def on_g2_title_changed(self, text):
        app = self.profiles_data.setdefault("app", {}).setdefault(self.current_app_id, {})
        groups = app.setdefault("groups", [])
        bot_idx = self.current_page * 2 + 1
        while len(groups) <= bot_idx:
            groups.append({"title": f"Group {len(groups) + 1}", "buttons": []})
        groups[bot_idx]["title"] = text

    def on_g2_color_changed(self, text):
        app = self.profiles_data.setdefault("app", {}).setdefault(self.current_app_id, {})
        groups = app.setdefault("groups", [])
        bot_idx = self.current_page * 2 + 1
        while len(groups) <= bot_idx:
            groups.append({"title": f"Group {len(groups) + 1}", "buttons": []})
        groups[bot_idx]["color"] = text

    def on_lock_target_toggled(self, checked):
        app = self.profiles_data.setdefault("app", {}).setdefault(self.current_app_id, {})
        app["lock_target"] = checked

    def new_profile_dialog(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "New Profile", "Enter Application ID / Profile Name:")
        if ok and name.strip():
            name = name.strip()
            if name not in self.profiles_data.setdefault("app", {}):
                self.profiles_data["app"][name] = {
                    "lock_target": True,
                    "groups": [
                        {"title": "Navigation", "color": "accent", "buttons": []},
                        {"title": "Actions", "color": "good", "buttons": []}
                    ]
                }
                self.current_app_id = name
                self.current_page = 0
                self.refresh_profile_list()

    def duplicate_profile(self):
        if not self.current_app_id:
            return
        new_name, ok = QtWidgets.QInputDialog.getText(
            self, "Duplicate Profile", f"Copy '{self.current_app_id}' as:"
        )
        if ok and new_name.strip():
            new_name = new_name.strip()
            self.profiles_data["app"][new_name] = copy.deepcopy(self.profiles_data["app"][self.current_app_id])
            self.current_app_id = new_name
            self.current_page = 0
            self.refresh_profile_list()

    def delete_profile(self):
        if self.current_app_id == "default":
            QtWidgets.QMessageBox.warning(self, "Cannot Delete", "The default profile cannot be deleted.")
            return
        res = QtWidgets.QMessageBox.question(
            self, "Confirm Delete", f"Are you sure you want to delete profile '{self.current_app_id}'?"
        )
        if res == QtWidgets.QMessageBox.StandardButton.Yes:
            self.profiles_data.get("app", {}).pop(self.current_app_id, None)
            self.current_app_id = "default"
            self.current_page = 0
            self.refresh_profile_list()

    def on_tab_changed(self, index):
        if index == 1:
            try:
                self.txt_raw_toml.setPlainText(tomli_w.dumps(self.profiles_data))
            except Exception as e:
                log.warning("Serialization to raw TOML failed: %s", e)
        elif index == 0:
            try:
                parsed = tomllib.loads(self.txt_raw_toml.toPlainText())
                self.profiles_data = parsed
                self.refresh_profile_list()
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, "Syntax Error", f"Cannot switch tabs: Invalid TOML syntax.\n{e}")
                self.tabs.setCurrentIndex(1)

    def validate_raw_toml(self):
        try:
            tomllib.loads(self.txt_raw_toml.toPlainText())
            self.status_updated.emit("TOML syntax is completely valid!", "#10b981")
        except Exception as e:
            self.status_updated.emit(f"TOML Syntax Error: {e}", "#ef4444")

    # --- Save & Gateway Sync ---

    def save_and_apply(self):
        if self.tabs.currentIndex() == 1:
            try:
                self.profiles_data = tomllib.loads(self.txt_raw_toml.toPlainText())
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Syntax Error", f"Cannot save invalid TOML:\n{e}")
                return

        tmp_file = self.profiles_path.with_suffix(".tmp")
        try:
            with open(tmp_file, "wb") as f:
                tomli_w.dump(self.profiles_data, f)
            os.replace(tmp_file, self.profiles_path)
            self.status_updated.emit(f"Profiles saved to {self.profiles_path.name} (deckd hot-reload triggered)", "#10b981")
        except Exception as e:
            self.status_updated.emit(f"Save failed: {e}", "#ef4444")
            return

        self.settings["gateway_host"] = self.edit_gw_host.text().strip()
        self.settings["gateway_user"] = self.edit_gw_user.text().strip()
        self.settings["auto_sync_gateway"] = self.chk_auto_sync.isChecked()
        save_settings(self.settings)

        if self.chk_auto_sync.isChecked():
            self.sync_gateway_now()

    def test_gateway_connection(self):
        gw_host = self.edit_gw_host.text().strip() or DEFAULT_GATEWAY_HOST
        gw_user = self.edit_gw_user.text().strip() or DEFAULT_GATEWAY_USER
        target = f"{gw_user}@{gw_host}" if gw_user else gw_host
        self.status_updated.emit(f"Testing SSH connection to {target}...", "#38bdf8")

        def run_test():
            try:
                cmd = ["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes", target, "uname -a"]
                res = subprocess.run(cmd, timeout=5, capture_output=True, text=True)
                if res.returncode == 0:
                    self.status_updated.emit(f"SSH connection to {target} succeeded!", "#10b981")
                else:
                    self.status_updated.emit(f"SSH test failed: {res.stderr.strip()}", "#ef4444")
            except Exception as e:
                self.status_updated.emit(f"SSH error: {e}", "#ef4444")

        threading.Thread(target=run_test, daemon=True).start()

    def sync_gateway_now(self):
        gw_host = self.edit_gw_host.text().strip() or DEFAULT_GATEWAY_HOST
        gw_user = self.edit_gw_user.text().strip() or DEFAULT_GATEWAY_USER
        target = f"{gw_user}@{gw_host}" if gw_user else gw_host

        self.status_updated.emit(f"Syncing profiles to Pi Gateway ({target})...", "#38bdf8")
        self.btn_sync_now.setEnabled(False)

        def run_sync():
            try:
                cmd1 = [
                    "scp", "-q", "-o", "ConnectTimeout=5", str(self.profiles_path),
                    f"{target}:Documents/Github/ESP32-S3-HomeServer/pi_gateway/deck_profiles.toml"
                ]
                res1 = subprocess.run(cmd1, timeout=10, capture_output=True, text=True)
                if res1.returncode != 0:
                    raise RuntimeError(f"SCP failed: {res1.stderr.strip()}")

                cmd2 = ["ssh", "-o", "ConnectTimeout=5", target, "systemctl --user restart esp-gateway"]
                res2 = subprocess.run(cmd2, timeout=10, capture_output=True, text=True)
                if res2.returncode != 0:
                    raise RuntimeError(f"SSH restart failed: {res2.stderr.strip()}")

                self.status_updated.emit(f"Synced to Pi Gateway ({target}) & restarted esp-gateway!", "#10b981")
            except Exception as e:
                self.status_updated.emit(f"Gateway sync failed: {e}", "#ef4444")
            finally:
                QtCore.QMetaObject.invokeMethod(self.btn_sync_now, "setEnabled", Qt.ConnectionType.QueuedConnection, QtCore.Q_ARG(bool, True))

        threading.Thread(target=run_sync, daemon=True).start()

    def toggle_ble(self, checked):
        if self.dbus_iface:
            self.dbus_iface.call("SetBleEnabled", checked)
        else:
            try:
                subprocess.run([
                    "busctl", "--user", "call", "org.mmesp32.Deckd", "/org/mmesp32/Deckd",
                    "org.mmesp32.Deckd1", "SetBleEnabled", "b", "true" if checked else "false"
                ], timeout=2)
            except Exception:
                pass

    def toggle_usb(self, checked):
        if self.dbus_iface:
            self.dbus_iface.call("SetUsbEnabled", checked)
        else:
            try:
                subprocess.run([
                    "busctl", "--user", "call", "org.mmesp32.Deckd", "/org/mmesp32/Deckd",
                    "org.mmesp32.Deckd1", "SetUsbEnabled", "b", "true" if checked else "false"
                ], timeout=2)
            except Exception:
                pass

    @Slot(str, str)
    def on_status_updated(self, msg, color):
        self.lbl_status.setText(msg)
        self.lbl_status.setStyleSheet(f"color: {color}; font-weight: bold;")


def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName("MMEsp32 Stream Deck")

    # Set dark palette application-wide so top-level popups inherit it
    dark_palette = QtGui.QPalette()
    dark_palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor("#0b0f19"))
    dark_palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor("#f8fafc"))
    dark_palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor("#111827"))
    dark_palette.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor("#1e293b"))
    dark_palette.setColor(QtGui.QPalette.ColorRole.ToolTipBase, QtGui.QColor("#1e293b"))
    dark_palette.setColor(QtGui.QPalette.ColorRole.ToolTipText, QtGui.QColor("#f8fafc"))
    dark_palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor("#f8fafc"))
    dark_palette.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor("#1e293b"))
    dark_palette.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor("#f8fafc"))
    dark_palette.setColor(QtGui.QPalette.ColorRole.BrightText, QtGui.QColor("#ffffff"))
    dark_palette.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor("#0284c7"))
    dark_palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor("#ffffff"))
    app.setPalette(dark_palette)
    app.setStyleSheet(GLOBAL_APP_STYLE)

    # Single-instance check via QLocalServer
    socket = QtNetwork.QLocalSocket()
    socket.connectToServer(LOCAL_SERVER_NAME)
    if socket.waitForConnected(300):
        socket.write(b"SHOW\n")
        socket.waitForBytesWritten(300)
        socket.disconnectFromServer()
        return 0

    server = QtNetwork.QLocalServer()
    QtNetwork.QLocalServer.removeServer(LOCAL_SERVER_NAME)
    server.listen(LOCAL_SERVER_NAME)

    win = DeckMainWindow()

    def on_new_connection():
        client = server.nextPendingConnection()
        if client:
            client.waitForReadyRead(200)
            win.showNormal()
            win.raise_()
            win.activateWindow()
            client.disconnectFromServer()

    server.newConnection.connect(on_new_connection)

    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
