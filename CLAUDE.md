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

Two things enforce it, and neither replaces the other:

- `.claude/settings.json` denies the file-writing tools on those paths. This is
  deterministic and no shell quoting gets around it.
- `.claude/hooks/guard-reference-repos.sh` refuses Bash commands that would
  change them — git writes, redirection, `rm`, `mv`, in-place `sed`, installs.
  Reads stay frictionless, including copying a file *out* of those trees, which
  is how the vendored code got here. It is a net rather than a wall: shell
  cannot be parsed reliably, so treat it as a catch for the ordinary mistake.

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
