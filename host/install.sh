#!/usr/bin/env bash
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
# Idempotent installer for the deckd user service.
# Renders and reloads the systemd unit but never enables or starts it;
# the final enable/start and the udev rule are printed for you to run.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/deckd.service"
EXT_UUID="deckfocus@mmesp32"
if [ -d "$HERE/gnome-extension/$EXT_UUID" ]; then
    EXT_SRC="$HERE/gnome-extension/$EXT_UUID"
else
    EXT_SRC="$REPO_ROOT/tools/gnome-extension/$EXT_UUID"
fi
EXT_DST="$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"

echo "==> deckd install dir: $HERE"

# 1. Virtualenv + dependencies (idempotent).
if [ ! -x "$HERE/.venv/bin/python" ]; then
    echo "==> creating virtualenv at $HERE/.venv"
    python3 -m venv "$HERE/.venv"
else
    echo "==> virtualenv already present"
fi
echo "==> installing/refreshing requirements"
"$HERE/.venv/bin/pip" install --quiet --upgrade pip
"$HERE/.venv/bin/pip" install --quiet -r "$HERE/requirements.txt"

# 2. Render the systemd user unit with the real install path.
echo "==> rendering $UNIT_PATH"
mkdir -p "$UNIT_DIR"
sed "s|@INSTALL_DIR@|$HERE|g" "$HERE/deckd.service.in" > "$UNIT_PATH"
systemctl --user daemon-reload

# 2b. Render the tray and GUI desktop launchers.
APPS_DIR="$HOME/.local/share/applications"
TRAY_DESKTOP="$APPS_DIR/decktray.desktop"
GUI_DESKTOP="$APPS_DIR/deckgui.desktop"
echo "==> rendering $TRAY_DESKTOP and $GUI_DESKTOP"
mkdir -p "$APPS_DIR"
sed "s|@INSTALL_DIR@|$HERE|g" "$HERE/decktray.desktop.in" > "$TRAY_DESKTOP"
sed "s|@INSTALL_DIR@|$HERE|g" "$HERE/deckgui.desktop.in" > "$GUI_DESKTOP"

# 3. Validate the rendered unit if systemd-analyze is available.
if command -v systemd-analyze >/dev/null 2>&1; then
    echo "==> verifying unit"
    systemd-analyze --user verify "$UNIT_PATH" \
        || echo "warning: systemd-analyze reported issues (see above)"
else
    echo "==> systemd-analyze not found; skipping unit verification"
fi

cat <<EOF

deckd is installed (unit rendered, NOT enabled or started).

Next steps — run these yourself when ready:

  # 1. Enable and start the daemon as a user service:
  systemctl --user enable --now deckd.service

  # 2. Grant /dev/uinput access for 'keys' actions (one-time, needs sudo):
  sudo install -m 0644 "$HERE/99-deckd-uinput.rules" /etc/udev/rules.d/99-deckd-uinput.rules
  sudo udevadm control --reload-rules
  sudo udevadm trigger --name-match=uinput
  sudo usermod -aG input "\$USER"    # then log out and back in

  # 3. Install the GNOME focus extension (then log out/in to load it):
  mkdir -p "$(dirname "$EXT_DST")"
  cp -r "$EXT_SRC" "$EXT_DST"
  gnome-extensions enable "$EXT_UUID"

  # 4. Start the tray applet now, and/or enable it at login:
  "$HERE/.venv/bin/python" "$HERE/decktray.py" &
  cp "$TRAY_DESKTOP" "\${XDG_CONFIG_HOME:-\$HOME/.config}/autostart/decktray.desktop"
EOF
