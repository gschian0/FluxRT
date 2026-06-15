#!/usr/bin/env bash
set -euo pipefail

pkill -f 'scripts/run_musicgen_radio_plus_musicGEN.py' || true
echo "Stopped musicgen radio generator (if running)."
