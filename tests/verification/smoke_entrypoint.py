"""`python app.py` must start and serve.

Every other test imports the module and pushes its own application context, so
none of them exercise the entry point the app is actually started with. That
gap let `RuntimeError: Working outside of application context` ship: init_db()
was called bare from __main__, and Flask-SQLAlchemy needs a context.

This starts the real process against a throwaway database and requests a few
pages over HTTP.
"""
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.path.join(HERE, "smoke_entrypoint.db")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


shutil.copy(os.path.join(ROOT, "cbt.db"), DB)
PORT = free_port()
env = dict(os.environ, CRAINBOW_DB=DB, PORT=str(PORT), PYTHONIOENCODING="utf-8")

proc = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def get(path):
    return urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5)


try:
    started = False
    deadline = time.time() + 45
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            get("/health")
            started = True
            break
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)

    if not started:
        out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
        check("app.py starts and serves", False, "process exited" if proc.poll() is not None else "timeout")
        print("\n--- process output ---")
        print(out[-3000:])
    else:
        check("app.py starts and serves", True)
        check("process still running", proc.poll() is None)
        for path, expect in (("/health", 200), ("/", 200), ("/login", 200)):
            try:
                r = get(path)
                check(f"GET {path}", r.status == expect, f"{r.status} != {expect}")
            except urllib.error.HTTPError as exc:
                check(f"GET {path}", exc.code == expect, str(exc.code))
        # A protected page must redirect an anonymous visitor, not error.
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{PORT}/admin/examination"), timeout=5)
            check("GET /admin/examination redirects or renders", r.status in (200, 302), str(r.status))
        except urllib.error.HTTPError as exc:
            check("GET /admin/examination redirects or renders", exc.code in (302, 401, 403), str(exc.code))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

print()
failed = [r for r in results if not r[1]]
print(f"{len(results)-len(failed)}/{len(results)} entry-point checks passed")
sys.exit(1 if failed else 0)
