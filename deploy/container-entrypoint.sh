#!/bin/sh
set -eu
# The image version marker is metadata for operators, never application config.
# Remove a stale value inherited through Compose env_file as well.
unset VERSION
exec "$@"
