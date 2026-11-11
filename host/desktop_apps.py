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
"""Portable Linux XDG desktop application discovery and launcher helpers."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


@dataclass(frozen=True)
class DesktopApp:
    """Metadata parsed from a freedesktop .desktop application entry."""

    desktop_id: str
    name: str
    exec_cmd: str
    comment: str = ""
    icon_name: str = ""
    categories: tuple[str, ...] = ()
    path: Path | None = None
    terminal: bool = False


def default_application_dirs() -> list[Path]:
    """Return Linux XDG + Flatpak + Snap application directories in priority order."""
    raw_dirs: list[Path] = []
    xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    home_share = Path(xdg_data_home) if xdg_data_home else (Path.home() / ".local" / "share")
    raw_dirs.append(home_share / "applications")
    raw_dirs.append(Path.home() / ".local" / "share" / "flatpak" / "exports" / "share" / "applications")

    xdg_data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    for base in xdg_data_dirs.split(":"):
        base = base.strip()
        if base:
            raw_dirs.append(Path(base) / "applications")

    for extra in (
        Path("/usr/local/share/applications"),
        Path("/usr/share/applications"),
        Path("/var/lib/snapd/desktop/applications"),
        Path("/var/lib/flatpak/exports/share/applications"),
    ):
        raw_dirs.append(extra)

    deduped: list[Path] = []
    seen: set[Path] = set()
    for d in raw_dirs:
        if d not in seen:
            seen.add(d)
            deduped.append(d)
    return deduped


def _parse_desktop_entry(path: Path) -> dict[str, str]:
    """Parse only the [Desktop Entry] section without configparser interpolation bugs."""
    in_entry = False
    fields: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return fields
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            if in_entry:
                break
            in_entry = (line == "[Desktop Entry]")
            continue
        if not in_entry or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in fields:
            fields[key] = value.strip()
    return fields


def _desktop_matches_env(fields: dict[str, str], current_desktop: str) -> bool:
    desktops = {d.strip().lower() for d in current_desktop.split(":") if d.strip()}
    only_show = {d.strip().lower() for d in fields.get("OnlyShowIn", "").split(";") if d.strip()}
    not_show = {d.strip().lower() for d in fields.get("NotShowIn", "").split(";") if d.strip()}
    if only_show and desktops and not (only_show & desktops):
        return False
    if not_show and desktops and (not_show & desktops):
        return False
    return True


def _is_true(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes")


def _try_exec_available(fields: dict[str, str]) -> bool:
    try_exec = fields.get("TryExec", "").strip()
    if not try_exec:
        return True
    p = Path(try_exec)
    if p.is_absolute():
        return os.access(p, os.X_OK)
    return shutil.which(try_exec) is not None


def _iter_desktop_files(app_dir: Path) -> Iterable[tuple[str, Path]]:
    """Yield (desktop_id, path) inside app_dir per XDG nested-subdir id rules."""
    if not app_dir.is_dir():
        return
    try:
        entries = sorted(app_dir.rglob("*.desktop"))
    except OSError:
        return
    for item in entries:
        if not item.is_file():
            continue
        try:
            rel = item.relative_to(app_dir)
        except ValueError:
            continue
        desktop_id = "-".join(rel.parts)
        yield desktop_id, item


def discover_desktop_apps(
    app_dirs: Iterable[Path] | None = None,
    current_desktop: str | None = None,
    check_try_exec: bool = True,
) -> list[DesktopApp]:
    """Discover launchable Linux desktop applications installed on the host."""
    dirs = list(app_dirs) if app_dirs is not None else default_application_dirs()
    if current_desktop is None:
        current_desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")

    apps_by_id: dict[str, DesktopApp] = {}
    seen_ids: set[str] = set()

    for app_dir in dirs:
        for desktop_id, path in _iter_desktop_files(app_dir):
            if desktop_id in seen_ids:
                continue
            fields = _parse_desktop_entry(path)
            if not fields:
                seen_ids.add(desktop_id)
                continue
            if fields.get("Type", "") != "Application":
                seen_ids.add(desktop_id)
                continue
            if _is_true(fields.get("Hidden", "")) or _is_true(fields.get("NoDisplay", "")):
                seen_ids.add(desktop_id)
                continue
            if current_desktop and not _desktop_matches_env(fields, current_desktop):
                seen_ids.add(desktop_id)
                continue
            if check_try_exec and not _try_exec_available(fields):
                seen_ids.add(desktop_id)
                continue
            name = fields.get("Name", "").strip()
            exec_cmd = fields.get("Exec", "").strip()
            if not name or not exec_cmd:
                seen_ids.add(desktop_id)
                continue
            seen_ids.add(desktop_id)
            cats = tuple(c.strip() for c in fields.get("Categories", "").split(";") if c.strip())
            apps_by_id[desktop_id] = DesktopApp(
                desktop_id=desktop_id,
                name=name,
                exec_cmd=exec_cmd,
                comment=fields.get("Comment", "").strip(),
                icon_name=fields.get("Icon", "").strip(),
                categories=cats,
                path=path,
                terminal=_is_true(fields.get("Terminal", "")),
            )

    return sorted(apps_by_id.values(), key=lambda a: (a.name.lower(), a.desktop_id))


def normalize_desktop_id(desktop_id: str | None) -> str:
    if not desktop_id:
        return ""
    cleaned = str(desktop_id).strip()
    if cleaned and not cleaned.endswith(".desktop"):
        cleaned += ".desktop"
    return cleaned


def short_label(name: str, limit: int = 13) -> str:
    """Compact an application name to fit a Stream Deck key label."""
    text = " ".join(name.split())
    if len(text) <= limit:
        return text
    for sep in (" - ", ": ", " ("):
        if sep in text:
            head = text.split(sep, 1)[0].strip()
            if head and len(head) <= limit:
                return head
    return text[:limit].rstrip()


def guess_deck_icon(app: DesktopApp) -> str:
    """Choose a Stream Deck icon token from XDG categories, icon name, and id."""
    cats = {c.lower() for c in app.categories}
    hay = f"{app.desktop_id} {app.icon_name} {app.name}".lower()

    if "terminalemulator" in cats or any(k in hay for k in ("terminal", "konsole", "alacritty", "kitty", "wezterm", "tilix")):
        return "terminal"
    if "filemanager" in cats or any(k in hay for k in ("nautilus", "dolphin", "thunar", "nemo", "files")):
        return "folder"
    if "webbrowser" in cats or any(k in hay for k in ("firefox", "chromium", "chrome", "edge", "brave", "vivaldi", "epiphany", "browser")):
        return "globe"
    if "development" in cats or "ide" in cats or any(k in hay for k in ("code", "vscode", "codium", "jetbrains", "pycharm", "eclipse", "kate", "neovim", "vim")):
        return "code"
    if "audiovideo" in cats or "audio" in cats or "music" in cats or any(k in hay for k in ("spotify", "vlc", "mpv", "rhythmbox", "audacity", "music")):
        return "music"
    if "graphics" in cats or "photography" in cats or any(k in hay for k in ("gimp", "inkscape", "krita", "blender", "screenshot", "camera", "obs")):
        return "camera"
    if "system" in cats or "monitor" in cats or any(k in hay for k in ("systemmonitor", "mission", "btop", "htop", "monitor")):
        return "activity"
    if "settings" in cats or "preferences" in cats or "settings" in hay:
        return "settings"
    if "email" in cats or any(k in hay for k in ("thunderbird", "geary", "mail")):
        return "mail"
    if "office" in cats or "texteditor" in cats:
        return "file"
    if "game" in cats or "steam" in hay:
        return "play"
    return "app"


def _default_xdg_mime_query(mime_type: str) -> str:
    if not shutil.which("xdg-mime"):
        return ""
    try:
        res = subprocess.run(
            ["xdg-mime", "query", "default", mime_type],
            timeout=2,
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def suggest_smart_defaults(
    apps: list[DesktopApp],
    xdg_mime_query: Callable[[str], str] | None = None,
    limit: int = 12,
) -> list[dict[str, str]]:
    """Build portable out-of-the-box launcher entries from apps on this host."""
    if not apps:
        return []
    query_mime = xdg_mime_query or _default_xdg_mime_query
    by_id = {app.desktop_id: app for app in apps}
    picked: list[DesktopApp] = []
    seen: set[str] = set()

    def pick(app: DesktopApp | None) -> None:
        if app is None or app.desktop_id in seen or len(picked) >= limit:
            return
        seen.add(app.desktop_id)
        picked.append(app)

    # 1. User's real default browser and file manager via xdg-mime.
    for mime in ("x-scheme-handler/https", "x-scheme-handler/http", "inode/directory"):
        desktop_id = normalize_desktop_id(query_mime(mime))
        if desktop_id in by_id:
            pick(by_id[desktop_id])

    # 2. Match well-known Linux desktop roles against installed entries.
    role_matchers: list[Callable[[DesktopApp], bool]] = [
        lambda a: "terminalemulator" in {c.lower() for c in a.categories}
        or any(k in a.desktop_id.lower() for k in ("terminal", "konsole", "alacritty", "kitty", "wezterm")),
        lambda a: "filemanager" in {c.lower() for c in a.categories},
        lambda a: "webbrowser" in {c.lower() for c in a.categories},
        lambda a: any(k in a.desktop_id.lower() for k in ("code", "codium", "texteditor", "kate", "gedit"))
        or "ide" in {c.lower() for c in a.categories},
        lambda a: any(k in f"{a.desktop_id} {a.name}".lower() for k in ("systemmonitor", "missioncenter", "mission-center", "resources", "htop")),
        lambda a: "settings" in {c.lower() for c in a.categories} or "control-center" in a.desktop_id.lower(),
        lambda a: any(k in f"{a.desktop_id} {a.name}".lower() for k in ("calculator", "screenshot", "flameshot")),
        lambda a: "audiovideo" in {c.lower() for c in a.categories} or "music" in {c.lower() for c in a.categories},
    ]
    for matcher in role_matchers:
        for app in apps:
            if app.desktop_id not in seen and matcher(app):
                pick(app)
                break

    color_by_icon = {
        "terminal": "warn",
        "folder": "accent",
        "globe": "accent",
        "code": "accent",
        "activity": "good",
        "music": "good",
        "camera": "good",
        "settings": "idle",
    }

    out: list[dict[str, str]] = []
    for app in picked[:limit]:
        icon = guess_deck_icon(app)
        out.append({
            "id": app.desktop_id,
            "label": short_label(app.name),
            "icon": icon,
            "color": color_by_icon.get(icon, "accent"),
        })
    return out


def build_exec_argv(app: DesktopApp) -> list[str]:
    """Parse a .desktop Exec= command line with freedesktop field codes stripped."""
    tokens = shlex.split(app.exec_cmd)
    argv: list[str] = []
    skip_next_if_i = False
    for tok in tokens:
        if skip_next_if_i:
            skip_next_if_i = False
            continue
        if tok == "%i":
            if app.icon_name:
                argv.extend(["--icon", app.icon_name])
            continue
        if tok == "%c":
            argv.append(app.name)
            continue
        if tok == "%k":
            if app.path is not None:
                argv.append(str(app.path))
            continue
        if tok in ("%f", "%F", "%u", "%U", "%d", "%D", "%n", "%N", "%v", "%m", "@@", "@@u"):
            continue
        # Strip inline field codes inside an argument token.
        for code in ("%f", "%F", "%u", "%U", "%i", "%c", "%k", "%d", "%D", "%n", "%N", "%v", "%m"):
            tok = tok.replace(code, "")
        if tok:
            argv.append(tok)

    if not argv:
        raise ValueError(f"empty Exec command for {app.desktop_id}")

    if app.terminal:
        for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal", "xterm"):
            if shutil.which(term):
                flag = "--" if term == "gnome-terminal" else "-e"
                return [term, flag, *argv]
    return argv


def launch_desktop_app(
    desktop_id: str,
    apps_by_id: dict[str, DesktopApp] | None = None,
) -> subprocess.Popen:
    """Launch an installed Linux desktop app by its .desktop ID."""
    target_id = normalize_desktop_id(desktop_id)
    if not target_id:
        raise ValueError("empty desktop app id")

    if apps_by_id is None:
        apps_by_id = {a.desktop_id: a for a in discover_desktop_apps(check_try_exec=False)}

    app = apps_by_id.get(target_id)
    if app is None and target_id.endswith(".desktop"):
        # Fallback: case-insensitive stem match.
        stem = target_id[:-8].lower()
        for candidate in apps_by_id.values():
            if candidate.desktop_id[:-8].lower() == stem:
                app = candidate
                target_id = candidate.desktop_id
                break

    if app is not None and app.path is not None and shutil.which("gio"):
        return subprocess.Popen(
            ["gio", "launch", str(app.path)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if shutil.which("gtk-launch"):
        return subprocess.Popen(
            ["gtk-launch", target_id],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if app is None:
        raise FileNotFoundError(f"desktop application not found: {target_id}")

    argv = build_exec_argv(app)
    return subprocess.Popen(
        argv,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
