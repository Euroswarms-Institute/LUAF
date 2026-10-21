#!/usr/bin/env bash
# Re-vendor the DoublePlus venture contracts the Launcher builds on and is
# tested against. Usage: contracts/scripts/sync-doubleplus.sh [path-to-DoublePlus]
set -euo pipefail
SRC="${1:-../DoublePlus}/contracts/contracts"
DST="$(cd "$(dirname "$0")/.." && pwd)/contracts/vendor/doubleplus"
[ -d "$SRC/v4/venture" ] || { echo "no DoublePlus contracts at $SRC" >&2; exit 1; }
rm -rf "$DST"
mkdir -p "$DST/v4/venture" "$DST/v4/interfaces" "$DST/mocks"
cp "$SRC"/v4/venture/*.sol "$DST/v4/venture/"
cp "$SRC/v4/QuiverToken.sol" "$DST/v4/"
cp "$SRC/v4/interfaces/IQuiverToken.sol" "$DST/v4/interfaces/"
cp "$SRC/mocks/WETH9.sol" "$SRC/mocks/EthRejecter.sol" "$DST/mocks/"
rev="$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)"
printf 'Vendored from DoublePlus contracts/contracts at %s. Do not edit; re-run scripts/sync-doubleplus.sh.\n' "$rev" > "$DST/VENDORED.md"
echo "vendored DoublePlus @ $rev into $DST"
