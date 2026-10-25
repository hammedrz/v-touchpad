// V-Touchpad helper: keep the cursor visible and focused while the pad is open.
//
// GNOME hides the cursor and takes pointer focus away on every touchscreen
// event, and gives both back on the next pointer event. V-Touchpad itself can
// only answer each touch after it arrives, so a finger moving on the pad made
// the cursor blink, and the app under it lose and regain hover, about 120
// times a second. From inside GNOME Shell the cursor comes back right away.

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const PAD_WM_CLASS = 'v-touchpad';

export default class VTouchpadExtension extends Extension {
    enable() {
        this._seat = global.stage.get_context().get_backend().get_default_seat();
        this._tracker = global.backend.get_cursor_tracker();
        // Its events count as pointer use, which is what makes GNOME show the cursor.
        this._pointer = this._seat.create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
        this._windows = new Map(); // MetaWindow -> signal handler ids
        this._active = false;

        this._visibilityId = this._tracker.connect('visibility-changed', () => this._showCursor());
        this._windowCreatedId = global.display.connect('window-created',
            (_display, window) => this._track(window));
        for (const window of global.display.list_all_windows())
            this._track(window);
    }

    disable() {
        global.display.disconnect(this._windowCreatedId);
        this._tracker.disconnect(this._visibilityId);
        for (const [window, ids] of this._windows)
            ids.forEach(id => window.disconnect(id));
        this._windows.clear();
        this._setActive(false);
        this._pointer = null;
        this._tracker = null;
        this._seat = null;
    }

    _track(window) {
        const update = () => this._update();
        const ids = [
            // Wayland windows get their app ID only after they are created.
            window.connect('notify::wm-class', update),
            window.connect('notify::minimized', update),
            window.connect('unmanaged', () => {
                ids.forEach(id => window.disconnect(id));
                this._windows.delete(window);
                this._update();
            }),
        ];
        this._windows.set(window, ids);
        this._update();
    }

    _update() {
        // The pad itself, not its dialogs; hiding it to the tray unmanages it.
        this._setActive([...this._windows.keys()].some(window =>
            window.get_wm_class() === PAD_WM_CLASS &&
            window.get_window_type() === Meta.WindowType.NORMAL &&
            !window.minimized));
    }

    _setActive(active) {
        if (active === this._active)
            return;
        this._active = active;
        if (active) {
            // Keep pointer focus on the app under the cursor even while it is hidden.
            this._seat.inhibit_unfocus();
            this._showCursor();
        } else {
            this._seat.uninhibit_unfocus();
        }
    }

    _showCursor() {
        // A zero-length motion: the cursor reappears without moving.
        if (this._active && !this._tracker.get_pointer_visible())
            this._pointer.notify_relative_motion(GLib.get_monotonic_time(), 0, 0);
    }
}
