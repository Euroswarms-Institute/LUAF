#!/usr/bin/env bash
#
# PreToolUse guard: DoublePlus and Abiogenesis are reference-only.
#
# Those repositories are where the venture stack is deployed from. This one is
# the only one this venture may write to, and a session that is handed all
# three will happily commit to the wrong one -- which is exactly what happened
# before this guard existed.
#
# Reads must stay frictionless, because vendoring from those trees is a normal
# part of the work: `cat`, `grep`, `git log`, and copying a file OUT of them are
# all allowed. What is refused is anything that changes them.
#
# This is a net, not a wall. Shell cannot be parsed reliably, so a determined
# command could slip past; the deterministic half of the rule is the Edit/Write
# deny in .claude/settings.json, which no shell quoting gets around. The point
# here is to catch the ordinary mistake, loudly, before it reaches a remote.
set -uo pipefail

payload="$(cat)"
cmd="$(printf '%s' "$payload" | jq -r '.tool_input.command // empty' 2>/dev/null || true)"
# cwd, when the runtime supplies it, catches a bare `git push` left running in a
# directory an earlier command changed into.
cwd="$(printf '%s' "$payload" | jq -r '.cwd // empty' 2>/dev/null || true)"

REPOS='DoublePlus|Abiogenesis'

# The overwhelmingly common case: nothing to do with those repos at all.
if [[ ! "$cmd $cwd" =~ $REPOS ]]; then exit 0; fi

deny() {
  jq -nc --arg r "$1" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $r
    }
  }'
  exit 0
}

reason() {
  printf '%s DoublePlus and Abiogenesis are reference-only in this session: read them freely, change nothing. Work in this repository instead, or copy what you need out of theirs.' "$1"
}

# --- git ------------------------------------------------------------------
# Every git subcommand that writes anything: the index, the worktree, refs, the
# config, or a remote. Read-only subcommands (log, show, diff, status,
# rev-parse, ls-files, cat-file, blame, grep, describe, ...) are not listed and
# so stay allowed.
GIT_WRITES='push|commit|add|rm|mv|checkout|switch|restore|reset|revert|merge|rebase|cherry-pick|stash|apply|am|clean|tag|branch|remote|config|init|clone|worktree|gc|prune|update-ref|update-index|symbolic-ref|notes|replace|filter-branch|filter-repo|submodule|fetch|pull|bisect|reflog|fsck|repack|send-email|format-patch|request-pull'

if [[ "$cmd" =~ (^|[^[:alnum:]_-])git([[:space:]]|$) ]]; then
  # Subcommand of each `git` invocation, skipping any leading options and the
  # argument of -C / --git-dir / --work-tree / -c.
  subs="$(printf '%s\n' "$cmd" \
    | grep -oE '\bgit([[:space:]]+(-C[[:space:]]+[^[:space:]]+|--git-dir[=[:space:]][^[:space:]]+|--work-tree[=[:space:]][^[:space:]]+|-c[[:space:]]+[^[:space:]]+|-[^[:space:]]+))*[[:space:]]+[a-z][a-z-]*' \
    | awk '{ print $NF }' | sort -u || true)"
  while IFS= read -r sub; do
    [[ -n "$sub" ]] || continue
    if [[ "$sub" =~ ^($GIT_WRITES)$ ]]; then
      deny "$(reason "Refused: \`git $sub\` would change a reference repository.")"
    fi
  done <<< "$subs"
fi

# --- redirection and in-place edits ---------------------------------------
if [[ "$cmd" =~ (\>\>?|\>\|)[[:space:]]*[^[:space:]\;\&\|]*($REPOS) ]]; then
  deny "$(reason 'Refused: that redirects output into a reference repository.')"
fi
if [[ "$cmd" =~ (^|[^[:alnum:]_-])(sed|perl|ruby)([[:space:]]+[^[:space:]]+)*[[:space:]]+-[a-zA-Z]*i ]]; then
  deny "$(reason 'Refused: that edits a reference repository in place.')"
fi
if [[ "$cmd" =~ (^|[^[:alnum:]_-])tee([[:space:]]|$) ]]; then
  deny "$(reason 'Refused: tee would write into a reference repository.')"
fi

# --- commands that destroy or move ----------------------------------------
# Named anywhere in the command, because the path is the target either way --
# and moving a file out of one of these trees changes it just as much.
for verb in rm rmdir unlink shred mv truncate dd chmod chown chgrp touch mkdir ln patch; do
  if [[ "$cmd" =~ (^|[^[:alnum:]_-])${verb}([[:space:]]|$) ]]; then
    deny "$(reason "Refused: \`$verb\` names a reference repository.")"
  fi
done

# --- copies, where only the destination matters ---------------------------
# `cp <reference>/x <here>/y` is a read and must keep working; it is how the
# vendored files got here. Only a reference repo as the LAST argument -- the
# destination -- is a write.
if [[ "$cmd" =~ (^|[^[:alnum:]_-])(cp|rsync|install|mvn|unzip|tar)([[:space:]]|$) ]]; then
  last="$(printf '%s' "$cmd" | awk '{ print $NF }')"
  if [[ "$last" =~ $REPOS ]]; then
    deny "$(reason 'Refused: that copies into a reference repository.')"
  fi
fi

# --- package managers -----------------------------------------------------
# An install rewrites node_modules and can rewrite a lockfile, which is a
# tracked change. Earlier in this project it did exactly that.
if [[ "$cmd" =~ (^|[^[:alnum:]_-])(npm|yarn|pnpm|bun|pip|pip3|poetry|cargo|go)([[:space:]]+[^[:space:]]+)*[[:space:]]+(install|ci|add|remove|uninstall|update|upgrade|build|get|mod|publish|link) ]]; then
  deny "$(reason 'Refused: that would install or build inside a reference repository.')"
fi

exit 0
