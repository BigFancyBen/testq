/**
 * testq client for Node projects. Ask before you launch an engine.
 *
 * The canonical copy of this file lives in the testq install
 * (`_tools/testq/clients/testq.mjs`). Projects VENDOR it rather than importing
 * it across an absolute path, so a checkout stays self-contained and still
 * builds on a machine that has never heard of testq — where every call here
 * turns into a warning and the work goes ahead unqueued. Copy it in; if you
 * change it, change it here first.
 *
 * Usage:
 *
 *   import { acquire } from './lib/testq.mjs';
 *   const slot = await acquire({ script: 'capture.mjs', arg: 'warehouse', gpu: 1 });
 *   try { ...launch the engine... } finally { slot.release(process.exitCode ?? 0); }
 *
 * Environment: TESTQ=on|off|require, TESTQ_PORT, TESTQ_AUTOSTART=0|1, TESTQ_HOME.
 */

import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { basename, join, resolve } from 'node:path';

const PROTO = 1;
const PORT = Number(process.env.TESTQ_PORT || 43117);
const BASE = `http://127.0.0.1:${PORT}`;
const MODE = process.env.TESTQ || 'on';
const AUTOSTART = process.env.TESTQ_AUTOSTART !== '0';

/** A no-op slot, so callers never have to branch on whether queueing happened. */
const UNQUEUED = { queued: false, release() {} };

/**
 * The same key the Python side computes, and it has to stay the same: `reap`
 * decides what scratch is stale by comparing these against the worktrees that
 * still exist, and a client that spells its own id differently looks deleted.
 *
 * Windows path, forward slashes, drive letter capitalised — the spelling the
 * engine itself reports, not the one Git-Bash prints.
 */
export function treeId(dir) {
  let native = resolve(dir).replace(/\\/g, '/').replace(/\/+$/, '');
  if (native[1] === ':') native = native[0].toUpperCase() + native.slice(1);
  const digest = createHash('md5').update(native, 'utf8').digest('hex').slice(0, 6);
  return { id: `${basename(native)}-${digest}`, path: native };
}

async function post(path, body, timeoutMs = 30000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${BASE}/${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    return { status: res.status, body: await res.json().catch(() => ({})) };
  } catch {
    return { status: 0, body: {} };
  } finally {
    clearTimeout(timer);
  }
}

async function up() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 2000);
  try {
    return (await fetch(`${BASE}/healthz`, { signal: controller.signal })).ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Where testq.py is, if it is anywhere. Only needed to START a daemon — a
 * daemon someone else already started is reached over HTTP and this never runs.
 */
function findDaemon() {
  const candidates = [
    process.env.TESTQ_PY,
    process.env.TESTQ_HOME && join(process.env.TESTQ_HOME, 'testq.py'),
    // The conventional home next to the shared Godot binary. Projects sit at
    // <projects>/<name>, worktrees at <projects>/<name>/.claude/worktrees/<x>,
    // so walk up from here until a sibling _tools/testq turns up.
    ...(function* () {
      let dir = process.cwd();
      for (let i = 0; i < 6; i += 1) {
        yield join(dir, '_tools', 'testq', 'testq.py');
        const parent = resolve(dir, '..');
        if (parent === dir) return;
        dir = parent;
      }
    })(),
  ];
  return candidates.find((p) => p && existsSync(p)) || null;
}

async function startDaemon() {
  const py = findDaemon();
  if (!py) return false;
  await new Promise((done) => {
    const child = spawn('python', [py, 'start', '--port', String(PORT)], {
      stdio: 'ignore',
      windowsHide: true,
    });
    child.on('error', () => done());
    child.on('close', () => done());
  });
  return up();
}

/**
 * Book capacity for one script invocation and return a handle to give it back.
 *
 * The unit is the invocation, never the engine process: a job that needs two
 * engines alive at once would deadlock against a per-process semaphore.
 *
 * @param {object} job
 * @param {string} job.script    what to call this in the UI, e.g. 'capture-warehouse.mjs'
 * @param {string} [job.arg]     the interesting argument, e.g. a scene name
 * @param {string} [job.project] registry key, for estimates and reap
 * @param {string} [job.treePath] the checkout this runs in (default: cwd)
 * @param {number} [job.slots]   CPU slots, of about four on this box
 * @param {number} [job.gpu]     1 if it opens a window
 * @param {number} [job.engines] engines alive at once
 * @param {boolean} [job.exclusive] wants a quiet box (timing measurements)
 * @param {string[]} [job.mutexes]  named exclusions, e.g. ['ports:27015']
 * @param {number} [job.etaSeconds] override the estimate
 */
export async function acquire(job) {
  if (MODE === 'off') return UNQUEUED;

  const warn = (msg) => process.stderr.write(`!!! ${msg}\n`);
  const say = (msg) => process.stderr.write(`[testq] ${msg}\n`);

  if (!(await up()) && !(AUTOSTART && (await startDaemon()))) {
    if (MODE === 'require') {
      warn('testq is not running and TESTQ=require — refusing to run unqueued');
      process.exit(3);
    }
    // Deliberate: infrastructure that can stop you testing is worse than the
    // contention it was built to prevent.
    warn('testq unavailable — running UNQUEUED; wall-clock assertions may flake');
    return UNQUEUED;
  }

  const tree = treeId(job.treePath || process.cwd());
  const request = await post(
    'acquire',
    {
      proto: PROTO,
      project: job.project || '',
      tree_id: tree.id,
      tree_path: tree.path,
      script: job.script,
      arg: job.arg || '',
      slots: job.slots ?? 1,
      gpu: job.gpu ?? 0,
      engines: job.engines ?? 1,
      exclusive: Boolean(job.exclusive),
      mutexes: job.mutexes || [],
      eta_s: job.etaSeconds ?? null,
      winpid: process.pid,
    },
    10000,
  );

  if (request.status === 409 && request.body?.error === 'proto') {
    warn('the testq daemon is older than this client — running UNQUEUED.');
    warn(`    Upgrade it with: ${request.body.hint || 'testq start --restart'}`);
    return UNQUEUED;
  }
  const ticket = request.body?.ticket;
  if (!ticket) {
    warn('testq gave no ticket — running UNQUEUED');
    return UNQUEUED;
  }

  const lease = () => ({
    queued: true,
    released: false,
    async release(exitCode) {
      if (this.released) return;
      this.released = true;
      await post('release', { lease: ticket, exit_code: exitCode ?? 0 }, 5000);
    },
  });

  if (request.body.granted) return lease();

  say(
    `the box is busy — queued at position ${request.body.position || '?'} ` +
      `(${request.body.blocked_on || 'waiting'}).`,
  );
  say(`nothing has launched yet; watch ${BASE.replace('127.0.0.1', 'localhost')}/`);

  const startedWaiting = Date.now();
  for (;;) {
    const poll = await post('wait', { ticket }, 40000);
    if (poll.status !== 200) {
      // The daemon went away mid-wait. Its slots went with it, so there is
      // nothing left to be polite about.
      warn('testq stopped answering — running UNQUEUED');
      return UNQUEUED;
    }
    const waited = Math.round((Date.now() - startedWaiting) / 1000);
    if (poll.body.granted) {
      say(`got the box after ${waited}s — starting`);
      return lease();
    }
    if (poll.body.cancelled) {
      warn('cancelled from the testq page before it started');
      process.exit(130);
    }
    if (poll.body.unknown) {
      warn('the daemon forgot this ticket — running UNQUEUED');
      return UNQUEUED;
    }
    say(
      `still queued at position ${poll.body.position || '?'} after ${waited}s — ` +
        `${poll.body.blocked_on || 'waiting'}`,
    );
  }
}
