#!/bin/sh
set -eu

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
"$SOURCE_DIR/packaging/build-deb.sh"
"$SOURCE_DIR/packaging/build-appimage.sh"
