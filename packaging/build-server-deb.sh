#!/bin/sh
# Пакет sentinel-server: серверная часть без GTK (systemd-служба).
set -eu

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=$(cat "$SOURCE_DIR/VERSION")
PACKAGE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/sentinel-server-deb.XXXXXX")
OUTPUT=${1:-"$SOURCE_DIR/sentinel_server_${VERSION}_all.deb"}
MODULES="sentinel_server sentinel_engine sentinel_storage sentinel_alarms sentinel_hikconnect sentinel_security
sentinel_protocols sentinel_people sentinel_recording sentinel_layouts sentinel_devmgmt sentinel_remote
sentinel_server_ctl sentinel_server_settings sentinel_server_tray sentinel_widgets sentinel_timeline"

cleanup() {
    rm -rf "$PACKAGE_ROOT"
}
trap cleanup EXIT HUP INT TERM
chmod 0755 "$PACKAGE_ROOT"

mkdir -p \
    "$PACKAGE_ROOT/DEBIAN" \
    "$PACKAGE_ROOT/usr/bin" \
    "$PACKAGE_ROOT/usr/lib/sentinel-server" \
    "$PACKAGE_ROOT/lib/systemd/system" \
    "$PACKAGE_ROOT/etc/default" \
    "$PACKAGE_ROOT/usr/share/doc/sentinel-server" \
    "$PACKAGE_ROOT/usr/share/sentinel-server/icons" \
    "$PACKAGE_ROOT/usr/share/applications" \
    "$PACKAGE_ROOT/usr/share/icons/hicolor/scalable/apps" \
    "$PACKAGE_ROOT/usr/share/icons/hicolor/scalable/status" \
    "$PACKAGE_ROOT/usr/share/polkit-1/actions" \
    "$PACKAGE_ROOT/usr/share/polkit-1/rules.d" \
    "$PACKAGE_ROOT/etc/xdg/autostart"

for module in $MODULES; do
    install -m 0644 "$SOURCE_DIR/$module.py" "$PACKAGE_ROOT/usr/lib/sentinel-server/$module.py"
done
install -m 0644 "$SOURCE_DIR/VERSION" "$PACKAGE_ROOT/usr/lib/sentinel-server/VERSION"
install -m 0644 "$SOURCE_DIR/packaging/sentinel-server.service" "$PACKAGE_ROOT/lib/systemd/system/sentinel-server.service"
install -m 0644 "$SOURCE_DIR/packaging/sentinel-server.default" "$PACKAGE_ROOT/etc/default/sentinel-server"
install -m 0644 "$SOURCE_DIR/README.md" "$PACKAGE_ROOT/usr/share/doc/sentinel-server/README.md"

# Окно настроек, значок в трее и помощник с правами root (pkexec)
install -m 0755 "$SOURCE_DIR/sentinel_server_helper.py" "$PACKAGE_ROOT/usr/lib/sentinel-server/sentinel-server-helper"
install -m 0644 "$SOURCE_DIR/packaging/io.sentinel.server.policy" "$PACKAGE_ROOT/usr/share/polkit-1/actions/io.sentinel.server.policy"
install -m 0644 "$SOURCE_DIR/packaging/50-sentinel-server.rules" "$PACKAGE_ROOT/usr/share/polkit-1/rules.d/50-sentinel-server.rules"
install -m 0644 "$SOURCE_DIR/packaging/io.sentinel.ServerSettings.desktop" "$PACKAGE_ROOT/usr/share/applications/io.sentinel.ServerSettings.desktop"
install -m 0644 "$SOURCE_DIR/packaging/io.sentinel.ServerTray.desktop" "$PACKAGE_ROOT/usr/share/applications/io.sentinel.ServerTray.desktop"
install -m 0644 "$SOURCE_DIR/packaging/io.sentinel.ServerTray-autostart.desktop" "$PACKAGE_ROOT/etc/xdg/autostart/io.sentinel.ServerTray.desktop"
install -m 0644 "$SOURCE_DIR/packaging/icons/io.sentinel.Server.svg" "$PACKAGE_ROOT/usr/share/icons/hicolor/scalable/apps/io.sentinel.Server.svg"
for icon in "$SOURCE_DIR"/packaging/icons/sentinel-server-*.svg; do
    install -m 0644 "$icon" "$PACKAGE_ROOT/usr/share/sentinel-server/icons/$(basename "$icon")"
    install -m 0644 "$icon" "$PACKAGE_ROOT/usr/share/icons/hicolor/scalable/status/$(basename "$icon")"
done
for tool in settings tray; do
    printf '#!/bin/sh\nexec /usr/bin/python3 /usr/lib/sentinel-server/sentinel_server_%s.py "$@"\n' "$tool" \
        > "$PACKAGE_ROOT/usr/bin/sentinel-server-$tool"
    chmod 0755 "$PACKAGE_ROOT/usr/bin/sentinel-server-$tool"
done

cat > "$PACKAGE_ROOT/usr/bin/sentinel-server" <<'EOF'
#!/bin/sh
# Команды администратора (create-admin, hikconnect-login…) от root выполняются от имени
# службы, чтобы база и секреты оставались у пользователя sentinel.
if [ "$(id -u)" = 0 ] && [ "${1:-run}" != run ] && id sentinel >/dev/null 2>&1; then
    export SENTINEL_DATA_DIR="${SENTINEL_DATA_DIR:-/var/lib/sentinel-server}"
    export SENTINEL_CREDENTIAL_STORE="${SENTINEL_CREDENTIAL_STORE:-file}"
    cd /
    exec runuser -u sentinel -- /usr/bin/python3 /usr/lib/sentinel-server/sentinel_server.py "$@"
fi
exec /usr/bin/python3 /usr/lib/sentinel-server/sentinel_server.py "$@"
EOF
chmod 0755 "$PACKAGE_ROOT/usr/bin/sentinel-server"

printf '/etc/default/sentinel-server\n/etc/xdg/autostart/io.sentinel.ServerTray.desktop\n' > "$PACKAGE_ROOT/DEBIAN/conffiles"

cat > "$PACKAGE_ROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
    if ! id sentinel >/dev/null 2>&1; then
        adduser --system --group --home /var/lib/sentinel-server --no-create-home sentinel >/dev/null
    fi
    install -d -o sentinel -g sentinel -m 0700 /var/lib/sentinel-server
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t /usr/share/icons/hicolor >/dev/null 2>&1 || true
    fi
    if [ -d /run/systemd/system ]; then
        systemctl daemon-reload >/dev/null 2>&1 || true
        if [ -z "${2:-}" ]; then
            # первая установка: сервер слушает только 127.0.0.1, пароль задаётся в окне настроек
            systemctl enable --now sentinel-server.service >/dev/null 2>&1 || true
        else
            systemctl try-restart sentinel-server.service >/dev/null 2>&1 || true
        fi
    fi
    echo "sentinel-server: откройте «Sentinel PRO Server» в меню приложений — появится значок в трее"
    echo "и окно, где задаются логин и пароль администратора. Без графики:"
    echo "  sudo sentinel-server reset-password    # логин admin, пароль спросит"
fi
exit 0
EOF
cat > "$PACKAGE_ROOT/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ -d /run/systemd/system ] && [ "$1" = remove ]; then
    systemctl stop sentinel-server.service >/dev/null 2>&1 || true
    systemctl disable sentinel-server.service >/dev/null 2>&1 || true
fi
exit 0
EOF
chmod 0755 "$PACKAGE_ROOT/DEBIAN/postinst" "$PACKAGE_ROOT/DEBIAN/prerm"

cat > "$PACKAGE_ROOT/DEBIAN/control" <<EOF
Package: sentinel-server
Version: ${VERSION}-1
Section: video
Priority: optional
Architecture: all
Maintainer: Sentinel contributors
Description: Sentinel PRO Server - security management server
 Headless service for Sentinel PRO: shared device catalog, users and roles, ISAPI event
 subscriptions, alarm rules, PC backup recording and P2P cloud account sync,
 exposed to Sentinel PRO clients over a REST API with Server-Sent Events.
 Includes a GTK 4 settings window (administrator password, network, TLS) and
 a system tray status icon with start/stop/restart controls.
Depends: python3 (>= 3.10), adduser
Recommends: ffmpeg, python3-gi, gir1.2-gtk-4.0, pkexec | policykit-1, openssl, libnotify-bin
EOF

dpkg-deb --build --root-owner-group "$PACKAGE_ROOT" "$OUTPUT"
printf 'Built %s\n' "$OUTPUT"
