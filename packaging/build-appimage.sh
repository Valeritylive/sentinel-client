#!/bin/sh
set -eu

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=$(cat "$SOURCE_DIR/VERSION")

if ! command -v appimage-builder >/dev/null 2>&1; then
    printf 'appimage-builder is required to build the AppImage.\n' >&2
    exit 1
fi

cd "$SOURCE_DIR"
APP_VERSION="$VERSION" appimage-builder --recipe "$SOURCE_DIR/packaging/AppImageBuilder.yml"
