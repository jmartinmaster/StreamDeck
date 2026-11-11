# MMEsp32 Deck for Ubuntu 26.04 amd64

## Install

Run on each Ubuntu desktop:

```bash
sudo apt install ./mmesp32-deckd_1.2.1_amd64.deb
sudo usermod -aG input,dialout "$USER"
```

APT installs Python dependencies from Ubuntu, not pip. Enable the Ubuntu
Universe repository if APT cannot locate a dependency. Bluetooth requires an
enabled BLE-capable adapter. The tray requires AppIndicator support (included
on standard Ubuntu Desktop); the optional focus extension supports GNOME 50.
The default Terminal button runs Ubuntu's `x-terminal-emulator` alternative
(for example Ptyxis), not a hard-coded GNOME Terminal executable. Files
(Nautilus) is recommended for the default Files button.

**Log out and back in** to apply group membership and load the focus extension.
Then run these as your desktop user, **without sudo**:

```bash
deckd-user enable
gnome-extensions enable deckfocus@mmesp32
deckd-user status
```

The package does not start a system/root daemon or enable services for every
user. Each user opts in; the daemon and tray then start with their graphical
session. Running multiple users' daemons simultaneously may contend for
Bluetooth, TCP port 4650, and USB.

Membership in `input` permits access to input devices and virtual key injection.
Only grant it to trusted users. The packaged udev rule grants group access to
`/dev/uinput`; `dialout` grants USB serial access.

## Profiles and connections

Profiles are copied on first enable to
`~/.config/mmesp32-deckd/profiles.toml` (or `$XDG_CONFIG_HOME/mmesp32-deckd/`).
The tray's **Edit profiles** opens that file, which is hot-reloaded. Upgrades
leave user profiles unchanged; new defaults are in
`/usr/share/mmesp32-deckd/profiles.toml`.

With matching S3/P4 firmware, the **Control profile** picker on the device
pins the chosen profile using the same override as the tray. Choose **Follow
focus** to restore automatic switching. The selected profile stays pinned
across device reconnects until cleared or the daemon restarts. Profile selection
changes shortcuts, not the focused window; `lock_target = true` on a profile
raises its application before key shortcuts, just as when pinning from the tray.

When upgrading from 1.0.0, existing user profiles are preserved. If your default
Terminal button still uses `shell = "gnome-terminal"`, change it to
`shell = "x-terminal-emulator"` through **Edit profiles**.
Version 1.2.0 adds an `org.gnome.Ptyxis` profile. On upgrades, copy that section
from `/usr/share/mmesp32-deckd/profiles.toml` into your user profiles if desired;
the installer does not overwrite your custom shortcuts.

## Multiple computers (daemon 1.2.0 and matching panel firmware)

Each daemon advertises as `DeckD-<hostname>` by default. On either panel, open
Deck and tap **Scan** beside **Host**. Scanning disconnects the current host and
lists up to eight nearby Deck computers for five seconds. Choose a computer
(its BLE address suffix distinguishes duplicate names), or select
**Automatic (first available)** and tap **Connect**.

An explicit computer selection never falls back to another computer; if the host
goes away, the panel searches for that same BLE address. P4 TCP fallback is
disabled while a specific BLE host is selected or the picker is waiting for a
choice. Selection survives closing/reopening Deck, but resets on panel reboot.
BLE address changes require a fresh scan. This is BLE discovery, not LAN/mDNS
host selection. Older daemons may appear as `DeckD`; upgrade for host identity.
The Host list refreshes even if opened during scanning. Scan completion reports
the number found; choosing a listed host starts connecting. Scanning itself
does not reconnect automatically.

On older Bluetooth adapters, advertising may fail while an existing BLE link is
still connected. Close Deck on both panels before restarting/upgrading the
daemon, then reopen them. Version 1.2.1 reports startup failures as BLE disabled
instead of incorrectly claiming the server is active. After resolving the adapter
error, enable BLE again using the tray. Check `journalctl --user -u deckd` for
the actual registration result.

For this release, use one panel per computer: for example S3 on the desktop and
P4 on the laptop. Connecting both panels to one daemon is not supported.
Check the computer name shown below the picker before triggering shortcuts.
The panel shows action acknowledgments/errors and stops waiting after five
seconds without retrying (an unacknowledged action might already have run).
An acknowledgment means the daemon dispatched the action, not that a launched
application finished successfully.

Use the tray's USB/BLE checkboxes or:

```bash
deckd-user release-usb
deckd-user resume-usb
```

Releasing USB keeps BLE/TCP running. Check `fuser /dev/ttyUSB0` before flashing.
USB resumes within 10 seconds; the toggle resets to enabled on daemon restart.
To choose a different port, create
`~/.config/mmesp32-deckd/environment`:

```ini
DECKD_PORT=/dev/serial/by-id/your-device
```

Set `DECKD_PORT=none` to disable USB polling entirely. Restart with
`systemctl --user restart deckd`. Both services also accept `DECKD_PROFILES`
in that environment file for a custom profile path.

The package opens no firewall ports and changes no Bluetooth authorization
policy. TCP listens on port 4650 and advertises via mDNS. Only use on trusted
networks: the protocol is not authenticated, and connected devices can trigger
configured commands. BLE uses Nordic UART Service without application-level
authentication. With older panel firmware or **Automatic** selection, turn off
BLE on unintended hosts to avoid connecting to the first one found.

## Existing source installations

Do not run the source installer and package services concurrently. Before
switching, stop the old daemon/tray, back up your profiles, and move conflicting
`~/.config/systemd/user/deckd.service` / `decktray.service` files aside.
Also remove any old Deck tray entry in `~/.config/autostart/`. A per-user copy of
`deckfocus@mmesp32` shadows the packaged extension: back it up and move it outside
`~/.local/share/gnome-shell/extensions/`, then log out/in.

`deckd-user enable` refuses conflicting user unit files rather than overwriting
them. Copy your old profile settings into the new per-user profiles file.

## Stop, upgrade, and uninstall

```bash
deckd-user disable
sudo apt install ./mmesp32-deckd_NEWVERSION_amd64.deb
systemctl --user daemon-reload
deckd-user enable
```

To uninstall, first run `deckd-user disable` in each enabled user's session,
disable the extension with `gnome-extensions disable deckfocus@mmesp32`, then
`sudo apt remove mmesp32-deckd`. User profiles and group membership are retained.
Log out/in after extension removal. Remove group membership separately if no
longer needed.

Logs: `journalctl --user -u deckd -u decktray`.

## Build

From the source checkout (no root, pip, or network access required):

```bash
bash host/packaging/build-deb.sh 1.2.1 /path/to/output
```

The package contains only the daemon, tray, default profiles, GNOME extension,
launchers, service units, udev/module configuration and this guide. It does not
include your virtualenv, firmware, credentials, or personal configuration.
