// V-Touchpad helper: keep the cursor, menus and drags working while the pad is open.
//
// GNOME hides the cursor and takes pointer focus away on every touchscreen
// event, and gives both back on the next pointer event. V-Touchpad itself can
// only answer each touch after it arrives, so a finger moving on the pad made
// the cursor blink, and the app under it lose and regain hover, about 120
// times a second. From inside GNOME Shell the cursor comes back right away.
//
// While a menu is open or a drag is under way, an app holds an input grab, and
// GNOME treats the pad like any other window outside it: the pad gets no
// touches, lifting a finger from it closes the menu, and fingers already on
// the pad when the grab starts are cancelled, which ends the drag. So this
// helper takes every touch on the pad's contents itself and passes it to
// V-Touchpad over D-Bus, and the grab never sees it.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const PAD_WM_CLASS = 'v-touchpad';
// V-Touchpad owns this name on the session bus and listens for our Active and
// Touch signals. While Active, it ignores the touches its window receives.
const BRIDGE_NAME = 'io.github.hammedrz.VTouchpad';
const BRIDGE_PATH = '/io/github/hammedrz/VTouchpad';
const TOUCH_PHASES = new Map([
    [Clutter.EventType.TOUCH_BEGIN, 'begin'],
    [Clutter.EventType.TOUCH_UPDATE, 'update'],
    [Clutter.EventType.TOUCH_END, 'end'],
    [Clutter.EventType.TOUCH_CANCEL, 'cancel'],
]);
// Mutter's name for the grab a Wayland app holds for an open menu or a drag.
const WAYLAND_GRAB_ACTOR = 'input only grab actor';

export default class VTouchpadExtension extends Extension {
    enable() {
        this._seat = global.stage.get_context().get_backend().get_default_seat();
        this._tracker = global.backend.get_cursor_tracker();
        // Its events count as pointer use, which is what makes GNOME show the cursor.
        this._pointer = this._seat.create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
        this._windows = new Map(); // MetaWindow -> signal handler ids
        this._pad = null; // the pad's MetaWindow while it is open
        this._bridge = null; // unique bus name of the V-Touchpad that listens
        this._filterId = 0;
        // Touch slot passed to V-Touchpad -> the pad's contents rectangle when
        // the touch started, so positions stay steady while the pad is dragged.
        this._taken = new Map();

        this._watchId = Gio.bus_watch_name(Gio.BusType.SESSION, BRIDGE_NAME,
            Gio.BusNameWatcherFlags.NONE,
            (_connection, _name, owner) => {
                this._bridge = owner;
                this._updateFilter();
            },
            () => {
                this._bridge = null;
                this._updateFilter();
            });
        this._visibilityId = this._tracker.connect('visibility-changed', () => this._showCursor());
        this._windowCreatedId = global.display.connect('window-created',
            (_display, window) => this._track(window));
        for (const window of global.display.list_all_windows())
            this._track(window);
    }

    disable() {
        Gio.bus_unwatch_name(this._watchId);
        global.display.disconnect(this._windowCreatedId);
        this._tracker.disconnect(this._visibilityId);
        for (const [window, ids] of this._windows)
            ids.forEach(id => window.disconnect(id));
        this._windows.clear();
        this._setPad(null);
        this._bridge = null;
        this._updateFilter();
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
        this._setPad([...this._windows.keys()].find(window =>
            window.get_wm_class() === PAD_WM_CLASS &&
            window.get_window_type() === Meta.WindowType.NORMAL &&
            !window.minimized) ?? null);
    }

    _setPad(pad) {
        if (pad === this._pad)
            return;
        const wasActive = this._pad !== null;
        this._pad = pad;
        if (pad && !wasActive) {
            // Keep pointer focus on the app under the cursor even while it is hidden.
            this._seat.inhibit_unfocus();
            this._showCursor();
        } else if (!pad && wasActive) {
            this._seat.uninhibit_unfocus();
        }
        this._updateFilter();
    }

    _showCursor() {
        // A zero-length motion: the cursor reappears without moving.
        if (this._pad && !this._tracker.get_pointer_visible())
            this._pointer.notify_relative_motion(GLib.get_monotonic_time(), 0, 0);
    }

    _updateFilter() {
        // Look at input events only while the pad is open and listens for touches.
        const wanted = this._pad !== null && this._bridge !== null;
        if (wanted && !this._filterId) {
            // Mutter's own filter runs first. While nothing holds a grab, it has
            // already delivered each touch to the pad window, which V-Touchpad
            // then ignores; during a grab it leaves the event to the grab,
            // which only sees it after this filter.
            this._filterId = Clutter.Event.add_filter(null,
                (event, actor) => this._filterEvent(event, actor));
            this._emit('Active', new GLib.Variant('(b)', [true]));
        } else if (!wanted && this._filterId) {
            Clutter.Event.remove_filter(this._filterId);
            this._filterId = 0;
            this._taken.clear();
            if (this._bridge)
                this._emit('Active', new GLib.Variant('(b)', [false]));
        }
    }

    _emit(signal, params) {
        Gio.DBus.session.emit_signal(this._bridge, BRIDGE_PATH, BRIDGE_NAME, signal, params);
    }

    _contentsRect() {
        return this._pad.frame_rect_to_client_rect(this._pad.get_frame_rect());
    }

    _filterEvent(event, actor) {
        const phase = TOUCH_PHASES.get(event.type());
        if (!phase)
            return Clutter.EVENT_PROPAGATE;
        try {
            return this._filterTouch(event, actor, phase);
        } catch (e) {
            logError(e, 'V-Touchpad: cannot pass a touch to the pad');
            return Clutter.EVENT_PROPAGATE;
        }
    }

    _filterTouch(event, actor, phase) {
        const slot = event.get_event_sequence().get_slot();
        const [x, y] = event.get_coords();
        if (phase === 'begin')
            this._taken.delete(slot); // in case its last touch ended unseen
        let rect = this._taken.get(slot);
        if (!rect) {
            if (!this._isOnPad(actor))
                return Clutter.EVENT_PROPAGATE;
            rect = this._contentsRect();
            if (phase === 'begin') {
                // The title bar and borders belong to GNOME.
                if (x < rect.x || x >= rect.x + rect.width || y < rect.y || y >= rect.y + rect.height)
                    return Clutter.EVENT_PROPAGATE;
            } else {
                // A finger that was already down when this filter started, e.g.
                // when the pad was shown. Take it over only if an app's grab
                // would close a menu when it lifts; V-Touchpad ignores the rest.
                // Outside such a grab it may belong to a GNOME Shell gesture.
                const grab = global.stage.get_grab_actor();
                if (grab?.get_name() !== WAYLAND_GRAB_ACTOR)
                    return Clutter.EVENT_PROPAGATE;
            }
        }
        if (phase === 'begin' || phase === 'update')
            this._taken.set(slot, rect);
        else
            this._taken.delete(slot);

        // Position within the pad window's contents, as a fraction of its size,
        // so it does not depend on how XWayland scales the window.
        this._emit('Touch', new GLib.Variant('(sidd)', [phase, slot,
            (x - rect.x) / Math.max(rect.width, 1),
            (y - rect.y) / Math.max(rect.height, 1)]));
        return Clutter.EVENT_STOP;
    }

    _isOnPad(actor) {
        for (; actor; actor = actor.get_parent()) {
            if (actor instanceof Meta.WindowActor)
                return actor.get_meta_window() === this._pad;
        }
        return false;
    }
}
