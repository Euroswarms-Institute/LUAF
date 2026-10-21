#!/usr/bin/env bash
# Re-vendor the DoublePlus venture contracts the Launcher builds on and is
# tested against. Usage: contracts/scripts/sync-doubleplus.sh [path-to-DoublePlus]
set -euo pipefail
ROOT="${1:-../DoublePlus}/contracts"
SRC="$ROOT/contracts"
DST="$(cd "$(dirname "$0")/.." && pwd)/contracts/vendor/doubleplus"
TESTS="$(cd "$(dirname "$0")/.." && pwd)/test/helpers"
[ -d "$SRC/v4/venture" ] || { echo "no DoublePlus contracts at $SRC" >&2; exit 1; }
rm -rf "$DST"
mkdir -p "$DST/v4/venture" "$DST/v4/interfaces" "$DST/v4/test" "$DST/mocks"
cp "$SRC"/v4/venture/*.sol "$DST/v4/venture/"
cp "$SRC/v4/QuiverToken.sol" "$DST/v4/"
cp "$SRC/v4/interfaces/IQuiverToken.sol" "$DST/v4/interfaces/"
# HookDeployer lives under v4/test/ upstream but the production deploy needs it:
# it is the CREATE2 deployer that places the hook at an address whose low bits
# encode its V4 permission flags. Leaving it out made the whole deploy path fail
# at step one, after the FeeSplitter had already been paid for.
cp "$SRC/v4/test/HookDeployer.sol" "$DST/v4/test/"
cp "$SRC/mocks/WETH9.sol" "$SRC/mocks/EthRejecter.sol" "$DST/mocks/"
# The stack's own test fixture, so the suites here deploy the real contracts
# rather than a local approximation of them. Its provenance header is restored
# after the copy, because the upstream file does not carry one.
mkdir -p "$TESTS"
cp "$ROOT/test/v4/helpers/venture.ts" "$TESTS/venture.ts"
python3 - "$TESTS/venture.ts" <<'EOF'
import pathlib, sys
p = pathlib.Path(sys.argv[1]); s = p.read_text()
note = """/** Vendored from DoublePlus contracts/test/v4/helpers/venture.ts. Do not edit
 *  here; re-run scripts/sync-doubleplus.sh. It deploys the real venture stack,
 *  which is what makes the suites below integration tests of the contracts the
 *  Launcher and the syndicate will actually meet rather than of mocks. */

"""
if "Vendored from DoublePlus contracts/test" not in s:
    p.write_text(note + s)
EOF
rev="$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)"
printf 'Vendored from DoublePlus contracts/contracts at %s. Do not edit; re-run scripts/sync-doubleplus.sh.\n' "$rev" > "$DST/VENDORED.md"
echo "vendored DoublePlus @ $rev into $DST"
