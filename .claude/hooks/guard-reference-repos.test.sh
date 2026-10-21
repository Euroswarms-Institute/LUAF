#!/usr/bin/env bash
#
# The guard's test battery. Run it after any change to the guard:
#   bash .claude/hooks/guard-reference-repos.test.sh
#
# Most of these are taken from commands this project actually ran. The reads
# matter more than the writes: an over-eager guard that refuses `mkdir -p
# <here>/x && cp <reference>/y <here>/x/` blocks the normal way code is
# vendored out of those trees, and that is how the first version of this guard
# was wrong.
set -uo pipefail

GUARD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/guard-reference-repos.sh"
pass=0
fail=0

check() {
  local exp="$1" cmdline="$2" cw="${3:-}" out got
  out="$(jq -nc --arg c "$cmdline" --arg w "$cw" \
    '{tool_name:"Bash",cwd:$w,tool_input:{command:$c}}' | bash "$GUARD")"
  got=allow
  [[ "$out" == *'"deny"'* ]] && got=deny
  if [[ "$got" == "$exp" ]]; then
    pass=$(( pass + 1 ))
  else
    fail=$(( fail + 1 ))
    printf 'FAIL want=%s got=%s  %s\n' "$exp" "$got" "$cmdline"
  fi
}

# --- reads, which must all keep working -----------------------------------
check allow 'git -C /home/user/DoublePlus log --oneline -3'
check allow 'git -C /home/user/DoublePlus status --short'
check allow 'git -C /home/user/Abiogenesis rev-parse HEAD'
check allow 'git -C /home/user/DoublePlus diff --stat'
check allow 'git -C /home/user/DoublePlus show HEAD:api/src/lib.ts'
check allow 'mkdir -p /home/user/LUAF/keeper && cp /home/user/DoublePlus/keeper/venture-regime.mjs /home/user/LUAF/keeper/'
check allow 'mkdir -p /home/user/LUAF/x /home/user/LUAF/y && cp -r /home/user/DoublePlus/aggregator/src /home/user/LUAF/x/'
check allow 'cd /home/user/DoublePlus && git log --oneline -3 2>&1'
check allow 'cd /home/user/DoublePlus && git status --short | head -5'
check allow 'cd /home/user/DoublePlus && grep -rn "pragma" contracts/contracts/v4 | head'
check allow 'cd /home/user/DoublePlus && cat README.md > /tmp/dp-readme.txt'
check allow 'cd /home/user/DoublePlus && git log > /dev/null'
check allow 'tar -czf /tmp/dp.tgz /home/user/DoublePlus'
check allow 'diff /home/user/DoublePlus/api/src/lib.ts /home/user/LUAF/service/src/auth.ts'
check allow 'cp /home/user/DoublePlus/keeper/venture-config.mjs /tmp/'
check allow 'ln -s /home/user/DoublePlus/contracts /home/user/LUAF/ref-contracts'
check allow 'wc -l /home/user/DoublePlus/contracts/test/v4/helpers/venture.ts'
check allow 'find /home/user/Abiogenesis -name "*.rs" | head'
check allow 'sed -n "1,40p" /home/user/DoublePlus/api/src/lucre.ts'
check allow 'cd /home/user/LUAF && git commit -m x'
check allow 'cd /home/user/DoublePlus && git log && cd /home/user/LUAF && git add -A'
check allow 'cd /home/user/LUAF && npm install'
check allow 'cd /home/user/LUAF && rm -rf node_modules'
check allow 'bash /home/user/DoublePlus/contracts/scripts/sync-doubleplus.sh'
check allow 'git log --oneline' '/home/user/DoublePlus'
check allow 'git push' '/home/user/LUAF'
check allow 'cd /home/user/LUAF && npm test'

# --- writes, which must all be refused ------------------------------------
check deny 'cd /home/user/DoublePlus && git add -A'
check deny 'cd /home/user/DoublePlus && git commit -m "x"'
check deny 'cd /home/user/DoublePlus && git push -u origin main'
check deny 'git -C /home/user/DoublePlus push'
check deny 'git -C /home/user/Abiogenesis commit -am wip'
check deny 'git -C /home/user/DoublePlus reset --hard'
check deny 'cd /home/user/Abiogenesis && git clean -fdx'
check deny 'cd /home/user/DoublePlus && git remote set-url origin git@x:y.git'
check deny 'cd /home/user/DoublePlus && git checkout -- .'
check deny 'echo hi > /home/user/DoublePlus/README.md'
check deny 'echo hi >> /home/user/Abiogenesis/notes.txt'
check deny 'cd /home/user/DoublePlus && echo hi > README.md'
check deny 'sed -i "s/a/b/" /home/user/DoublePlus/README.md'
check deny 'rm -rf /home/user/DoublePlus/aggregator'
check deny 'mv /home/user/DoublePlus/api/src/lucre.ts /tmp/x'
check deny 'cp /home/user/LUAF/x.ts /home/user/DoublePlus/api/src/x.ts'
check deny 'cd /home/user/DoublePlus && cp /tmp/x.ts api/src/x.ts'
check deny 'cd /home/user/DoublePlus && npm install'
check deny 'cd /home/user/DoublePlus/api && npm install --no-save viem'
check deny 'touch /home/user/Abiogenesis/x'
check deny 'mkdir -p /home/user/DoublePlus/newdir'
check deny 'cat x | tee /home/user/DoublePlus/y'
check deny 'chmod +x /home/user/DoublePlus/scripts/x.sh'
check deny 'tar -xzf /tmp/x.tgz -C /home/user/DoublePlus'
check deny 'cd /home/user/Abiogenesis && cargo build'
check deny 'git push' '/home/user/DoublePlus'

printf '%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
