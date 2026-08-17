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

**The tray follows the work.** The daemon raises the icon itself when the box
goes busy, and an auto-raised icon leaves once the queue has been idle for
`TRAY_IDLE_LINGER`; `testq.py tray` now means "pin it up permanently". Upstream
the icon was something you started by hand and left running, which is a fine
arrangement for a tool that belongs to one project and a poor one here: nobody
should have to have started anything for the queue to be visible while some
other project's worktree is using the box. `start` copies `tray.ps1` into the
runtime directory alongside the snapshot, since the daemon must still find it
after the install it came from has been deleted.

This also fixes a latent bug inherited from upstream: the tray was spawned with
`DETACHED_PROCESS | CREATE_NO_WINDOW`, which Windows documents as mutually
exclusive, and when DETACHED wins `powershell.exe` has no console for its host
and exits 0 without running a line of the script — no window, no error, no icon.
`CREATE_NO_WINDOW` alone. Worth carrying back to any vendored copy.

**Comments and docs.** The design notes that said "the suite" or "this box's
worktrees" now name mfrs where they mean mfrs. `tray.ps1` keeps the
`mfrs-testq-tray-$Port` name for its global mutex, since preventing two trays is
the entire point of it and a vendored copy is still out there using that name —
which also means an old vendored tray and this one still exclude each other.

## Collapsing the fork

**In progress: mfrs PR #94** deletes `tools/testq/` and points `run_lib.sh` here
instead, by walking up from `$PROJ` for `_tools/testq/testq.py` (a walk rather
than a fixed `../..` because `$PROJ` is a worktree as often as it is the project
root, and those sit four directories deeper). Nothing else in its client
changes. Once that merges there is one copy of this file on the machine and this
document is history.

Until it merges nothing is broken either: both copies share the runtime
directory and the protocol, and whichever daemon is up serves both projects.
Note that an mfrs daemon serving the box is one that does not raise the icon,
since that half is only in this copy — one more reason to make sure this is the
one that wins the bind.

**Keep the legacy runtime directory support regardless of #94.** It is not about
the fork: `%LOCALAPPDATA%\mfrs-testq` is where the accumulated run history
actually lives on this box, and the scheduler's estimates are medians drawn from
it. Renaming the directory would throw that away for no gain.
