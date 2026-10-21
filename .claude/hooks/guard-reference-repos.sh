#!/usr/bin/env bash
#
# PreToolUse guard: DoublePlus and Abiogenesis are read freely, written never.
#
# Those repositories are where the venture stack is deployed from and where the
# selection rules were first written. They are read constantly -- vendoring out
# of them is normal work -- so reads must stay completely frictionless. Only
# changes to them are refused.
#
# The command is examined one segment at a time (split on && || ; | and
# newlines) rather than as one string, because a verb and a path appearing in
# the same command line says nothing about whether that verb targets that path.
#   mkdir -p <here>/x && cp <reference>/y <here>/x/
# is two segments: a write to this repo, and a read of a reference one. Judging
# the line as a whole refuses it, which is wrong -- that is how the vendored
# files got here.
#
# `cd` is tracked across segments, so `cd <reference> && git push` is caught
# even though the second segment names no path.
#
# This is a net, not a wall. Shell cannot be parsed reliably, so the half of
# the rule that does not depend on pattern-matching is the Edit deny in
# .claude/settings.json. The point here is to catch the ordinary mistake,
# loudly, before it reaches a remote.
set -uo pipefail

payload="$(cat)"
cmd="$(printf '%s' "$payload" | jq -r '.tool_input.command // empty' 2>/dev/null || true)"
cwd="$(printf '%s' "$payload" | jq -r '.cwd // empty' 2>/dev/null || true)"

REPOS='DoublePlus|Abiogenesis'

# The overwhelmingly common case: nothing to do with those repos at all.
if [[ ! "$cmd $cwd" =~ $REPOS ]]; then exit 0; fi

deny() {
  jq -nc --arg r "$1 DoublePlus and Abiogenesis are read-only here: read them freely, change nothing. Work in this repository, or copy what you need out of theirs." '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $r
    }
  }'
  exit 0
}

# Every git subcommand that writes the index, the worktree, refs, the config or
# a remote. Read-only subcommands -- log, show, diff, status, rev-parse,
# ls-files, cat-file, blame, grep, describe, for-each-ref, shortlog -- are not
# listed, so they are allowed against a reference repo like any other read.
GIT_WRITES='push|commit|add|rm|mv|checkout|switch|restore|reset|revert|merge|rebase|cherry-pick|stash|apply|am|clean|tag|branch|remote|config|init|clone|worktree|gc|prune|update-ref|update-index|symbolic-ref|notes|replace|filter-branch|filter-repo|submodule|fetch|pull|bisect|repack|send-email'

# Verbs whose every argument is a target: naming a reference repo at all is a
# write. `mv` included -- moving a file out of one of those trees changes it.
TARGET_VERBS='rm|rmdir|unlink|shred|mv|truncate|dd|chmod|chown|chgrp|touch|mkdir|patch|unzip'
# Verbs where only the destination is a write, so a reference repo as a source
# is just a read.
DEST_VERBS='cp|rsync|install|ln'
PKG_MANAGERS='npm|yarn|pnpm|bun|pip|pip3|poetry|cargo|go|hardhat|npx'

ecwd="$cwd"

# Split into segments. A `|` inside quotes splits wrongly, which can only ever
# produce a segment whose first word is not a verb -- it loses a catch rather
# than inventing one.
segments="$(printf '%s' "$cmd" | sed -E 's/(\&\&|\|\||;|\|)/\n/g')"

while IFS= read -r seg; do
  # trim
  seg="${seg#"${seg%%[![:space:]]*}"}"
  seg="${seg%"${seg##*[![:space:]]}"}"
  [[ -n "$seg" ]] || continue

  # Track the working directory so a later bare command is judged in context.
  if [[ "$seg" =~ ^cd[[:space:]]+([^[:space:]]+) ]]; then
    ecwd="${BASH_REMATCH[1]}"
    ecwd="${ecwd%\"}"; ecwd="${ecwd#\"}"; ecwd="${ecwd%\'}"; ecwd="${ecwd#\'}"
    continue
  fi

  in_repo=0
  [[ "$ecwd" =~ $REPOS ]] && in_repo=1
  names_repo=0
  [[ "$seg" =~ $REPOS ]] && names_repo=1
  # A segment that neither names a reference repo nor runs inside one cannot
  # touch one.
  (( in_repo || names_repo )) || continue

  # shellcheck disable=SC2206
  words=( $seg )
  # Skip env assignments and wrappers to reach the real command.
  i=0
  while (( i < ${#words[@]} )); do
    w="${words[$i]}"
    if [[ "$w" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || [[ "${w##*/}" =~ ^(sudo|env|command|nohup|time|timeout|xargs|then|else|do)$ ]]; then
      i=$(( i + 1 ))
    else
      break
    fi
  done
  verb="${words[$i]:-}"
  verb="${verb##*/}"
  args=( "${words[@]:$(( i + 1 ))}" )
  last="${words[${#words[@]} - 1]}"

  # --- redirection ------------------------------------------------------
  # `2>&1` and friends are not file writes, so they are stripped first.
  bare="$(printf '%s' "$seg" | sed -E 's/[0-9]*>&[0-9-]+//g')"
  if [[ "$bare" =~ (\>\>?|\>\|)[[:space:]]*([^[:space:]]+) ]]; then
    tgt="${BASH_REMATCH[2]}"
    if [[ "$tgt" =~ $REPOS ]]; then
      deny "Refused: that redirects output into a reference repository."
    fi
    # A relative target inside a reference repo writes there too. /dev/null and
    # absolute paths elsewhere do not.
    if (( in_repo )) && [[ "$tgt" != /* ]]; then
      deny "Refused: that redirects output into a reference repository."
    fi
  fi

  case "$verb" in
    git)
      # First non-option word after git, skipping -C/-c/--git-dir/--work-tree
      # and their arguments.
      j=0
      sub=""
      while (( j < ${#args[@]} )); do
        a="${args[$j]}"
        case "$a" in
          -C|-c|--git-dir|--work-tree|--namespace|--exec-path) j=$(( j + 2 )); continue ;;
          -*) j=$(( j + 1 )); continue ;;
          *) sub="$a"; break ;;
        esac
      done
      if [[ -n "$sub" && "$sub" =~ ^($GIT_WRITES)$ ]]; then
        deny "Refused: \`git $sub\` would change a reference repository."
      fi
      ;;
    tee)
      deny "Refused: tee would write into a reference repository."
      ;;
    sed|perl|ruby)
      for a in "${args[@]}"; do
        if [[ "$a" =~ ^-[a-zA-Z]*i ]]; then
          deny "Refused: that edits a reference repository in place."
        fi
      done
      ;;
    tar)
      for a in "${args[@]}"; do
        if [[ "$a" =~ ^-?[a-zA-Z]*x ]]; then
          deny "Refused: that extracts into a reference repository."
        fi
      done
      ;;
  esac

  if [[ "$verb" =~ ^($TARGET_VERBS)$ ]]; then
    deny "Refused: \`$verb\` targets a reference repository."
  fi

  if [[ "$verb" =~ ^($DEST_VERBS)$ ]]; then
    # Only the destination is a write; a reference repo as a source is a read.
    if [[ "$last" =~ $REPOS ]] || { (( in_repo )) && [[ "$last" != /* ]]; }; then
      deny "Refused: that would write into a reference repository."
    fi
  fi

  if [[ "$verb" =~ ^($PKG_MANAGERS)$ ]]; then
    for a in "${args[@]}"; do
      if [[ "$a" =~ ^(install|ci|add|remove|uninstall|update|upgrade|publish|link|compile|build|mod|get)$ ]]; then
        deny "Refused: that would install or build inside a reference repository."
      fi
    done
  fi
done <<< "$segments"

exit 0
