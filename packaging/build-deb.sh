#!/usr/bin/env bash
# Build dist/v-touchpad_<version>_all.deb. Needs only dpkg-deb (any Debian/Ubuntu).
set -euo pipefail

APP=v-touchpad
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$ROOT/v_touchpad.py")"
MAINTAINER="${DEB_MAINTAINER:-$(git -C "$ROOT" config user.name) <$(git -C "$ROOT" config user.email)>}"
umask 022
PKG="$(mktemp -d)"
trap 'rm -rf "$PKG"' EXIT
chmod 755 "$PKG"

# --- Files ------------------------------------------------------------------
install -Dm 644 "$ROOT/v_touchpad.py" "$PKG/usr/share/$APP/v_touchpad.py"
install -Dm 644 -t "$PKG/usr/share/$APP/data" "$ROOT/data/$APP.svg" "$ROOT/data/$APP-tray.svg"
install -Dm 644 "$ROOT/data/$APP.desktop" "$PKG/usr/share/applications/$APP.desktop"
install -Dm 644 "$ROOT/data/$APP.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/$APP.svg"
install -Dm 644 "$ROOT/data/60-$APP-uinput.rules" "$PKG/usr/lib/udev/rules.d/60-$APP-uinput.rules"
# libinput reads every *.quirks file in its data directory.
install -Dm 644 "$ROOT/data/$APP.quirks" "$PKG/usr/share/libinput/90-$APP.quirks"

mkdir -p "$PKG/usr/bin"
cat > "$PKG/usr/bin/$APP" <<EOF
#!/bin/sh
exec /usr/bin/python3 /usr/share/$APP/v_touchpad.py "\$@"
EOF
chmod 755 "$PKG/usr/bin/$APP"

DOC="$PKG/usr/share/doc/$APP"
mkdir -p "$DOC"
{
    echo "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/"
    echo "Upstream-Name: $APP"
    echo
    echo "Files: *"
    echo "Copyright: $(sed -n 's/^Copyright (c) //p' "$ROOT/LICENSE")"
    echo "License: Expat"
    sed -n '/^Permission is hereby/,$p' "$ROOT/LICENSE" | sed 's/^$/./; s/^/ /'
} > "$DOC/copyright"
printf '%s (%s) unstable; urgency=medium\n\n  * Release %s.\n\n -- %s  %s\n' \
    "$APP" "$VERSION" "$VERSION" "$MAINTAINER" "$(date -R)" | gzip -9n > "$DOC/changelog.gz"
chmod 644 "$DOC"/*

# --- Package metadata -------------------------------------------------------
mkdir -p "$PKG/DEBIAN"
cat > "$PKG/DEBIAN/control" <<EOF
Package: $APP
Version: $VERSION
Architecture: all
Maintainer: $MAINTAINER
Installed-Size: $(du -sk --exclude=DEBIAN "$PKG" | cut -f1)
Depends: python3 (>= 3.10), python3-evdev, python3-pyside6.qtcore, python3-pyside6.qtgui (>= 6.5),
 python3-pyside6.qtwidgets, python3-pyside6.qtnetwork, qt6-svg-plugins,
 qt6-xdgdesktopportal-platformtheme
Recommends: gnome-shell-extension-appindicator
Section: utils
Priority: optional
Description: on-screen touchpad for Linux touchscreens
 V-Touchpad shows a small floating window that works like a laptop
 touchpad: pointer movement, tap to click, two-finger scrolling and
 right-click, gestures, and left/right buttons. Fingers are forwarded to
 a virtual touchpad, so the desktop treats it like real hardware.
 .
 Includes a tray icon, light and dark themes, and start at login.
EOF

cat > "$PKG/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ] && command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload || true
    udevadm trigger --subsystem-match=misc --sysname-match=uinput || true
    echo "V-Touchpad: on 2-in-1s, log out and back in once so it keeps working in tablet mode."
fi
EOF
cat > "$PKG/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = remove ] || [ "$1" = purge ]; then
    command -v udevadm >/dev/null 2>&1 && udevadm control --reload || true
fi
EOF
chmod 755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/postrm"

mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/${APP}_${VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$PKG" "$OUT" >/dev/null
echo "Built $OUT"
