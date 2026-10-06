#!/usr/bin/env python3
"""v-touchpad: an on-screen touchpad for Linux touchscreens.

Finger positions are forwarded unchanged to a virtual touchpad device, so
libinput (and therefore the desktop) handles pointer acceleration, tap-to-click,
smooth two-finger scrolling, pinch and three/four-finger swipes exactly as it
does for a physical touchpad, using the system touchpad settings.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from evdev import AbsInfo, UInput, ecodes
from evdev.uinput import UInputError
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, QSettings, Qt
from PySide6.QtGui import QActionGroup, QColor, QEventPoint, QFont, QIcon, QPainter, QPalette, QPen
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QCheckBox, QMenu, QMessageBox, QSystemTrayIcon,
                               QWidget)

APP_ID = "v-touchpad"
VERSION = "0.1.0"
DATA_DIR = Path(__file__).resolve().parent / "data"
ICON_FILE = DATA_DIR / f"{APP_ID}.svg"
TRAY_ICON_FILE = DATA_DIR / f"{APP_ID}-tray.svg"
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
AUTOSTART_FILE = CONFIG_HOME / "autostart" / f"{APP_ID}.desktop"
LAUNCHERS = {Path("/usr/share") / APP_ID: Path("/usr/bin") / APP_ID,  # .deb package
             Path.home() / ".local/share" / APP_ID: Path.home() / ".local/bin" / APP_ID}  # install.sh
CLOSE_ACTIONS = {"ask": "Ask every time", "hide": "Hide to tray", "quit": "Quit"}

SLOTS = 5
RESOLUTION = 40  # device units per mm
SURFACE_MM = (300, 200)  # virtual surface; the on-screen pad maps onto its centre
FALLBACK_MM_PER_PX = 25.4 / 96
TOOL_KEYS = (ecodes.BTN_TOOL_FINGER, ecodes.BTN_TOOL_DOUBLETAP, ecodes.BTN_TOOL_TRIPLETAP,
             ecodes.BTN_TOOL_QUADTAP, ecodes.BTN_TOOL_QUINTTAP)
TOUCH_EVENTS = {QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate,
                QEvent.Type.TouchEnd, QEvent.Type.TouchCancel}

# Layout, in logical pixels.
MARGIN = 10
BAR_H = 30      # top strip: drag handle and theme toggle
BUTTON_H = 44
GAP = 6

THEMES = {
    "light": {"window": "#eceef1", "pad": "#ffffff", "edge": "#00000014", "button": "#dfe2e7",
              "pressed": "#c9cdd4", "handle": "#b3b8c0", "finger": "#0000001a", "error": "#b3261e",
              "text": "#1f2328", "accent": "#3584e4"},
    "dark": {"window": "#1b1c1f", "pad": "#26282c", "edge": "#ffffff0f", "button": "#303237",
             "pressed": "#45484f", "handle": "#5b5f67", "finger": "#ffffff1f", "error": "#f2b8b5",
             "text": "#e6e7ea", "accent": "#62a0ea"},
}


def _color(value: str) -> QColor:
    """QColor from "#rrggbb" or CSS-style "#rrggbbaa"."""
    color = QColor(value[:7])
    if len(value) == 9:
        color.setAlpha(int(value[7:], 16))
    return color


def _axis(maximum: int, resolution: int = RESOLUTION) -> AbsInfo:
    return AbsInfo(value=0, min=0, max=maximum, fuzz=0, flat=0, resolution=resolution)


class _UInput(UInput):
    """UInput that never opens its own /dev/input/event* node.

    We only write to the device. Desktop users usually cannot read that node,
    and python-evdev would retry for two seconds per device before giving up.
    """

    def _find_device(self, fd: int) -> None:
        return None


class VirtualTouchpad:
    """Multi-touch (protocol B) touchpad with two physical buttons."""

    def __init__(self) -> None:
        self.width, self.height = (mm * RESOLUTION for mm in SURFACE_MM)
        self.device = _UInput(
            {ecodes.EV_KEY: [ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_TOUCH, *TOOL_KEYS],
             ecodes.EV_ABS: [(ecodes.ABS_X, _axis(self.width)),
                             (ecodes.ABS_Y, _axis(self.height)),
                             (ecodes.ABS_MT_SLOT, _axis(SLOTS - 1, 0)),
                             (ecodes.ABS_MT_TRACKING_ID, _axis(0xFFFF, 0)),
                             (ecodes.ABS_MT_POSITION_X, _axis(self.width)),
                             (ecodes.ABS_MT_POSITION_Y, _axis(self.height))]},
            name="V-Touchpad",
            # A USB bus makes libinput treat this as an external touchpad, which
            # disables palm edge zones and disable-while-typing pairing.
            bustype=ecodes.BUS_USB, vendor=0x1209, product=0x0001,
            input_props=[ecodes.INPUT_PROP_POINTER],
        )
        self.slots: list[tuple[int, int] | None] = [None] * SLOTS
        self._slot = 0
        self._tracking_id = 0
        self._fingers = 0

    def _abs(self, code: int, value: int) -> None:
        self.device.write(ecodes.EV_ABS, code, value)

    def _select(self, slot: int) -> None:
        if slot != self._slot:
            self._abs(ecodes.ABS_MT_SLOT, slot)
            self._slot = slot

    def _clamp(self, x: float, y: float) -> tuple[int, int]:
        return round(min(max(x, 0), self.width)), round(min(max(y, 0), self.height))

    def touch_down(self, x: float, y: float) -> int | None:
        if None not in self.slots:
            return None
        slot = self.slots.index(None)
        self.slots[slot] = self._clamp(x, y)
        self._select(slot)
        self._abs(ecodes.ABS_MT_TRACKING_ID, self._tracking_id)
        self._tracking_id = (self._tracking_id + 1) & 0xFFFF
        self._abs(ecodes.ABS_MT_POSITION_X, self.slots[slot][0])
        self._abs(ecodes.ABS_MT_POSITION_Y, self.slots[slot][1])
        return slot

    def touch_move(self, slot: int, x: float, y: float) -> None:
        pos = self._clamp(x, y)
        if pos == self.slots[slot]:
            return
        self._select(slot)
        if pos[0] != self.slots[slot][0]:
            self._abs(ecodes.ABS_MT_POSITION_X, pos[0])
        if pos[1] != self.slots[slot][1]:
            self._abs(ecodes.ABS_MT_POSITION_Y, pos[1])
        self.slots[slot] = pos

    def touch_up(self, slot: int) -> None:
        self.slots[slot] = None
        self._select(slot)
        self._abs(ecodes.ABS_MT_TRACKING_ID, -1)

    def sync(self) -> None:
        """Finish a frame: update finger-count keys, single-touch axes, SYN_REPORT."""
        active = [pos for pos in self.slots if pos is not None]
        if len(active) != self._fingers:
            if bool(active) != bool(self._fingers):
                self.device.write(ecodes.EV_KEY, ecodes.BTN_TOUCH, int(bool(active)))
            if self._fingers:
                self.device.write(ecodes.EV_KEY, TOOL_KEYS[self._fingers - 1], 0)
            if active:
                self.device.write(ecodes.EV_KEY, TOOL_KEYS[len(active) - 1], 1)
            self._fingers = len(active)
        if active:
            self._abs(ecodes.ABS_X, active[0][0])
            self._abs(ecodes.ABS_Y, active[0][1])
        self.device.syn()

    def close(self) -> None:
        self.device.close()


class VirtualMouse:
    """Relative mouse for the LEFT/RIGHT buttons and for keeping the cursor alive.

    GNOME hides the cursor on every touchscreen event and, while it is hidden,
    gives no app pointer focus, so clicks only reach GNOME Shell (e.g. the dock).
    A pointer event brings both back, but only *after* that event is handled.
    `nudge()` sends a net-zero motion so the cursor is visible and focused before
    the next button event, and stays visible after the fingers lift.
    """

    def __init__(self) -> None:
        self.device = _UInput(
            {ecodes.EV_KEY: [ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE],
             ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y]},
            name="V-Touchpad Buttons",
            bustype=ecodes.BUS_USB, vendor=0x1209, product=0x0002,
        )
        self._step = 1

    def nudge(self) -> None:
        # Separate frames: libinput drops a frame whose motion sums to zero.
        # Alternate the direction so acceleration rounding cannot drift one way.
        for dx in (self._step, -self._step):
            self.device.write(ecodes.EV_REL, ecodes.REL_X, dx)
            self.device.syn()
        self._step = -self._step

    def button(self, code: int, pressed: bool) -> None:
        self.device.write(ecodes.EV_KEY, code, int(pressed))
        self.device.syn()

    def close(self) -> None:
        self.device.close()


class Touchpad(QWidget):
    def __init__(self, pad: VirtualTouchpad | None, mouse: VirtualMouse | None, error: str = "",
                 speed: float = 1.0, debug: bool = False, theme: str = "auto") -> None:
        super().__init__()
        self.pad = pad
        self.mouse = mouse
        self.debug = debug
        self.error = error
        self.speed = speed
        self.scale = 1.0  # device units per widget pixel, refreshed on each TouchBegin
        # Qt touch point id -> ("pad", slot) | ("button", code) | ("move", 0) | ("ignored", 0)
        self.touches: dict[int, tuple[str, int]] = {}
        self.held: dict[int, int] = {}  # button code -> fingers holding it
        self.drag: tuple[int, QPointF, QPoint] | None = None  # finger, start global pos, start window pos
        self.fingers: dict[int, QPointF] = {}  # pad finger positions, drawn as feedback
        self.theme = theme  # "auto" follows the desktop; the toggle switches to light/dark
        QApplication.styleHints().colorSchemeChanged.connect(self.update)
        self.setWindowTitle("V-Touchpad")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents)
        self.setMinimumSize(340, 210)
        self.resize(460, 270)

    def pad_rect(self) -> QRectF:
        return QRectF(self.rect()).adjusted(MARGIN, BAR_H, -MARGIN, -(BUTTON_H + GAP + MARGIN))

    def button_rects(self) -> dict[int, QRectF]:
        width = (self.width() - 2 * MARGIN - GAP) / 2
        y = self.height() - MARGIN - BUTTON_H
        return {ecodes.BTN_LEFT: QRectF(MARGIN, y, width, BUTTON_H),
                ecodes.BTN_RIGHT: QRectF(MARGIN + width + GAP, y, width, BUTTON_H)}

    def toggle_rect(self) -> QRectF:
        return QRectF(self.width() - MARGIN - BAR_H, 0, BAR_H, BAR_H)

    def is_dark(self) -> bool:
        if self.theme != "auto":
            return self.theme == "dark"
        return QApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark

    def palette_for_theme(self) -> QPalette:
        """Palette for dialogs, matching the pad's light or dark look."""
        c = {name: _color(value) for name, value in THEMES["dark" if self.is_dark() else "light"].items()}
        palette = QPalette()
        for role, color in ((QPalette.ColorRole.Window, c["window"]), (QPalette.ColorRole.Base, c["pad"]),
                            (QPalette.ColorRole.Button, c["button"]), (QPalette.ColorRole.Highlight, c["accent"]),
                            (QPalette.ColorRole.WindowText, c["text"]), (QPalette.ColorRole.Text, c["text"]),
                            (QPalette.ColorRole.ButtonText, c["text"])):
            palette.setColor(role, color)
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        return palette

    def toggle_theme(self) -> None:
        self.theme = "light" if self.is_dark() else "dark"
        self.update()

    def paintEvent(self, event) -> None:
        del event
        c = {name: _color(value) for name, value in THEMES["dark" if self.is_dark() else "light"].items()}
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), c["window"])
        p.setPen(Qt.PenStyle.NoPen)

        # Drag handle.
        p.setBrush(c["handle"])
        p.drawRoundedRect(QRectF(self.width() / 2 - 20, BAR_H / 2 - 2, 40, 4), 2, 2)

        # Theme toggle: a half-filled circle.
        centre = self.toggle_rect().center()
        p.setPen(QPen(c["handle"], 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(centre, 6, 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c["handle"])
        p.drawPie(QRectF(centre.x() - 6, centre.y() - 6, 12, 12), 90 * 16, 180 * 16)

        pad = self.pad_rect()
        p.setPen(QPen(c["edge"], 1))
        p.setBrush(c["pad"])
        p.drawRoundedRect(pad.adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c["finger"])
        for pos in self.fingers.values():
            p.drawEllipse(pos, 18, 18)
        if self.error:
            font = QFont(self.font())
            font.setPixelSize(12)
            p.setFont(font)
            p.setPen(c["error"])
            p.drawText(pad.adjusted(20, 12, -20, -12),
                       Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.error)
            p.setPen(Qt.PenStyle.NoPen)

        for code, rect in self.button_rects().items():
            p.setBrush(c["pressed"] if self.held.get(code, 0) else c["button"])
            p.drawRoundedRect(rect, 12, 12)

    def _units_per_px(self) -> float:
        mm_per_px = FALLBACK_MM_PER_PX
        screen = self.screen()
        if screen and screen.physicalSize().width() > 0 and screen.geometry().width() > 0:
            mm_per_px = screen.physicalSize().width() / screen.geometry().width()
            # Some outputs report nonsense physical sizes; keep it plausible.
            mm_per_px = min(max(mm_per_px, 0.1), 0.6)
        return mm_per_px * RESOLUTION * self.speed

    def _to_device(self, pos: QPointF) -> tuple[float, float]:
        centre = self.pad_rect().center()
        return (self.pad.width / 2 + (pos.x() - centre.x()) * self.scale,
                self.pad.height / 2 + (pos.y() - centre.y()) * self.scale)

    def _press(self, point: QEventPoint) -> None:
        pos = point.position()
        pad = self.pad_rect()
        if pos.y() < pad.top() and self.toggle_rect().contains(pos):
            self.touches[point.id()] = ("ignored", 0)
            self.toggle_theme()
        elif pos.y() < pad.top():
            self.touches[point.id()] = ("move", 0)
            if len(self.touches) == 1:
                if QApplication.platformName() == "xcb":
                    # GNOME ignores touch-started system moves of X11 windows,
                    # but X11 windows may move themselves.
                    self.drag = (point.id(), point.globalPosition(), self.pos())
                elif self.windowHandle():
                    self.windowHandle().startSystemMove()
        elif pos.y() > pad.bottom():
            code = ecodes.BTN_LEFT if pos.x() < self.width() / 2 else ecodes.BTN_RIGHT
            self.touches[point.id()] = ("button", code)
            self.held[code] = self.held.get(code, 0) + 1
            if self.held[code] == 1:
                self.mouse.button(code, True)
                self.update()
        else:
            slot = self.pad.touch_down(*self._to_device(pos))
            self.touches[point.id()] = ("ignored", 0) if slot is None else ("pad", slot)
            if slot is not None:
                self.fingers[point.id()] = pos
                self.update()

    def _release(self, point_id: int) -> None:
        role, value = self.touches.pop(point_id, ("ignored", 0))
        if self.drag and self.drag[0] == point_id:
            self.drag = None
        if role == "pad":
            self.pad.touch_up(value)
            self.fingers.pop(point_id, None)
            self.update()
        elif role == "button":
            self.held[value] -= 1
            if self.held[value] == 0:
                self.mouse.button(value, False)
                self.update()

    def event(self, event) -> bool:
        if event.type() not in TOUCH_EVENTS:
            if self.debug and event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove):
                print(f"{event.type().name} {event.position()} (mouse, not touch)", file=sys.stderr)
            return super().event(event)
        event.accept()
        if self.debug:
            print(event.type().name, [(p.id(), p.state().name, round(p.position().x()), round(p.position().y()))
                                      for p in event.points()], file=sys.stderr)
        if not (self.pad and self.mouse):
            return True
        # This touchscreen event just hid the cursor and took pointer focus from
        # every app. Nudge the mouse *before* sending any motion, scroll, button
        # or tap event, so it reaches the app under the cursor.
        self.mouse.nudge()
        if event.type() == QEvent.Type.TouchBegin:
            self.scale = self._units_per_px()
        if event.type() in (QEvent.Type.TouchBegin, QEvent.Type.TouchCancel):
            for point_id in list(self.touches):  # stale or cancelled contacts
                self._release(point_id)
        if event.type() != QEvent.Type.TouchCancel:
            for point in event.points():
                if point.state() == QEventPoint.State.Released:
                    self._release(point.id())
                elif point.id() not in self.touches:
                    self._press(point)
                elif point.state() == QEventPoint.State.Updated:
                    role, slot = self.touches[point.id()]
                    if role == "pad":
                        self.pad.touch_move(slot, *self._to_device(point.position()))
                        self.fingers[point.id()] = point.position()
                        self.update()
                    elif self.drag and self.drag[0] == point.id():
                        _, start, origin = self.drag
                        self.move(origin + (point.globalPosition() - start).toPoint())
        self.pad.sync()
        return True


class Controller(QObject):
    """Tray icon, start-at-login, the close prompt and single-instance handling."""

    def __init__(self, window: Touchpad, server: QLocalServer) -> None:
        super().__init__()
        self.window = window
        self.settings = QSettings(APP_ID, APP_ID)  # ~/.config/v-touchpad/v-touchpad.conf
        self.quitting = False
        self.server = server
        server.newConnection.connect(self._on_connection)
        self.tray: QSystemTrayIcon | None = None
        window.installEventFilter(self)
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        menu = QMenu()
        self.toggle_action = menu.addAction("Show touchpad", self.toggle_window)  # relabelled on show/hide
        menu.addSeparator()
        self.autostart_action = menu.addAction("Start at login")
        self.autostart_action.setCheckable(True)
        self.autostart_action.setChecked(AUTOSTART_FILE.exists())
        self.autostart_action.toggled.connect(set_autostart)
        close_menu = menu.addMenu("When closing the window")
        self.close_group = QActionGroup(close_menu)
        for key, label in CLOSE_ACTIONS.items():
            action = close_menu.addAction(label)
            action.setCheckable(True)
            action.setData(key)
            self.close_group.addAction(action)
        self.close_group.triggered.connect(lambda action: self.set_close_action(action.data()))
        self._sync_close_menu()
        menu.addSeparator()
        menu.addAction("Quit", self.quit)
        self.menu = menu  # keep a reference; the tray does not own it

        self.tray = QSystemTrayIcon(QIcon(str(TRAY_ICON_FILE)), self)
        self.tray.setToolTip("V-Touchpad")
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def close_action(self) -> str:
        action = self.settings.value("close_action", "ask")
        return action if action in CLOSE_ACTIONS else "ask"

    def set_close_action(self, action: str) -> None:
        self.settings.setValue("close_action", action)
        self._sync_close_menu()

    def _sync_close_menu(self) -> None:
        for action in self.close_group.actions():
            action.setChecked(action.data() == self.close_action())

    def show_window(self) -> None:
        self.window.show()
        self.window.raise_()

    def toggle_window(self) -> None:
        if self.window.isVisible():
            self.window.hide()
        else:
            self.show_window()

    def quit(self) -> None:
        self.quitting = True
        QApplication.quit()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Most trays send Trigger on a left click. Ubuntu's AppIndicator extension
        # opens the menu on a left click and sends Trigger only on a double-click.
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.MiddleClick):
            self.toggle_window()

    def _on_connection(self) -> None:
        """Another launch of the app: show this instance's pad (unless it asked not to)."""
        socket = self.server.nextPendingConnection()
        socket.waitForReadyRead(500)
        if bytes(socket.readAll().data()) != b"hidden":
            self.show_window()
        socket.disconnectFromServer()

    def _ask_close_action(self) -> str | None:
        box = QMessageBox(self.window)
        box.setPalette(self.window.palette_for_theme())
        box.setWindowTitle("Close V-Touchpad")
        box.setText("Hide V-Touchpad to the tray, or quit it?")
        box.setInformativeText("When hidden, it keeps running and you can bring it back from its tray icon.")
        hide = box.addButton("Hide to tray", QMessageBox.ButtonRole.AcceptRole)
        quit_ = box.addButton("Quit", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(hide)
        remember = QCheckBox("Remember my choice")
        box.setCheckBox(remember)
        box.exec()
        if box.clickedButton() not in (hide, quit_):
            return None
        action = "hide" if box.clickedButton() is hide else "quit"
        if remember.isChecked():
            self.set_close_action(action)
        return action

    def eventFilter(self, obj, event) -> bool:
        if obj is self.window and event.type() in (QEvent.Type.Show, QEvent.Type.Hide) and self.tray:
            self.toggle_action.setText("Hide touchpad" if event.type() == QEvent.Type.Show
                                       else "Show touchpad")
        if obj is not self.window or event.type() != QEvent.Type.Close or self.quitting:
            return False
        action = self.close_action() if self.tray else "quit"
        if action == "ask":
            action = self._ask_close_action()
        if action == "hide":
            self.window.hide()
        elif action == "quit":
            self.quit()
        event.ignore()
        return True


def set_autostart(enabled: bool) -> None:
    """Start in the tray at login, through the XDG autostart directory."""
    if not enabled:
        AUTOSTART_FILE.unlink(missing_ok=True)
        return
    launcher = LAUNCHERS.get(Path(__file__).resolve().parent)
    if launcher and launcher.exists():
        command = f'"{launcher}"'
    else:
        command = f'"{sys.executable}" "{Path(__file__).resolve()}"'
    AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART_FILE.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=V-Touchpad\n"
        "Comment=Start V-Touchpad in the tray\n"
        f"Exec={command} --hidden\n"
        f"Icon={APP_ID}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog=APP_ID, description="On-screen touchpad for Linux touchscreens.")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="finger travel multiplier; 1.0 matches the pad's physical size (default)")
    parser.add_argument("--theme", choices=("auto", "light", "dark"), default="auto",
                        help="colour scheme; auto follows the desktop (default)")
    parser.add_argument("--hidden", action="store_true",
                        help="start in the tray without showing the touchpad")
    parser.add_argument("--debug", action="store_true", help="print received touch events")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args, qt_args = parser.parse_known_args()

    # GNOME ignores always-on-top and no-focus requests from Wayland clients, so
    # run through XWayland, where both are honoured. QT_QPA_PLATFORM overrides.
    if os.environ.get("WAYLAND_DISPLAY") and os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    # Read the desktop's light/dark preference (and its changes) from the portal.
    os.environ.setdefault("QT_QPA_PLATFORMTHEME", "xdgdesktopportal")
    # Deliver every touch update instead of merging them; merged updates feel laggy.
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_CompressHighFrequencyEvents, False)
    app = QApplication([APP_ID, *qt_args])  # argv[0] becomes the X11 WM_CLASS
    app.setApplicationName(APP_ID)
    app.setStyle("Fusion")  # plain widgets that take the pad's light/dark palette
    app.setApplicationVersion(VERSION)
    if app.platformName() == "wayland":
        # Wayland app ID, to match windows to v-touchpad.desktop. On X11 the
        # WM_CLASS does that, and setting it there makes the portal complain.
        app.setDesktopFileName(APP_ID)
    app.setWindowIcon(QIcon.fromTheme(APP_ID, QIcon(str(ICON_FILE))))

    # Only one instance: a second launch asks the running one to show its pad.
    server_name = f"{APP_ID}-{os.getuid()}"
    socket = QLocalSocket()
    socket.connectToServer(server_name)
    if socket.waitForConnected(500):
        socket.write(b"hidden" if args.hidden else b"show")
        socket.waitForBytesWritten(500)
        socket.disconnectFromServer()
        return 0
    QLocalServer.removeServer(server_name)  # left over from a crashed instance
    server = QLocalServer()
    server.listen(server_name)

    pad = mouse = None
    error = ""
    try:
        pad = VirtualTouchpad()
        mouse = VirtualMouse()
    except (OSError, UInputError) as exc:
        error = f"Cannot create virtual input devices through /dev/uinput: {exc}"
    widget = Touchpad(pad, mouse, error, args.speed, args.debug, args.theme)
    controller = Controller(widget, server)
    app.setQuitOnLastWindowClosed(False)  # hiding the pad keeps the tray running
    if not (args.hidden and controller.tray):
        widget.show()
    for device in (pad, mouse):
        if device:
            app.aboutToQuit.connect(device.close)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
