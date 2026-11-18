#!/bin/sh
set -eu

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DATA_ROOT=${XDG_DATA_HOME:-"$HOME/.local/share"}
APP_DIR="$DATA_ROOT/sentinel"
DESKTOP_DIR="$DATA_ROOT/applications"
ICON_DIR="$DATA_ROOT/icons/hicolor/scalable/apps"

mkdir -p "$APP_DIR" "$DESKTOP_DIR" "$ICON_DIR"
install -m 0755 "$SOURCE_DIR/app.py" "$APP_DIR/app.py"
install -m 0644 "$SOURCE_DIR/sentinel_storage.py" "$APP_DIR/sentinel_storage.py"
install -m 0644 "$SOURCE_DIR/sentinel_protocols.py" "$APP_DIR/sentinel_protocols.py"
install -m 0644 "$SOURCE_DIR/VERSION" "$APP_DIR/VERSION"
install -m 0644 "$SOURCE_DIR/io.sentinel.VideoClient.svg" "$ICON_DIR/io.sentinel.VideoClient.svg"
sed "s|@APP_PATH@|$APP_DIR/app.py|g" "$SOURCE_DIR/io.sentinel.VideoClient.desktop.in" > "$DESKTOP_DIR/io.sentinel.VideoClient.desktop"
chmod 0644 "$DESKTOP_DIR/io.sentinel.VideoClient.desktop"
printf 'Installed Sentinel GTK app. Start it from the desktop application menu.\n'
