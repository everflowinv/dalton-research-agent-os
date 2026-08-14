#!/bin/zsh
set -euo pipefail

dalton_root="$HOME/Library/Application Support/Dalton"
"$dalton_root/runtime/venv/bin/dalton-health" \
  --config "$dalton_root/config/service.json" \
  --max-age-seconds 45
