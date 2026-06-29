#!/usr/bin/env python3
"""
Reproducer for SIGTERM handling bugs in the AMQP proxy.

Creates a minimal TCP forwarding proxy that mirrors the async patterns in
proxy.py (asyncio.start_server + per-connection handler tasks blocked on
read()), then tests three SIGTERM shutdown scenarios:

  1. old_sigterm     server.close() only (the original bug).
                     Hangs on Python 3.12+ because serve_forever()
                     internally calls wait_closed(), which now blocks
                     until every active connection handler finishes.

  2. fixed_no_guard  cancel-all-tasks SIGTERM handler, but NO
                     CancelledError guard in the task-wait section
                     (partial fix).  Exits cleanly but orphans the
                     per-direction forwarding tasks briefly.

  3. full_fix        cancel-all-tasks handler + CancelledError guard
                     that explicitly cancels forwarding tasks (complete
                     fix).  Clean exit, no orphans.

Usage
-----
    python3 test/functional/proxy_sigterm_test.py

Requires Python 3.12+ to reproduce the hang in scenario 1.
"""

import os
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time

HANG_TIMEOUT = 8
CLEAN_TIMEOUT = 5
NUM_CLIENTS = 3

# ---------------------------------------------------------------------------
# Proxy script — written to a temp file and executed as a subprocess.
# Accepts a "variant" argument to switch between old/fixed behaviour.
# ---------------------------------------------------------------------------

PROXY_SCRIPT = textwrap.dedent("""\
import asyncio
import signal
import sys


async def forward(reader, writer):
    try:
        while True:
            data = await reader.read(8192)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


async def handle_client(client_reader, client_writer,
                        backend_host, backend_port, variant):
    backend_writer = None
    try:
        backend_reader, backend_writer = await asyncio.wait_for(
            asyncio.open_connection(backend_host, backend_port), timeout=5.0)

        task_c2s = asyncio.create_task(forward(client_reader, backend_writer))
        task_s2c = asyncio.create_task(forward(backend_reader, client_writer))

        if variant == "full_fix":
            try:
                done, pending = await asyncio.wait(
                    [task_c2s, task_s2c],
                    return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
            except asyncio.CancelledError:
                task_c2s.cancel()
                task_s2c.cancel()
                raise
        else:
            # old_sigterm and fixed_no_guard: no CancelledError guard
            done, pending = await asyncio.wait(
                [task_c2s, task_s2c],
                return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()

    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        for wr in [client_writer, backend_writer]:
            if wr:
                try:
                    wr.close()
                    await wr.wait_closed()
                except Exception:
                    pass


async def main():
    listen_port = int(sys.argv[1])
    backend_port = int(sys.argv[2])
    variant = sys.argv[3]

    server = await asyncio.start_server(
        lambda r, w: handle_client(
            r, w, "127.0.0.1", backend_port, variant),
        "127.0.0.1", listen_port)

    loop = asyncio.get_running_loop()

    if variant == "old_sigterm":
        loop.add_signal_handler(signal.SIGTERM, server.close)
    else:
        def _handle_sigterm():
            for task in asyncio.all_tasks(loop):
                task.cancel()
        loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)

    print("READY", flush=True)

    try:
        async with server:
            await server.serve_forever()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("SHUTDOWN", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
""")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    return False


# ---------------------------------------------------------------------------
# Mock backend — holds every accepted connection open until the thread dies
# ---------------------------------------------------------------------------

def _run_backend(port, ready):
    import asyncio as _aio

    async def _serve():
        async def _hold(reader, writer):
            try:
                while True:
                    if not await reader.read(4096):
                        break
            except Exception:
                pass
            finally:
                writer.close()

        srv = await _aio.start_server(_hold, "127.0.0.1", port)
        ready.set()
        async with srv:
            await srv.serve_forever()

    try:
        _aio.run(_serve())
    except Exception:
        pass


class MockBackend:
    def __init__(self):
        self.port = find_free_port()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=_run_backend, args=(self.port, self._ready), daemon=True)

    def start(self):
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("backend did not start")
        if not wait_for_port(self.port):
            raise RuntimeError("backend port unreachable")


# ---------------------------------------------------------------------------
# Test driver
# ---------------------------------------------------------------------------

def run_test(label, script_path, backend_port, variant, expect_hang):
    print(f"\n{'=' * 60}")
    print(f"TEST: {label}")
    print(f"{'=' * 60}")

    proxy_port = find_free_port()
    proc = subprocess.Popen(
        [sys.executable, script_path,
         str(proxy_port), str(backend_port), variant],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # Wait for the proxy to signal readiness
    line = proc.stdout.readline()
    if b"READY" not in line:
        print(f"  FAIL: proxy did not print READY (got {line!r})")
        proc.kill()
        proc.wait()
        return False

    print(f"  Proxy on :{proxy_port}  (PID {proc.pid})")

    # Open persistent client connections through the proxy so that
    # forwarding tasks block on read().
    clients = []
    for i in range(NUM_CLIENTS):
        try:
            s = socket.create_connection(("127.0.0.1", proxy_port), timeout=2)
            s.sendall(b"PING")
            clients.append(s)
        except Exception as exc:
            print(f"  warning: client {i} failed: {exc}")

    print(f"  {len(clients)} client(s) connected")
    time.sleep(0.5)

    # Send SIGTERM and measure how long the process takes to exit
    print("  Sending SIGTERM ...")
    t0 = time.monotonic()
    os.kill(proc.pid, signal.SIGTERM)

    try:
        proc.wait(timeout=HANG_TIMEOUT)
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        for s in clients:
            s.close()
        if expect_hang:
            print(f"  Proxy HUNG for {elapsed:.1f}s — bug reproduced")
            print("  PASS")
            proc.kill()
            proc.wait()
            return True
        print(f"  FAIL: proxy hung {elapsed:.1f}s (expected clean exit)")
        proc.kill()
        proc.wait()
        return False

    elapsed = time.monotonic() - t0
    stderr_text = proc.stderr.read().decode(errors="replace")
    for s in clients:
        try:
            s.close()
        except Exception:
            pass

    if expect_hang:
        print(f"  FAIL: expected hang but proxy exited in {elapsed:.1f}s")
        return False

    if elapsed > CLEAN_TIMEOUT:
        print(f"  FAIL: took {elapsed:.1f}s to exit (limit {CLEAN_TIMEOUT}s)")
        return False

    print(f"  Proxy exited in {elapsed:.1f}s  (code {proc.returncode})")
    if "was destroyed" in stderr_text:
        print("  Note: orphaned-task warnings on stderr")
    print("  PASS")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    vi = sys.version_info
    print(f"Python {vi.major}.{vi.minor}.{vi.micro}")

    if vi < (3, 12):
        print(
            "\nNOTE: Python < 3.12 — the server.close() hang only\n"
            "reproduces on 3.12+ (wait_closed() semantics changed).\n"
            "Test 1 will be skipped.\n")

    tmpdir = tempfile.mkdtemp(prefix="proxy_sigterm_test_")
    script = os.path.join(tmpdir, "proxy.py")
    with open(script, "w") as f:
        f.write(PROXY_SCRIPT)

    backend = MockBackend()
    backend.start()
    print(f"Mock backend on :{backend.port}")

    results = {}

    if vi >= (3, 12):
        results["old_sigterm"] = run_test(
            "OLD: server.close() only — expect HANG",
            script, backend.port, "old_sigterm", expect_hang=True)
    else:
        results["old_sigterm"] = None

    results["fixed_no_guard"] = run_test(
        "PARTIAL FIX: cancel-all-tasks, no CancelledError guard — expect CLEAN EXIT",
        script, backend.port, "fixed_no_guard", expect_hang=False)

    results["full_fix"] = run_test(
        "FULL FIX: cancel-all-tasks + CancelledError guard — expect CLEAN EXIT",
        script, backend.port, "full_fix", expect_hang=False)

    try:
        os.unlink(script)
        os.rmdir(tmpdir)
    except Exception:
        pass

    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    all_pass = True
    for name, result in results.items():
        if result is None:
            tag = "SKIP (needs 3.12+)"
        elif result:
            tag = "PASS"
        else:
            tag = "FAIL"
            all_pass = False
        print(f"  {name:20s} {tag}")

    print()
    if all_pass:
        print("All tests passed.")
    else:
        print("Some tests FAILED.")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
