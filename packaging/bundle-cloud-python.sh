#!/bin/sh
# Встраивает в AppImage отдельный Python 3.12 с библиотеками облачного видео P2P
# (hikcloudstream и pyezvizapi требуют Python 3.12+, а AppImage собирается на Ubuntu 22.04 с Python 3.10).
# Используется переносимая сборка python-build-standalone; помощник облачного потока запускается этим Python.
set -eu

TARGET=${1:?usage: bundle-cloud-python.sh TARGET_DIR}
PBS_RELEASE=20241016
PBS_FILE="cpython-3.12.7+${PBS_RELEASE}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
PBS_SHA256=3a4d53a7ba3916c0c1f35cbbe57068e2571b138389f29cf5c35367fec8f4c617
# Должно совпадать с RUNTIME_PACKAGES в sentinel_cloudstream.py (проверяется тестом).
PACKAGES="hikcloudstream==0.1.4 pyezvizapi==1.0.5.0"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
curl -fsSL --retry 4 -o "$WORK/python.tar.gz" \
    "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${PBS_FILE}"
printf '%s  %s\n' "$PBS_SHA256" "$WORK/python.tar.gz" | sha256sum -c -
tar -xzf "$WORK/python.tar.gz" -C "$WORK"
rm -rf "$TARGET"
mkdir -p "$(dirname -- "$TARGET")"
mv "$WORK/python" "$TARGET"

# shellcheck disable=SC2086
"$TARGET/bin/python3" -I -m pip install --disable-pip-version-check --no-cache-dir -q $PACKAGES
"$TARGET/bin/python3" -I -c "import hikcloudstream.stream.session, pyezvizapi.stream; \
from hikcloudstream import Camera, ClientConfig, HikConnectClient; \
from pyezvizapi.stream import StreamTransport, VtmChannel, detect_transport; print('cloud runtime ok')"
# pip и тесты стандартной библиотеки внутри AppImage не нужны.
"$TARGET/bin/python3" -I -m pip uninstall -y -q pip || true
rm -rf "$TARGET/lib/python3.12/test" "$TARGET/lib/python3.12/idlelib" "$TARGET/lib/python3.12/tkinter" \
       "$TARGET/include" "$TARGET/share"
