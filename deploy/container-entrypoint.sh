#!/bin/sh
set -eu
# A copied host configuration must not make a newer image report an old release.
VERSION="$(cat /usr/local/share/zerohome-version)"
export VERSION
exec "$@"
