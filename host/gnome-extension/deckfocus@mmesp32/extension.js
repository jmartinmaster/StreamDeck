/*
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * This file is part of Stream Deck.
 *
 * Stream Deck is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * Stream Deck is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with Stream Deck.  If not, see <https://www.gnu.org/licenses/>.
 */

// deckfocus@mmesp32 - minimal focus reporter for the MMEsp32 deckd daemon.
//
// Owns org.mmesp32.DeckFocus on the session bus and exports
// /org/mmesp32/DeckFocus with:
//   method GetFocus() -> (ss)      (app_id, title)
//   method ActivateApp(s) -> (b)   raise/focus an app by id; true on success
//   signal FocusChanged(ss)        (app_id, title)

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'org.mmesp32.DeckFocus';
const OBJECT_PATH = '/org/mmesp32/DeckFocus';

const DBUS_IFACE = `
<node>
  <interface name="org.mmesp32.DeckFocus">
    <method name="GetFocus">
      <arg type="s" direction="out" name="app_id"/>
      <arg type="s" direction="out" name="title"/>
    </method>
    <method name="ActivateApp">
      <arg type="s" direction="in" name="app_id"/>
      <arg type="b" direction="out" name="activated"/>
    </method>
    <signal name="FocusChanged">
      <arg type="s" name="app_id"/>
      <arg type="s" name="title"/>
    </signal>
  </interface>
</node>`;

export default class DeckFocusExtension extends Extension {
    enable() {
        this._appId = '';
        this._title = '';
        this._titleHandlerId = 0;
        this._titleWindow = null;

        this._dbus = Gio.DBusExportedObject.wrapJSObject(DBUS_IFACE, this);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);

        this._busId = Gio.DBus.session.own_name(
            BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            null,
            null);

        this._focusHandlerId = global.display.connect(
            'notify::focus-window',
            () => this._onFocusChanged());

        this._onFocusChanged();
    }

    disable() {
        if (this._focusHandlerId) {
            global.display.disconnect(this._focusHandlerId);
            this._focusHandlerId = 0;
        }
        this._untrackTitle();
        if (this._dbus) {
            this._dbus.unexport();
            this._dbus = null;
        }
        if (this._busId) {
            Gio.DBus.session.unown_name(this._busId);
            this._busId = 0;
        }
    }

    GetFocus() {
        return [this._appId, this._title];
    }

    ActivateApp(appId) {
        if (!appId)
            return false;

        // Prefer the Shell app (raises the most recent window, or launches).
        const appSystem = Shell.AppSystem.get_default();
        const app = appSystem.lookup_app(`${appId}.desktop`) ??
            appSystem.lookup_app(appId);
        if (app && app.get_n_windows() > 0) {
            app.activate();
            return true;
        }

        // Fall back to matching a window's tracked app id or wm_class
        // (covers apps without a resolvable .desktop entry).
        const tracker = Shell.WindowTracker.get_default();
        for (const actor of global.get_window_actors()) {
            const window = actor.meta_window;
            if (!window)
                continue;
            let windowAppId =
                tracker.get_window_app(window)?.get_id() ?? '';
            if (windowAppId.endsWith('.desktop'))
                windowAppId = windowAppId.slice(0, -'.desktop'.length);
            const wmClass = window.get_wm_class() ?? '';
            if (windowAppId === appId || wmClass === appId) {
                window.activate(global.get_current_time());
                return true;
            }
        }
        return false;
    }

    _untrackTitle() {
        if (this._titleWindow && this._titleHandlerId) {
            this._titleWindow.disconnect(this._titleHandlerId);
        }
        this._titleWindow = null;
        this._titleHandlerId = 0;
    }

    _onFocusChanged() {
        const window = global.display.focus_window;

        this._untrackTitle();
        if (window) {
            this._titleWindow = window;
            this._titleHandlerId = window.connect(
                'notify::title',
                () => this._update(window));
        }
        this._update(window);
    }

    _update(window) {
        let appId = '';
        let title = '';
        if (window) {
            const app = Shell.WindowTracker.get_default().get_window_app(window);
            appId = app?.get_id() ?? '';
            if (appId.endsWith('.desktop'))
                appId = appId.slice(0, -'.desktop'.length);
            if (!appId)
                appId = window.get_wm_class() ?? '';
            title = window.get_title() ?? '';
        }
        if (appId === this._appId && title === this._title)
            return;
        this._appId = appId;
        this._title = title;
        this._dbus?.emit_signal(
            'FocusChanged',
            new GLib.Variant('(ss)', [appId, title]));
    }
}
