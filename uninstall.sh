#!/usr/bin/env bash
# Remove v-touchpad. With --system, also undo the system setup done by install.sh.
set -euo pipefail

APP=v-touchpad
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
UDEV_RULE=/etc/udev/rules.d/60-$APP-uinput.rules
QUIRKS=/etc/libinput/local-overrides.quirks

system=0
case "${1:-}" in
    "") ;;
    --system) system=1 ;;
    -h|--help) echo "Usage: ./uninstall.sh [--system]"; exit 0 ;;
    *) echo "Usage: ./uninstall.sh [--system]" >&2; exit 2 ;;
esac

pkill -f "$DATA_HOME/$APP/v_touchpad.py" 2>/dev/null || true
rm -rf "$DATA_HOME/$APP"
rm -f "$HOME/.local/bin/$APP" \
      "$DATA_HOME/applications/$APP.desktop" \
      "$DATA_HOME/icons/hicolor/scalable/apps/$APP.svg" \
      "$CONFIG_HOME/autostart/$APP.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA_HOME/applications" || true
echo "Removed V-Touchpad."

if ((system)); then
    if [ -f "$UDEV_RULE" ]; then
        sudo rm -f "$UDEV_RULE"
        sudo udevadm control --reload
    fi
    if [ -f "$QUIRKS" ] && grep -q "^# BEGIN $APP" "$QUIRKS"; then
        sudo sed -i "/^# BEGIN $APP\$/,/^# END $APP\$/d" "$QUIRKS"
    fi
    echo "Removed the system setup (/dev/uinput rule, libinput quirk)."
else
    echo "System setup left in place; run ./uninstall.sh --system to remove it too."
fi
