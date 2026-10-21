# Working in this repository

## This is the only repository you may change

A session is often handed three checkouts. Only this one is writable.

| | |
| --- | --- |
| `LUAF` (Lucreocracy) | **yours.** Develop, commit and push here. |
| `DoublePlus` | **reference only.** Read it; change nothing, ever. |
| `Abiogenesis` | **reference only.** Read it; change nothing, ever. |

DoublePlus is where the venture stack is deployed from, and Abiogenesis is
where the selection rules were first written. Both are read constantly and
written never. This is not a style preference: a session that was given
branches in all three committed and pushed nine commits to DoublePlus before
anyone noticed, which is why the rule is now enforced in code rather than
remembered.

**Read them freely.** They are in `additionalDirectories`, so reads are in
scope and unrestricted: `cat`, `grep`, `git log`, `git -C <repo> show`, copying
a file *out* of them — all normal, all expected. Nothing here denies a read,
and nothing should: vendoring out of those trees is part of the work.

Two things stop the writes, and neither replaces the other:

- `.claude/settings.json` denies the file-writing tools on those paths. It
  denies no reads. This half is deterministic and no shell quoting gets around
  it.
- `.claude/hooks/guard-reference-repos.sh` refuses Bash commands that would
  change them — git subcommands that write, redirection into them, `rm`, `mv`,
  in-place `sed`, `tee`, installs, and a copy whose *destination* is in them.

  The hook judges each command segment separately and tracks `cd`, because a
  verb and a path on the same line say nothing about whether that verb targets
  that path: `mkdir -p <here>/x && cp <reference>/y <here>/x/` is a write here
  and a read there. Scanning the line as a whole refuses it, which is wrong.
  There are 53 cases in `.claude/hooks/guard-reference-repos.test.sh` — 27 of
  them reads that must keep working. Run it after any change to the guard.

  It is a net rather than a wall: shell cannot be parsed reliably, so treat it
  as a catch for the ordinary mistake, not a boundary.

If something genuinely needs to change in one of those repositories, say so and
let the person do it. Do not work around the guard.

**Repository scope is fixed when a session starts.** Narrowing it is done at
launch, by selecting only this repository, not from inside a running session.

## Vendored code

`contracts/contracts/vendor/doubleplus/` and `contracts/test/helpers/venture.ts`
are copies of DoublePlus files, so the contracts here are tested against the
ones they will actually meet. Do not edit them in place — re-pin with
`contracts/scripts/sync-doubleplus.sh [path-to-DoublePlus]`, which reads that
tree and writes only into this one.

Anything that needs a venture deployment artifact takes its path from the
environment (`VENTURE_DEPLOYMENT`, `DEPLOYMENT_FILE`) rather than guessing a
filename, because those artifacts are written by that repository and not by
this one.

## Commits

Author every commit as `ilumCI <ilumbackup@gmail.com>`. No co-author trailers,
no session links, no tool attribution of any kind in commit messages, PR bodies
or code comments.

Develop on the branch the session names. Do not open a pull request unless you
are asked for one.
