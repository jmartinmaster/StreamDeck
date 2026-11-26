# SPDX-License-Identifier: GPL-3.0-or-later
"""Windows Start Menu application discovery and launch helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    from desktop_apps import guess_deck_icon, short_label
except ImportError:  # pragma: no cover - package import fallback
    from .desktop_apps import guess_deck_icon, short_label


@dataclass(frozen=True)
class WindowsApp:
    """A launchable Windows Start Menu shortcut."""

    desktop_id: str
    name: str
    exec_cmd: str = ""
    comment: str = ""
    icon_name: str = ""
    categories: tuple[str, ...] = ()
    path: Path | None = None
    terminal: bool = False


def default_start_menu_dirs() -> list[tuple[str, Path]]:
    """Return user and all-users Start Menu program directories."""
    roots: list[tuple[str, Path]] = []
    appdata = os.environ.get("APPDATA")
    program_data = os.environ.get("PROGRAMDATA")
    if appdata:
        roots.append((
            "user",
            Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        ))
    if program_data:
        roots.append((
            "common",
            Path(program_data) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        ))
    return roots


def discover_windows_apps(
    menu_dirs: Iterable[tuple[str, Path]] | None = None,
) -> list[WindowsApp]:
    """Find Start Menu shortcuts, preserving IDs across user profiles."""
    apps: dict[str, WindowsApp] = {}
    for scope, root in menu_dirs if menu_dirs is not None else default_start_menu_dirs():
        if not root.is_dir():
            continue
        try:
            shortcuts = sorted(
                path for path in root.rglob("*")
                if path.is_file() and path.suffix.lower() in (".lnk", ".url")
            )
        except OSError:
            continue
        for path in shortcuts:
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if any(part.casefold() in {"startup", "uninstall"} for part in relative.parts):
                continue
            app_id = f"startmenu:{scope}:{relative.as_posix()}"
            name = path.stem.strip()
            if not name:
                continue
            apps.setdefault(
                app_id,
                WindowsApp(
                    desktop_id=app_id,
                    name=name,
                    icon_name=path.stem,
                    path=path,
                ),
            )
    return sorted(apps.values(), key=lambda app: (app.name.casefold(), app.desktop_id.casefold()))


def normalize_desktop_id(app_id: str | None) -> str:
    """Keep the shared profile app-ID helper name without Linux suffix rules."""
    return str(app_id or "").strip()


def suggest_smart_defaults(
    apps: list[WindowsApp],
    xdg_mime_query=None,
    limit: int = 12,
) -> list[dict[str, str]]:
    """Suggest one installed app for common Windows desktop roles."""
    del xdg_mime_query
    role_matchers = (
        ("terminal", ("terminal", "powershell", "command prompt", "cmd")),
        ("folder", ("file explorer", "explorer")),
        ("globe", ("edge", "chrome", "firefox", "brave", "browser")),
        ("code", ("visual studio code", "vscode", "visual studio")),
        ("music", ("spotify", "vlc", "music")),
        ("settings", ("settings",)),
    )
    picked: list[dict[str, str]] = []
    seen: set[str] = set()
    for icon, terms in role_matchers:
        for app in apps:
            haystack = f"{app.name} {app.desktop_id}".casefold()
            if app.desktop_id not in seen and any(term in haystack for term in terms):
                picked.append({
                    "id": app.desktop_id,
                    "label": short_label(app.name),
                    "icon": icon,
                    "color": "accent",
                })
                seen.add(app.desktop_id)
                break
    for app in apps:
        if len(picked) >= limit:
            break
        if app.desktop_id in seen:
            continue
        icon = guess_deck_icon(app)
        picked.append({
            "id": app.desktop_id,
            "label": short_label(app.name),
            "icon": icon,
            "color": "accent",
        })
        seen.add(app.desktop_id)
    return picked[:limit]


def launch_desktop_app(
    desktop_id: str,
    apps_by_id: dict[str, WindowsApp] | None = None,
) -> None:
    """Launch a discovered Start Menu shortcut through the Windows shell."""
    app_id = normalize_desktop_id(desktop_id)
    if not app_id:
        raise ValueError("empty Windows app id")
    if apps_by_id is None:
        apps_by_id = {app.desktop_id: app for app in discover_windows_apps()}
    app = apps_by_id.get(app_id)
    if app is None or app.path is None or not app.path.is_file():
        raise FileNotFoundError(f"Windows Start Menu app not found: {app_id}")
    if not hasattr(os, "startfile"):
        raise OSError("Windows shell launching is only available on Windows")
    os.startfile(str(app.path))
