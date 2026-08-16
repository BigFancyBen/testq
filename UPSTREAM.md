# Where this diverges from mfrs, and how to collapse the fork

`testq.py` and `tray.ps1` here started as byte-identical copies of
`mfrs/tools/testq/` at commit `fea68b1` (mfrs master, PR #90 plus the two that
followed). Every change since is about the tool not belonging to one project.
None of them change the wire protocol, so `PROTO` is still 1 and mfrs's existing
vendored client works against this daemon unmodified.

## The changes

**Runtime directory.** `%LOCALAPPDATA%\mfrs-testq` → `%LOCALAPPDATA%\testq`, but
only for a fresh install: an existing `mfrs-testq` directory is still used when
it is there. Two copies of this file can both autostart, either can win the port
bind, and if they disagreed about where the history database lives the queue's
memory would depend on which one started first. `TESTQ_HOME` overrides.

**`projects.json`.** New, optional, in the runtime directory. Holds the two
things that were hardcoded to mfrs: where a project's Godot user directory is
(so `suite_eta` can find `weights-<tree>.json`) and which directories `reap` may
walk. An unregistered project queues exactly the same; it just falls back to
history medians for its estimates, which is where every non-suite job already
got them.

- `weights_dir()` takes a project name and returns "" when there is no entry.
- `suite_eta(tree_id, which, shards, project)` returns None rather than reading
  mfrs's `Middle Fork River Slop` userdata.
- `cmd_reap` iterates registered projects instead of a hardcoded `MFRS_ROOT`,
  gains `--project`, and refuses to delete anything when nothing is registered.
  The per-project half moved into `reap_candidates()`.

**A `project` field on `/acquire`.** Optional; carried on the ticket and shown
on the page as `project/worktree`. Two repos can both have a branch worktree
called `godot-fixes`, and the worktree column alone stopped being enough to tell
whose run you are looking at.

**`origin.txt`.** `start` records the absolute path of the file it was run from,
and the protocol-mismatch hint quotes it. "Restart the daemon" is useless advice
once more than one copy of `testq.py` exists and the running one serves from a
snapshot whose `__file__` names neither.

**`clients/testq.mjs`.** New. A Node client speaking the same protocol as
`run_lib.sh`'s bash one, because the projects that were not mfrs are Node.

**Comments and docs.** The design notes that said "the suite" or "this box's
worktrees" now name mfrs where they mean mfrs. `tray.ps1` is unchanged except
for two usage-comment paths; its global mutex deliberately keeps the
`mfrs-testq-tray-$Port` name, since preventing two trays is the entire point of
it and a vendored copy is still out there using that name.

## Collapsing the fork

The fork exists because mfrs vendors the daemon. Two ways out, in order of
preference:

1. **Point mfrs at this install.** In `run_lib.sh`, the autostart line

       python "$PROJ/tools/testq/testq.py" start --port "$TESTQ_PORT"

   becomes a lookup that prefers a shared install and falls back to the vendored
   copy, e.g.

       TESTQ_PY="${TESTQ_PY:-$PROJ/../_tools/testq/testq.py}"
       [ -f "$TESTQ_PY" ] || TESTQ_PY="$PROJ/tools/testq/testq.py"
       python "$TESTQ_PY" start --port "$TESTQ_PORT"

   mfrs can then delete `tools/testq/` whenever suits, and until it does, the
   only copy that ever starts a daemon is this one.

2. **Upstream these changes into mfrs and vendor from there.** They apply
   cleanly and are all additive. This is more work for the same result and
   leaves the queue owned by a project it outgrew.

Until either happens, nothing is broken: both copies share the runtime directory
and the protocol, and whichever daemon is up serves both projects. Keep this one
running — the tray, or a `shell:startup` shortcut — and mfrs's client will never
need to spawn its own, because it only autostarts when nothing answers on the
port.
