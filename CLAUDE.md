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

Anything that needs a deployment artifact takes its path from the environment
(`TENANT_DEPLOYMENT`, `VENTURE_DEPLOYMENT`, `DEPLOYMENT_FILE`) rather than
guessing a filename from a chain id: one chain can carry several stacks, and a
tool pointed at the wrong one is wrong silently.

The deploy path is `DEPLOY.md` plus `contracts/scripts/`. Four of the addresses
a deploy sets are immutable, so `preflight.ts` runs every check that can be made
before gas is spent and `verify-deployment.ts` reads the result back off the
chain afterwards. Rehearse against a forked node before touching a real one; the
scripts detect a development node and mark the artifact `fork: true`.

## Frozen: do not extend these

Nothing here is deployed and nothing has earned a wei. Two parts of this repo
are a year ahead of the business and are **frozen** — they work, they are
tested, and they are not to be grown until there is real money to point them
at.

| Frozen | Why | Unfreezes when |
| --- | --- | --- |
| `core/src/selection.ts` and `core/src/persistent.ts` — the breeder | It optimises launch formats against fee income that has never been collected. You cannot breed against a number that does not exist. | Real fee income exists from launched coins |
| `aggregator/public/terminal.html`, `service/public/console.html` | Three redesigns before a single person has used either | Somebody has actually used one and said what is wrong |
| `service/src/tenants.ts` budgets and grants | An accounting system for tenants who do not exist | There is a second tenant |

Frozen means: fix a bug, do not add a feature, and do not refactor for
elegance. If a task seems to need one of these extended, that is a signal the
task is premature — say so instead of building it.

What is *not* frozen, because it is load-bearing before any money moves: the
contracts (immutable once deployed, so they have to be right first), the deploy
path and its preflight, and the aggregator's indexing (the router needs an index
of other people's coins to be worth routing through).

## Commits

Author every commit as `ilumCI <ilumbackup@gmail.com>`. No co-author trailers,
no session links, no tool attribution of any kind in commit messages, PR bodies
or code comments.

Develop on the branch the session names. Do not open a pull request unless you
are asked for one.
