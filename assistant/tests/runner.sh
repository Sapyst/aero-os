#!/bin/sh
set -eu
here=$(dirname "$(readlink -f "$0")")
exec python3 "$here/runner.py" "$@"
