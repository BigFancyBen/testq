# testq

One queue for every Godot run on this machine, belonging to no project.

## Why

There is one box, one GPU, and about four engines' worth of real capacity —
mfrs's `run_test_par.sh` records the measurement: six and eight shards each ran
1.5–1.7× slower than four and finished later overall. Against that there are
several projects and a couple of dozen worktrees between them, each of which
thinks it is alone on the machine.

Two sessions starting `run_test_par.sh 4` in the same minute put eight engines
on a four-engine box, and everything that asserts against the wall clock starts
flipping: the generation budget, `Sfx.warm()`, and above all `run_mp.sh`, whose
whole verdict is a clock-skew measurement between two live processes. Those
failures read exactly like real ones. Several of them have reached pull
requests.

The scripts were already safe against *collision* — OS-assigned ports, output
directories keyed on the checkout, scratch keyed on the invocation. None of
that helps with *contention*. This is the missing half: a daemon holds the
slots and the run scripts ask before they launch.

**It sits outside every project because the contention does.** A warehouse
capture in the DJ app and a physics suite in the game are two engines on one
box, and neither repo can see the other's. A queue living inside one of them can
only manage half the load — and the half it cannot see is exactly where the
damage came from: prognosticator's capture script used to clear the box with
`taskkill /IM Godot....exe`, which killed every engine in every mfrs worktree
too, and at that end it read as a crashed test run with no error in it
(prognosticator issue #9).

This copy started as mfrs's `tools/testq/` and is byte-identical to it apart
from the changes listed in [UPSTREAM.md](UPSTREAM.md) — all of which are about
not being one project's tool any more.

## Install

Nothing to install: Python 3, standard library only, and the daemon autostarts
the first time a client asks for it.

    python testq.py start             # usually unnecessary
    python testq.py start --restart   # after editing testq.py
    python testq.py status
    python testq.py stats --days 7
    python testq.py stop [--force]
    python testq.py tray              # pin the icon up (it appears by itself)
    python testq.py reap              # delete scratch of deleted worktrees

State, the history database and the daemon's own snapshot live in
`%LOCALAPPDATA%\testq\` — or in `%LOCALAPPDATA%\mfrs-testq\` when that older
directory exists, which on this box it does. Keep it that way: that directory
holds every run this machine has recorded, and the scheduler's estimates are
medians drawn from it. `TESTQ_HOME` overrides.

mfrs is losing its vendored copy in its PR #94, which points `run_lib.sh` here.
Until that merges there are two copies of `testq.py` on the box and either can
win the port bind — harmless, because they share the runtime directory and the
protocol, though only this one raises the tray icon. Keep this one running (a
shortcut to `testq.py start` in `shell:startup` is enough) and mfrs will never
spawn its own: its client only autostarts a daemon when nothing answers on the
port.

## Using it from a project

The client is the whole integration: ask before launching an engine, give the
slot back afterwards.

**Node** — vendor `clients/testq.mjs` into the project (the note at the top of
that file says why it is copied rather than imported across an absolute path):

```js
import { acquire } from './lib/testq.mjs';

const slot = await acquire({
  project: 'prognosticator',        // key into projects.json, optional
  script: 'capture-warehouse.mjs',  // what to call this on the page
  arg: 'warehouse',
  treePath: ROOT,                   // the checkout, for the worktree column
  gpu: 1,                           // it opens a window
  engines: 1,
});
// ...launch the engine...
await slot.release(exitCode);
```

**Bash** — mfrs's `run_lib.sh` carries `testq_acquire` / `testq_release`, which
speak the same protocol over `curl`.

The one rule in either language: acquire BEFORE any engine starts, including an
import pass, and give the slot back when the last engine exits. A run that has
to wait says so:

    [testq] the box is busy -- queued at position 2 (waiting for slots).
    [testq] nothing has launched yet; watch http://localhost:43117/
    [testq] got the box after 184s -- starting

A slot that is never released is not leaked. The daemon tracks each client by
(pid, creation time) and reclaims a dead one's slots within about five seconds,
so a script killed outright — or one whose failure path calls `exit` — costs the
queue nothing. Release explicitly anyway when you can: it is immediate, and it
records the exit code.

## The page

**<http://localhost:43117/>** is the point of the thing: what is running, in
which project and worktree, for how long against its estimate; what is queued
and what it is waiting for; anything running outside the queue; and the last
fifty jobs with their exit codes. Each row has a cancel button that really does
stop the work (see below). `python testq.py status` is the same thing in the
terminal.

## The history database

Every finished job is written to `testq.db` in the runtime directory, a SQLite
file with one `runs` table: which worktree, which script and argument, how long
it ran, how long it waited, how it exited, and the most engines the box was
carrying while it ran. It survives daemon restarts, worktree deletions and
reboots, so it accumulates across sessions.

    python testq.py stats --days 7 [--tree NAME]

gives engine hours, time lost to queueing, per-job medians and worst cases,
per-worktree totals, and — the useful one — a list of jobs that *sometimes*
pass and sometimes do not, which is the expensive kind of problem here and
easy to miss one run at a time.

It is a plain SQLite file, so anything can read it:

```sql
-- is the suite getting slower on this branch?
SELECT date(finished,'unixepoch','localtime') d, COUNT(*) n, AVG(dur_s)
FROM runs WHERE script='run_test_par.sh' AND verdict='released'
GROUP BY d ORDER BY d;
```

The medians here are also what the queue schedules with: shortest-job-first
needs an estimate, and for anything with no `weights-*.json` entry — `run_mp`,
clips, screenshots, captures, or any worktree that has not yet run the full
suite — the estimate is the median of that job's own recent history, preferring
this worktree's runs and falling back to every worktree's.

An older JSONL history file is imported once on first start and renamed to
`history.jsonl.imported`; nothing measured is thrown away.

## projects.json

Optional, in the runtime directory. Everything in it is a convenience: a project
that registers nothing still queues correctly, and still gets estimates once it
has run the same job twice.

```json
{
  "mfrs": {
    "root": "C:/Users/Tango/Documents/projects/mfrs",
    "userdata": "%APPDATA%/Godot/app_userdata/Middle Fork River Slop",
    "scratch": "%TEMP%/mfrs",
    "scratch_subdirs": ["clips", "shots"],
    "weights": "test"
  },
  "prognosticator": { "root": "C:/Users/Tango/Documents/projects/prognosticator" }
}
```

`userdata` + `weights` point at a directory of `weights-<TREE_ID>.json` files —
the tag → milliseconds map a suite publishes — which is what lets a brand new
worktree get a real estimate before it has any history of its own. `root`,
`scratch` and `scratch_subdirs` are what `reap` walks, and it only ever looks at
directories a project has named: a reaper that guesses where scratch lives is a
reaper that deletes somebody's work.

## The tray icon

**There is nothing to start.** The icon appears in the notification area
whenever the box is busy — any project, any worktree, whether the run was
started by you or by an agent — and takes itself away again a minute and a half
after the queue goes quiet. Any project's first `acquire` starts the daemon, and
the daemon raises the icon.

Hover for a one-line summary, left-click to open the page, right-click for the
running and queued jobs in full. The icon itself is the status: grey idle, blue
running, amber something waiting, purple engines on the box that the queue did
not start, red if the last job to finish did not pass — and a balloon when one
fails. The linger after the queue empties is what leaves that red dot and its
balloon on screen long enough to be read.

Windows 11 files new icons under the overflow chevron by default. Drag it onto
the taskbar to see it without opening the chevron — Windows remembers that
per-icon, so it holds for the ones raised on later runs too.

Unmanaged engines deliberately do not keep the icon up. An editor left open all
afternoon is an engine the queue did not start, and pinning a permanent icon on
that would make it furniture again — it still turns the icon purple while
something else is running.

    python testq.py tray          # pin it up permanently
    python testq.py tray --stop   # and take it down again

is there for when you want it up regardless: an icon asked for by hand stays
until it is dismissed, which is also true of "Hide this icon" on its menu —
dismiss it mid-run and it stays gone until the queue has gone quiet and come
back. There is no need for a `shell:startup` shortcut any more, though one is
still the surest way to make this install, rather than a project's vendored
copy, the daemon that ends up serving.

It is a PowerShell script (`tray.ps1`) using the tray API Windows already has,
not an Electron app. Everything the tray needs to do — sit in the overflow
area, stay out of the way, open on click, summarise on hover — is native
behaviour, and Electron would have added a couple of hundred megabytes of Node
and a second long-running process to a tool whose selling point is that it
installs nothing. It opens the same page either way.

### Environment

| Variable | Default | Meaning |
|---|---|---|
| `TESTQ` | `on` | `off` bypasses the queue entirely; `require` fails rather than run unqueued |
| `TESTQ_PORT` | `43117` | outside 27015–27022, which mfrs's `run_shots.sh ui` photographs |
| `TESTQ_AUTOSTART` | `1` | `0` to never spawn a daemon |
| `TESTQ_HOME` | — | runtime directory override |
| `TESTQ_PY` | — | path to `testq.py`, if a Node client's autostart cannot find it |
| `TESTQ_GODOT_MATCH` | `godot_v4` | image-name fragment used to spot engines |

Use `TESTQ=require` when a number has to be trustworthy — a performance
measurement you intend to quote. Use `TESTQ=off` when the queue itself is what
you are debugging.

## What each job books

The unit is the **script invocation**, never the engine process: `run_mp` needs
two engines alive at once and `run_test_par` wants N, so a per-process
semaphore of size one would deadlock both on the first call.

| Job | CPU | GPU | Engines | Notes |
|---|---|---|---|---|
| mfrs `run_test.sh` | 1 | – | 1 | |
| mfrs `run_test_par.sh N` | min(N,4) | – | N | asking for more than four is already documented as net-worse |
| mfrs `run_mp.sh` | **all** | **all** | 2 | exclusive: its verdict is only meaningful on a quiet box |
| mfrs `run_shots.sh` | 1 | 1 | 1 | windowed; `ui` also takes the `ports:27015` mutex |
| mfrs `run_clip.sh` | 1 | 1 | 1 | takes `clip:<tree>:<scenario>`, closing the same-scenario frame-dir race |
| mfrs `run_attach.sh` | – | – | – | no engine, never queued |
| prognosticator `capture-warehouse.mjs` | 1 | 1 | 1 | windowed; releases when the engine exits, not when ffmpeg does |
| prognosticator `godot-export-web.js` | 1 | – | 1 | two headless passes, one after the other |

Queue order is **shortest first**, estimated from the `weights-<tree>.json` a
suite writes, so a twenty-second tagged slice does not sit behind two
eleven-minute suites. Nothing starves: a job that has waited longer than
`max(600 s, 2× its own estimate)` ages to the front, and while a large job is
at the head its unmet slots are reserved so a stream of small ones cannot keep
it out. A job blocked only on the GPU still lets CPU-only work past it.

## Design notes worth knowing

**It never launches or pools a Godot process.** A warm engine would show up in
`run_mp`'s stray count and condemn every multiplayer run to INCONCLUSIVE
forever.

**Engines it did not grant still count.** Worktrees without the client, and the
editor, are real load. The daemon counts what is actually on the box, subtracts
what it granted, and docks capacity by the difference (debounced over two
samples). That one rule also stops a restarted daemon from overgranting on top
of the previous one's orphans, and it is why the page can show "2 unmanaged
engines".

**Leader election is the port bind**, with `SO_EXCLUSIVEADDRUSE` set before
bind. There is no lock file to go stale, and a second daemon exits quietly.

**The daemon never runs from a project.** `start` copies the file to
`<runtime dir>/testq-<sha>.py` and serves from there, recording where it came
from in `origin.txt`; `serve` refuses to run from anywhere else. Editing this
file, or deleting the checkout it lives in, cannot tear a running daemon out
from under itself. `origin.txt` is also how a client that gets a protocol
mismatch can be told which copy to restart, now that there can be more than one.

**Liveness is (winpid, creation time).** Bash's `$$` is an MSYS pid that no
Windows API has heard of, so the bash client sends `/proc/$$/winpid`; Node's
`process.pid` is already the Windows one. The creation time is there because
Windows reuses pids briskly and a killed run's slots must not be held open by
whatever inherited its number. A client killed outright is reclaimed within
about five seconds without its EXIT trap ever running.

**Cancelling takes several passes.** `taskkill /F /T` on the run's shell is not
enough — Git-Bash nests a second `timeout.exe` between shell and engine, the
console build of Godot launches the real one as its own child, and the chain
re-parents as its links die. A single sweep reliably left two engines running
after the page said the job was cancelled. So the kill re-scans and repeats
until the worktree is clear, matching the **image name** for Godot and the
**command line** for the worktree. Both halves matter: matching Godot in the
command line instead would also match shells that merely mention the engine,
and a blanket kill by image name is the failure this whole tool sits downstream
of.

**Queue time is free.** Every outer `timeout` in the run scripts wraps only its
engine invocation, and the engine-side watchdogs start with the engine, so a
job that waits an hour still gets its full 1500 s. All blocking happens before
launch, which is also why a client acquires before `godot_import` — the import
is itself an engine.

**The queue cannot break your tests.** If the daemon is unreachable and cannot
be started, the run goes ahead unqueued with a loud warning. Infrastructure
that can stop you testing is worse than the contention it was built to prevent.

## Testing the queue itself

`run_mp.sh` in mfrs is the regression test. Its stray count and INCONCLUSIVE
verdict were left exactly as they were; under a working queue it measures zero
and never fires. An INCONCLUSIVE from a queued run means the queue let something
through.
