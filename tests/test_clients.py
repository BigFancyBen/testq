"""Tests for the two clients, against a stand-in daemon.

    python -m unittest discover tests

The stand-in is a few lines of http.server on a port of its own: it answers
/healthz, queues everything, and records what it is told. Nothing here goes
near the real daemon or its port. A test is skipped on a machine without bash
(or node) and curl.
"""

import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENTS = os.path.join(os.path.dirname(HERE), "clients")


def posix(path):
    return os.path.abspath(path).replace("\\", "/")


class StandIn(object):
    """A daemon that queues everything for ever, or says `duplicate`."""

    def __init__(self, duplicate=False, wait_s=0.2):
        outer = self
        self.duplicate = duplicate
        self.wait_s = wait_s
        self.cancels = []
        self.acquires = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, payload):
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.send({"ok": 1})

            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(size) or b"{}")
                path = self.path.strip("/")
                if path == "acquire":
                    outer.acquires.append(body)
                    if outer.duplicate:
                        return self.send({"duplicate": 1, "granted": 0, "existing": "J9",
                                          "position": 2, "start_in_s": 60,
                                          "start_in_floor": 0})
                    return self.send({"ticket": "J1", "granted": False, "position": 1,
                                      "blocked_on": "waiting for slots"})
                if path == "wait":
                    time.sleep(outer.wait_s)
                    return self.send({"granted": False, "position": 1,
                                      "blocked_on": "waiting for slots"})
                if path == "cancel":
                    outer.cancels.append(body)
                    return self.send({"cancelled": "removed a queued job"})
                return self.send({})

        class Quiet(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass                       # a client killed mid-reply is the test

        self.server = Quiet(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ClientCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="testq-clients-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def stand_in(self, **kw):
        daemon = StandIn(**kw)
        self.addCleanup(daemon.close)
        return daemon

    def wait_for(self, condition, seconds=20.0):
        end = time.time() + seconds
        while time.time() < end:
            if condition():
                return True
            time.sleep(0.1)
        return False


@unittest.skipUnless(shutil.which("bash") and shutil.which("curl"), "needs bash and curl")
class BashClient(ClientCase):
    def script(self):
        """A run script: source the client, ask, leave its exit code behind."""
        path = os.path.join(self.dir, "run_x.sh")
        with open(path, "w", newline="\n") as fh:
            fh.write(
                '. "%s/testq.sh"\n'
                'trap \'echo $? > "%s/rc"\' EXIT\n'
                'testq_acquire --arg sdt_hf --gpu 1\n'
                'echo reached > "%s/ran"\n'
                % (posix(CLIENTS), posix(self.dir), posix(self.dir)))
        return posix(path)

    def env(self, daemon, **more):
        env = dict(os.environ, TESTQ="on", TESTQ_PORT=str(daemon.port),
                   TESTQ_AUTOSTART="0", TESTQ_TREE=posix(self.dir))
        env.pop("TESTQ_DETACHED", None)
        env.update(more)
        return env

    def orphan(self, daemon, **more):
        """Start the script from a shell that exits a second later."""
        script = self.script()
        subprocess.run(
            ["bash", "-c", "bash %s </dev/null >/dev/null 2>&1 & sleep 1" % script],
            env=self.env(daemon, **more), timeout=30)

    def rc(self):
        path = os.path.join(self.dir, "rc")
        if not os.path.exists(path):
            return None
        with open(path) as fh:
            return fh.read().strip()

    def test_a_queued_run_whose_starter_is_gone_leaves_the_queue(self):
        daemon = self.stand_in()
        self.orphan(daemon)
        self.assertTrue(self.wait_for(lambda: daemon.cancels), "never cancelled")
        self.assertEqual(daemon.cancels[0], {"job": "J1", "why": "orphan"})
        self.assertTrue(self.wait_for(lambda: self.rc() is not None))
        self.assertEqual(self.rc(), "4")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "ran")))

    def test_a_run_started_detached_stays_in_the_queue(self):
        daemon = self.stand_in()
        self.orphan(daemon, TESTQ_DETACHED="1")
        self.assertTrue(self.wait_for(lambda: daemon.acquires))
        time.sleep(3)
        self.assertEqual(daemon.cancels, [])
        self.assertIsNone(self.rc())

    def test_a_run_whose_starter_is_still_there_stays_too(self):
        daemon = self.stand_in()
        proc = subprocess.Popen(["bash", self.script()], env=self.env(daemon),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.kill)
        self.assertTrue(self.wait_for(lambda: daemon.acquires))
        time.sleep(2)
        self.assertEqual(daemon.cancels, [])
        self.assertIsNone(proc.poll())

    def test_a_duplicate_is_told_which_ticket_holds_the_place(self):
        daemon = self.stand_in(duplicate=True)
        proc = subprocess.run(["bash", self.script()], env=self.env(daemon),
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 6)
        self.assertIn("J9", proc.stderr)
        self.assertIn("already queued", proc.stderr)
        self.assertIn("=== RESULT: FAIL (exit 6) -- duplicate of J9 queued from this tree ===", proc.stdout)
        self.assertEqual(daemon.acquires[0].get("dedupe"), 1)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "ran")))


@unittest.skipUnless(shutil.which("node"), "needs node")
class NodeClient(ClientCase):
    HARNESS = (
        "import { acquire } from 'file:///%s/testq.mjs';\n"
        "import { writeFileSync } from 'node:fs';\n"
        "process.on('exit', (code) => writeFileSync('%s/rc', String(code)));\n"
        "await acquire({ script: 'cap.mjs', arg: 'x', gpu: 1, treePath: '%s' });\n"
        "writeFileSync('%s/ran', 'yes');\n"
    )

    def harness(self):
        path = os.path.join(self.dir, "cap.mjs")
        d = posix(self.dir)
        with open(path, "w") as fh:
            fh.write(self.HARNESS % (posix(CLIENTS), d, d, d))
        return path

    def env(self, daemon, **more):
        env = dict(os.environ, TESTQ="on", TESTQ_PORT=str(daemon.port),
                   TESTQ_AUTOSTART="0")
        env.pop("TESTQ_DETACHED", None)
        env.update(more)
        return env

    def rc(self):
        path = os.path.join(self.dir, "rc")
        if not os.path.exists(path):
            return None
        with open(path) as fh:
            return fh.read().strip()

    def test_a_queued_run_whose_starter_is_gone_leaves_the_queue(self):
        daemon = self.stand_in()
        # A launcher that starts the harness and exits a second later.
        launcher = os.path.join(self.dir, "launch.mjs")
        with open(launcher, "w") as fh:
            fh.write(
                "import { spawn } from 'node:child_process';\n"
                "spawn('node', ['%s'], { detached: true, stdio: 'ignore' }).unref();\n"
                "setTimeout(() => process.exit(0), 1000);\n" % self.harness().replace("\\", "/"))
        subprocess.run(["node", launcher], env=self.env(daemon), timeout=30)
        self.assertTrue(self.wait_for(lambda: daemon.cancels), "never cancelled")
        self.assertEqual(daemon.cancels[0], {"job": "J1", "why": "orphan"})
        self.assertTrue(self.wait_for(lambda: self.rc() is not None))
        self.assertEqual(self.rc(), "130")

    def test_a_duplicate_is_told_which_ticket_holds_the_place(self):
        daemon = self.stand_in(duplicate=True)
        proc = subprocess.run(["node", self.harness()], env=self.env(daemon),
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 6)
        self.assertIn("J9", proc.stderr)
        self.assertIn("=== RESULT: FAIL (exit 6) -- duplicate of J9 queued from this tree ===", proc.stdout)
        self.assertEqual(daemon.acquires[0].get("dedupe"), 1)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "ran")))

    def test_a_run_started_detached_stays_in_the_queue(self):
        daemon = self.stand_in()
        launcher = os.path.join(self.dir, "launch.mjs")
        with open(launcher, "w") as fh:
            fh.write(
                "import { spawn } from 'node:child_process';\n"
                "const c = spawn('node', ['%s'], { detached: true, stdio: 'ignore' });\n"
                "console.log(c.pid); c.unref();\n"
                "setTimeout(() => process.exit(0), 1000);\n" % self.harness().replace("\\", "/"))
        out = subprocess.run(["node", launcher], env=self.env(daemon, TESTQ_DETACHED="1"),
                             capture_output=True, text=True, timeout=30)
        pid = int(out.stdout.strip())
        self.addCleanup(lambda: subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)], capture_output=True)
            if os.name == "nt" else os.kill(pid, 9))
        self.assertTrue(self.wait_for(lambda: daemon.acquires))
        time.sleep(3)
        self.assertEqual(daemon.cancels, [])


if __name__ == "__main__":
    unittest.main()
