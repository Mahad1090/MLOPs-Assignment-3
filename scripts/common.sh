#!/usr/bin/env bash
# Shared setup for the service scripts: repo root, PATH and variables from .env.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH"
if [ -f .env ]; then set -a; . ./.env; set +a; fi
