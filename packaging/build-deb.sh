#!/bin/sh
set -eu

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=$(cat "$SOURCE_DIR/VERSION")
ARCH=$(dpkg --print-architecture)
PACKAGE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/sentinel-deb.XXXXXX")
OUTPUT=${1:-"$SOURCE_DIR/sentinel_${VERSION}_${ARCH}.deb"}

cleanup() {
    rm -rf "$PACKAGE_ROOT"
}
trap cleanup EXIT HUP INT TERM

mkdir -p \
    "$PACKAGE_ROOT/DEBIAN" \
    "$PACKAGE_ROOT/usr/bin" \
    "$PACKAGE_ROOT/usr/lib/sentinel" \
    "$PACKAGE_ROOT/usr/share/applications" \
    "$PACKAGE_ROOT/usr/share/icons/hicolor/scalable/apps"

install -m 0644 "$SOURCE_DIR/app.py" "$PACKAGE_ROOT/usr/lib/sentinel/app.py"
for module in "$SOURCE_DIR"/sentinel_*.py; do
    install -m 0644 "$module" "$PACKAGE_ROOT/usr/lib/sentinel/$(basename "$module")"
done
install -m 0644 "$SOURCE_DIR/VERSION" "$PACKAGE_ROOT/usr/lib/sentinel/VERSION"
install -m 0644 "$SOURCE_DIR/io.sentinel.VideoClient.svg" \
    "$PACKAGE_ROOT/usr/share/icons/hicolor/scalable/apps/io.sentinel.VideoClient.svg"
sed 's|@APP_PATH@|/usr/lib/sentinel/app.py|g' \
    "$SOURCE_DIR/io.sentinel.VideoClient.desktop.in" \
    > "$PACKAGE_ROOT/usr/share/applications/io.sentinel.VideoClient.desktop"

cat > "$PACKAGE_ROOT/usr/bin/sentinel-video-client" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/lib/sentinel/app.py "$@"
EOF
chmod 0755 "$PACKAGE_ROOT/usr/bin/sentinel-video-client"

cat > "$PACKAGE_ROOT/DEBIAN/control" <<EOF
Package: sentinel-video-client
Version: ${VERSION}-1
Section: video
Priority: optional
Architecture: ${ARCH}
Maintainer: Sentinel contributors
Description: GTK video surveillance client
 Sentinel is a local Linux desktop client for supported video surveillance devices.
Depends: python3, python3-gi, python3-gi-cairo, gir1.2-gtk-4.0, gir1.2-gstreamer-1.0, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad, gstreamer1.0-libav, libsecret-tools
Recommends: ffmpeg, python3-venv
Suggests: gir1.2-webkit-6.0
EOF

dpkg-deb --build --root-owner-group "$PACKAGE_ROOT" "$OUTPUT"
printf 'Built %s\n' "$OUTPUT"

# Серверная часть — отдельный пакет рядом с клиентским.
sh "$SOURCE_DIR/packaging/build-server-deb.sh" "$(dirname -- "$OUTPUT")/sentinel_server_${VERSION}_all.deb"
