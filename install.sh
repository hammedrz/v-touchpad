#!/usr/bin/env bash
# Install v-touchpad for the current user, plus the one-time system setup it needs.
set -euo pipefail

APP=v-touchpad
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
APP_DIR="$DATA_HOME/$APP"
BIN_DIR="$HOME/.local/bin"
UDEV_RULE=/etc/udev/rules.d/60-$APP-uinput.rules
QUIRKS=/etc/libinput/local-overrides.quirks

autostart=0
system=ask
usage() {
    cat <<EOF
Usage: ./install.sh [--autostart] [--system | --no-system]

  --autostart   also start V-Touchpad when you log in
  --system      do the system setup (sudo) without asking
  --no-system   skip the system setup (/dev/uinput access, tablet-mode quirk)
EOF
}
for arg in "$@"; do
    case "$arg" in
        --autostart) autostart=1 ;;
        --system) system=yes ;;
        --no-system) system=no ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# --- Requirements -----------------------------------------------------------
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null || die "python3 is not installed."
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || die "Python 3.10 or newer is required."
UV="$(command -v uv || true)"  # optional; avoids needing python3-venv
missing=()
[ -n "$UV" ] || "$PYTHON" -c 'import venv, ensurepip' 2>/dev/null || missing+=("python3-venv")
command -v "${CC:-cc}" >/dev/null || missing+=("C compiler")
"$PYTHON" -c 'import os, sysconfig; sys_inc = sysconfig.get_paths()["include"]; raise SystemExit(not os.path.exists(os.path.join(sys_inc, "Python.h")))' \
    || missing+=("Python headers")
if ((${#missing[@]})); then
    echo "Missing: ${missing[*]}. Install the build requirements with:"
    echo "  Debian/Ubuntu:  sudo apt install python3-venv python3-dev gcc"
    echo "  Fedora:         sudo dnf install python3-devel gcc"
    echo "  Arch:           sudo pacman -S python gcc"
    echo "  openSUSE:       sudo zypper install python3-devel gcc"
    exit 1
fi

# --- App files --------------------------------------------------------------
say "Installing to $APP_DIR"
mkdir -p "$APP_DIR/data" "$BIN_DIR"
install -m 644 "$SRC/v_touchpad.py" "$SRC/requirements.txt" "$APP_DIR/"
install -m 644 "$SRC/data/$APP.svg" "$APP_DIR/data/"

say "Installing Python libraries (PySide6, evdev); this can take a minute"
rm -rf "$APP_DIR/venv"
if [ -n "$UV" ]; then
    "$UV" venv --quiet --python "$(command -v "$PYTHON")" "$APP_DIR/venv"
    VIRTUAL_ENV="$APP_DIR/venv" "$UV" pip install --quiet -r "$APP_DIR/requirements.txt"
else
    "$PYTHON" -m venv "$APP_DIR/venv"
    "$APP_DIR/venv/bin/python" -m pip install --quiet --disable-pip-version-check \
        -r "$APP_DIR/requirements.txt"
fi

cat > "$BIN_DIR/$APP" <<EOF
#!/bin/sh
exec "$APP_DIR/venv/bin/python" "$APP_DIR/v_touchpad.py" "\$@"
EOF
chmod 755 "$BIN_DIR/$APP"

say "Adding V-Touchpad to the app menu"
install -Dm 644 "$SRC/data/$APP.svg" "$DATA_HOME/icons/hicolor/scalable/apps/$APP.svg"
mkdir -p "$DATA_HOME/applications"
# Absolute Exec path: ~/.local/bin is not on every session's PATH.
sed "s|^Exec=.*|Exec=$BIN_DIR/$APP|" "$SRC/data/$APP.desktop" > "$DATA_HOME/applications/$APP.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA_HOME/applications" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -qtf "$DATA_HOME/icons/hicolor" || true

if ((autostart)); then
    say "Starting V-Touchpad at login"
    mkdir -p "$CONFIG_HOME/autostart"
    cp "$DATA_HOME/applications/$APP.desktop" "$CONFIG_HOME/autostart/$APP.desktop"
fi

# --- System setup (sudo) ----------------------------------------------------
need_rule=0
[ -w /dev/uinput ] || need_rule=1
need_quirk=1
[ -f "$QUIRKS" ] && grep -q "^# BEGIN $APP" "$QUIRKS" && need_quirk=0

relogin=0
if ((need_rule || need_quirk)) && [ "$system" != no ]; then
    echo
    echo "System setup (needs sudo):"
    ((need_rule)) && echo "  - let your desktop user create virtual input devices (/dev/uinput)"
    ((need_quirk)) && echo "  - keep the pad working in tablet mode on 2-in-1s (libinput quirk)"
    answer=y
    if [ "$system" = ask ]; then
        read -r -p "Do it now? [Y/n] " answer || answer=n
    fi
    if [[ "${answer:-y}" =~ ^[Yy]?$|^[Yy]es$ ]]; then
        if ((need_rule)); then
            echo 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"' \
                | sudo tee "$UDEV_RULE" >/dev/null
            sudo udevadm control --reload
            sudo udevadm trigger --name-match=uinput || true
        fi
        if ((need_quirk)); then
            sudo mkdir -p /etc/libinput
            printf '# BEGIN %s\n[V-Touchpad]\nMatchName=V-Touchpad\nMatchUdevType=touchpad\nModelTabletModeNoSuspend=1\n# END %s\n' "$APP" "$APP" \
                | sudo tee -a "$QUIRKS" >/dev/null
        fi
        relogin=1
    else
        echo "Skipped. Run ./install.sh --system later to do it."
    fi
fi

echo
say "Done. Open V-Touchpad from the app menu, or run: $APP"
((relogin)) && echo "    Log out and back in first, so the system setup takes effect."
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "    ($BIN_DIR is not on your PATH; the app menu entry still works.)" ;; esac
