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
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$(cd "$HERE/.." && pwd)"
VERSION="${1:-1.2.1}"
OUTPUT="${2:-$PWD}"
if [[ ! "$VERSION" =~ ^[0-9]+(\.[0-9]+)*$ ]]; then
    echo "Version must be numeric, for example 1.0.0" >&2
    exit 2
fi
mkdir -p "$OUTPUT"
OUTPUT="$(cd "$OUTPUT" && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf -- "$STAGE"' EXIT

install -d "$STAGE/DEBIAN" "$STAGE/usr/lib/mmesp32-deckd" \
    "$STAGE/usr/share/mmesp32-deckd" "$STAGE/usr/share/doc/mmesp32-deckd"
for module in deckd.py deckcontrol.py decktray.py ble_deck.py; do
    install -m 644 "$SOURCE/$module" "$STAGE/usr/lib/mmesp32-deckd/"
done
install -m 644 "$SOURCE/profiles.toml" "$STAGE/usr/share/mmesp32-deckd/"
for command in deckd decktray deckd-user; do
    install -D -m 755 "$HERE/$command" "$STAGE/usr/bin/$command"
done
for unit in deckd.service decktray.service; do
    install -D -m 644 "$HERE/$unit" "$STAGE/usr/lib/systemd/user/$unit"
done
install -D -m 644 "$HERE/decktray.desktop" \
    "$STAGE/usr/share/applications/mmesp32-decktray.desktop"
install -D -m 644 "$SOURCE/99-deckd-uinput.rules" \
    "$STAGE/usr/lib/udev/rules.d/99-deckd-uinput.rules"
install -D -m 644 "$HERE/modules-load.conf" \
    "$STAGE/usr/lib/modules-load.d/mmesp32-deckd.conf"
EXTENSION="$STAGE/usr/share/gnome-shell/extensions/deckfocus@mmesp32"
install -d "$EXTENSION"
install -m 644 "$SOURCE/../gnome-extension/deckfocus@mmesp32/"{extension.js,metadata.json} "$EXTENSION/"
install -m 644 "$HERE/README.md" "$STAGE/usr/share/doc/mmesp32-deckd/README.md"
install -m 755 "$HERE/postinst" "$STAGE/DEBIAN/postinst"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: mmesp32-deckd
Version: $VERSION
Architecture: amd64
Maintainer: MMEsp32 contributors <noreply@localhost>
Section: utils
Priority: optional
Depends: python3 (>= 3.11), python3-serial, python3-evdev, python3-dbus-next, python3-zeroconf, bluez, systemd, udev, kmod, xdg-utils, x-terminal-emulator
Recommends: gnome-shell (>= 50), gnome-shell (<< 51), gnome-shell-ubuntu-extensions, nautilus
Installed-Size: $(du -sk "$STAGE/usr" | cut -f1)
Description: MMEsp32 shortcut deck daemon and desktop tray
 Context-aware shortcut layouts over USB serial, BLE and TCP, with
 per-user services, editable profiles and a GNOME Shell focus extension.
 Built for Ubuntu 26.04 amd64.
EOF
dpkg-deb --root-owner-group --build "$STAGE" "$OUTPUT/mmesp32-deckd_${VERSION}_amd64.deb"
