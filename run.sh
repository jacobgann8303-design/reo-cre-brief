#!/usr/bin/env bash
# Local run — same three steps the cloud workflow does.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
python3 collect/gather.py --verbose
python3 brief/generate.py
python3 deliver/push.py
echo
echo "Done. Read it:  open docs/index.html"
