#!/bin/sh
set -eu

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=$(cat "$SOURCE_DIR/VERSION")
ARCH=$(dpkg --print-architecture)
PACKAGE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/sentinel-deb.XXXXXX")
OUTPUT=${1:-"$SOURCE_DIR/sentinel-pro_${VERSION}_${ARCH}.deb"}

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
install -m 0644 "$SOURCE_DIR/io.sentinel.SentinelPro.svg" \
    "$PACKAGE_ROOT/usr/share/icons/hicolor/scalable/apps/io.sentinel.SentinelPro.svg"
sed 's|@APP_PATH@|/usr/lib/sentinel/app.py|g' \
    "$SOURCE_DIR/io.sentinel.SentinelPro.desktop.in" \
    > "$PACKAGE_ROOT/usr/share/applications/io.sentinel.SentinelPro.desktop"

cat > "$PACKAGE_ROOT/usr/bin/sentinel-pro" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/lib/sentinel/app.py "$@"
EOF
chmod 0755 "$PACKAGE_ROOT/usr/bin/sentinel-pro"
# Прежняя команда запуска (до 0.6.4.3 пакет назывался sentinel-video-client).
ln -s sentinel-pro "$PACKAGE_ROOT/usr/bin/sentinel-video-client"

cat > "$PACKAGE_ROOT/DEBIAN/control" <<EOF
Package: sentinel-pro
Version: ${VERSION}-1
Section: video
Priority: optional
Architecture: ${ARCH}
Maintainer: Sentinel contributors
Provides: sentinel-video-client
Conflicts: sentinel-video-client
Replaces: sentinel-video-client
Description: Sentinel PRO - universal security management center
 Sentinel PRO is a Linux desktop security management center: live video and
 playback, alarms and events, access control, visitors, e-maps and backup recording.
Depends: python3, python3-gi, python3-gi-cairo, gir1.2-gtk-4.0, gir1.2-gstreamer-1.0, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad, gstreamer1.0-libav, libsecret-tools
Recommends: ffmpeg, python3-venv
Suggests: gir1.2-webkit-6.0, mesa-va-drivers, intel-media-va-driver, vainfo
EOF

dpkg-deb --build --root-owner-group "$PACKAGE_ROOT" "$OUTPUT"
printf 'Built %s\n' "$OUTPUT"

# Серверная часть — отдельный пакет рядом с клиентским.
sh "$SOURCE_DIR/packaging/build-server-deb.sh" "$(dirname -- "$OUTPUT")/sentinel-pro-server_${VERSION}_all.deb"
