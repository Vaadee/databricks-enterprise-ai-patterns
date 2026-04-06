#!/usr/bin/env python3
"""
smoke_test.py — End-to-end smoke test for the background-async-inference app.

Runs a full suite of checks: health, readiness, validation, 404s, submit→poll→result.
Submits two real inference jobs (short + long) and polls until completion.
No external dependencies — stdlib only.

Usage:
    # Option A: let the script grab the token via the CLI
    export APP_URL=https://background-async-inference-dev-<workspace>.databricksapps.com
    export PROFILE=partner
    python smoke_test.py

    # Option B: supply the token directly
    export APP_URL=https://...
    export TOKEN=<databricks_personal_access_token>
    python smoke_test.py

    # Override poll timeout (default 300s):
    POLL_TIMEOUT=120 python smoke_test.py
"""

import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

# ── ANSI colour helpers ───────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
CYAN   = "\033[36m"
DIM    = "\033[2m"


def green(s):  return f"{GREEN}{s}{RESET}"
def yellow(s): return f"{YELLOW}{s}{RESET}"
def red(s):    return f"{RED}{s}{RESET}"
def cyan(s):   return f"{CYAN}{s}{RESET}"
def bold(s):   return f"{BOLD}{s}{RESET}"
def dim(s):    return f"{DIM}{s}{RESET}"


def ok(msg):   print(f"  {green('✓')} {msg}")
def fail(msg): print(f"  {red('✗')} {msg}")
def warn(msg): print(f"  {yellow('⚠')} {msg}")
def info(msg): print(f"  {cyan('·')} {msg}")


# ── Config ────────────────────────────────────────────────────────────────────

APP_URL      = os.environ.get("APP_URL", "").rstrip("/")
TOKEN        = os.environ.get("TOKEN", "")
PROFILE      = os.environ.get("PROFILE", "DEFAULT")
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "300"))
POLL_FAST    = 2   # seconds between polls for the first 30s
POLL_SLOW    = 5   # seconds between polls after 30s


# ── Token resolution ──────────────────────────────────────────────────────────

def _run_cli(*args) -> str:
    """Run a databricks CLI command, trying PATH then Homebrew. Returns stdout."""
    for exe in ["databricks", "/opt/homebrew/bin/databricks"]:
        try:
            result = subprocess.run(
                [exe, *args], capture_output=True, text=True, check=True,
            )
            return result.stdout
        except FileNotFoundError:
            continue
        except subprocess.CalledProcessError as e:
            print(red(f"\nDatabricks CLI error: {e.stderr.strip()}"))
            sys.exit(1)
    print(red("\nDatabricks CLI not found. Install it or set TOKEN= directly."))
    sys.exit(1)


def get_token() -> str:
    """Return TOKEN env var, or fetch a fresh one via the Databricks CLI."""
    if TOKEN:
        return TOKEN
    print(f"\n{dim('Getting auth token from Databricks CLI (profile: ' + PROFILE + ')...')}")
    return json.loads(_run_cli("auth", "token", "--profile", PROFILE, "--output", "json"))["access_token"]


def get_bundle_experiment_name() -> str:
    """
    Resolve the MLflow experiment name from the deployed bundle via `bundle validate`.
    Returns empty string if unavailable (not a failure — just skips the link).
    """
    try:
        raw = _run_cli("bundle", "validate", "--profile", PROFILE, "--output", "json")
        config = json.loads(raw)
        # Path: resources → experiments → inference_tracking → name
        return (
            config.get("resources", {})
                  .get("experiments", {})
                  .get("inference_tracking", {})
                  .get("name", "")
        )
    except Exception:
        return ""


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _request(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    """Make an HTTP request and return (status_code, response_json)."""
    url = f"{APP_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body_bytes = e.read()
            resp_json = json.loads(body_bytes)
        except Exception:
            resp_json = {"detail": e.reason}
        return e.code, resp_json


def GET(path, token):   return _request("GET",  path, token)
def POST(path, token, body): return _request("POST", path, token, body)


# ── Test harness ──────────────────────────────────────────────────────────────

_results: list[tuple[str, bool, str]] = []   # (name, passed, detail)


def check(name: str, passed: bool, detail: str = ""):
    _results.append((name, passed, detail))
    if passed:
        ok(f"{name}{dim(' — ' + detail) if detail else ''}")
    else:
        fail(f"{name}{red(' — ' + detail) if detail else ''}")


def section(title: str):
    print(f"\n{bold(title)}")
    print(dim("─" * 50))


# ── Poll loop ─────────────────────────────────────────────────────────────────

def poll_until_done(job_id: str, token: str, label: str) -> dict | None:
    """
    Poll /jobs/status/{job_id} until DONE or FAILED, with a live status line.
    Returns the final status response dict, or None on timeout.
    """
    deadline = time.time() + POLL_TIMEOUT
    start    = time.time()
    last_status = ""

    while time.time() < deadline:
        elapsed = int(time.time() - start)
        code, body = GET(f"/jobs/status/{job_id}", token)
        if code != 200:
            print(f"\r  {red('Poll error:')} HTTP {code}                ")
            return None

        status = body.get("status", "?")

        # Colour the live status
        colour = {
            "PENDING":   yellow,
            "RUNNING":   cyan,
            "STREAMING": cyan,
            "DONE":      green,
            "FAILED":    red,
        }.get(status, dim)

        print(
            f"\r  {dim(f'[{elapsed:>3}s]')} {label} → {colour(status):<25}",
            end="", flush=True,
        )

        if status in ("DONE", "FAILED"):
            print()   # newline after the live line
            return body

        last_status = status
        interval = POLL_FAST if elapsed < 30 else POLL_SLOW
        time.sleep(interval)

    print(f"\n  {red('Timed out after')} {POLL_TIMEOUT}s (last status: {last_status})")
    return None


# ── Main test suite ───────────────────────────────────────────────────────────

def run_tests():
    if not APP_URL:
        print(red("\nAPP_URL is not set. Export it and retry:\n"))
        print("  export APP_URL=https://background-async-inference-dev-<workspace>.databricksapps.com")
        sys.exit(1)

    token = get_token()

    print(f"\n{bold('╔══════════════════════════════════════════════════╗')}")
    print(f"{bold('║   background-async-inference  smoke test         ║')}")
    print(f"{bold('╚══════════════════════════════════════════════════╝')}")
    print(f"\n  {dim('APP_URL:')} {APP_URL}")
    print(f"  {dim('PROFILE:')} {PROFILE}")
    print(f"  {dim('Timeout:')} {POLL_TIMEOUT}s per job")

    # ── 1. Health & Readiness ─────────────────────────────────────────────────
    section("1 · Health & Readiness")

    code, body = GET("/health", token)
    check("GET /health → 200", code == 200, f"status={body.get('status')}")

    code, body = GET("/ready", token)
    check(
        "GET /ready → 200 (DB reachable)",
        code == 200,
        f"status={body.get('status')}",
    )
    if code == 503:
        warn("/ready returned 503 — DB may be unreachable. Continuing anyway.")

    # ── 2. OpenAPI docs ───────────────────────────────────────────────────────
    section("2 · OpenAPI docs")

    code, body = GET("/openapi.json", token)
    check("GET /openapi.json → 200", code == 200,
          f"paths={len(body.get('paths', {}))}")

    # ── 3. Input validation ───────────────────────────────────────────────────
    section("3 · Input validation (should all be 422)")

    code, _ = POST("/jobs/submit", token, {})
    check("Missing messages → 422", code == 422, f"got {code}")

    code, _ = POST("/jobs/submit", token, {"messages": []})
    check("Empty messages array → 422", code == 422, f"got {code}")

    code, _ = POST("/jobs/submit", token, {
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 0,
    })
    check("max_tokens=0 → 422", code == 422, f"got {code}")

    code, _ = POST("/jobs/submit", token, {
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 99999,
    })
    check("max_tokens=99999 → 422", code == 422, f"got {code}")

    code, _ = POST("/jobs/submit", token, {
        "messages": [{"role": "user", "content": "hi"}],
        "caller_id": "x" * 256,    # exceeds max_length=255
    })
    check("caller_id too long → 422", code == 422, f"got {code}")

    # ── 4. 404s ───────────────────────────────────────────────────────────────
    section("4 · 404 for unknown job IDs")

    bogus = str(uuid.uuid4())
    code, body = GET(f"/jobs/status/{bogus}", token)
    check(f"GET /jobs/status/<random-uuid> → 404", code == 404, body.get("detail", ""))

    code, body = GET(f"/jobs/result/{bogus}", token)
    check(f"GET /jobs/result/<random-uuid> → 404", code == 404, body.get("detail", ""))

    # ── 5. Short job: submit → poll → result ──────────────────────────────────
    section("5 · Short job  (submit → poll → result)")

    short_payload = {
        "messages": [
            {"role": "system", "content": "You are a concise assistant."},
            {"role": "user",   "content": "Say exactly: 'Hello, world!'"},
        ],
        "max_tokens": 30,
        "caller_id": "smoke-test-short",
    }
    code, body = POST("/jobs/submit", token, short_payload)
    check("POST /jobs/submit → 202", code == 202, f"job_id={body.get('job_id')}")

    if code != 202:
        fail(f"Cannot continue short-job test — submit failed ({code}: {body})")
    else:
        short_job_id = body["job_id"]
        check("Response has job_id", bool(short_job_id))
        check("Initial status is PENDING", body.get("status") == "PENDING",
              body.get("status"))

        info(f"Polling job {cyan(short_job_id)} ...")
        final = poll_until_done(short_job_id, token, "short job")

        if final is None:
            fail("Short job timed out")
        else:
            check("Short job reached DONE", final["status"] == "DONE",
                  f"status={final['status']}")

            # Fetch result
            code, result = GET(f"/jobs/result/{short_job_id}", token)
            check("GET /jobs/result → 200", code == 200, f"status={result.get('status')}")
            check("Result has full text",
                  bool(result.get("result")),
                  repr(result.get("result", "")[:80]))
            check("Result has total_tokens > 0",
                  (result.get("total_tokens") or 0) > 0,
                  str(result.get("total_tokens")))
            check("Result has latency_ms > 0",
                  (result.get("latency_ms") or 0) > 0,
                  f"{result.get('latency_ms')}ms")

            print(f"\n  {dim('Response text:')} {cyan(repr(result.get('result', '')[:120]))}")

    # ── 6. Long job: checks STREAMING state ───────────────────────────────────
    section("6 · Long job  (checks STREAMING state is visible)")

    long_payload = {
        "messages": [
            {
                "role": "user",
                "content": (
                    "Write a detailed 400-word essay on why async programming patterns "
                    "matter for AI inference workloads."
                ),
            }
        ],
        "max_tokens": 600,
        "caller_id": "smoke-test-long",
    }
    code, body = POST("/jobs/submit", token, long_payload)
    check("POST /jobs/submit (long) → 202", code == 202, f"job_id={body.get('job_id')}")

    if code != 202:
        fail("Cannot continue long-job test — submit failed")
    else:
        long_job_id = body["job_id"]
        info(f"Polling job {cyan(long_job_id)} (watching for STREAMING state) ...")

        saw_streaming = False
        saw_running   = False
        deadline      = time.time() + POLL_TIMEOUT
        start         = time.time()
        final_status  = None

        while time.time() < deadline:
            elapsed = int(time.time() - start)
            code, st_body = GET(f"/jobs/status/{long_job_id}", token)
            if code != 200:
                break
            status = st_body.get("status", "?")

            colour = {
                "PENDING":   yellow,
                "RUNNING":   cyan,
                "STREAMING": cyan,
                "DONE":      green,
                "FAILED":    red,
            }.get(status, dim)

            print(
                f"\r  {dim(f'[{elapsed:>3}s]')} long job → {colour(status):<25}",
                end="", flush=True,
            )

            if status == "RUNNING":
                saw_running = True
            if status == "STREAMING":
                saw_streaming = True

                # While STREAMING, also check /result for partial data
                _, partial = GET(f"/jobs/result/{long_job_id}", token)
                if partial.get("partial"):
                    print(
                        f"\r  {dim('[partial preview]')} {cyan(repr(partial['partial'][:60]))}...",
                    )

            if status in ("DONE", "FAILED"):
                print()
                final_status = status
                break

            interval = POLL_FAST if elapsed < 30 else POLL_SLOW
            time.sleep(interval)
        else:
            print()
            fail(f"Long job timed out after {POLL_TIMEOUT}s")

        check("Long job reached DONE",
              final_status == "DONE", f"status={final_status}")
        # RUNNING is often missed — job moves PENDING→RUNNING→STREAMING in < 2s
        if saw_running:
            ok("Observed RUNNING state")
        else:
            warn("Did not observe RUNNING state (transitions faster than poll interval — not a failure)")
        if saw_streaming:
            ok("Observed STREAMING state")
        else:
            warn("Did not observe STREAMING state (timing-dependent — not a failure)")

        if final_status == "DONE":
            code, result = GET(f"/jobs/result/{long_job_id}", token)
            check("Long job result has text",
                  bool(result.get("result")),
                  f"{len(result.get('result',''))} chars")
            check("Long job total_tokens > 0",
                  (result.get("total_tokens") or 0) > 0,
                  str(result.get("total_tokens")))

    # ── 7. Concurrent submits ─────────────────────────────────────────────────
    section("7 · Concurrent submits (two jobs at once)")

    concurrent_payload = {
        "messages": [{"role": "user", "content": "Name any one planet in one word."}],
        "max_tokens": 10,
        "caller_id": "smoke-test-concurrent",
    }
    codes_and_ids = []
    for i in range(2):
        code, body = POST("/jobs/submit", token, concurrent_payload)
        codes_and_ids.append((code, body.get("job_id")))

    all_202 = all(c == 202 for c, _ in codes_and_ids)
    unique_ids = len({jid for _, jid in codes_and_ids if jid}) == 2
    check("Both submits → 202", all_202,
          " ".join(str(c) for c, _ in codes_and_ids))
    check("Both have distinct job_ids", unique_ids,
          " ".join(str(jid) for _, jid in codes_and_ids))

    if all_202:
        for i, (_, jid) in enumerate(codes_and_ids):
            info(f"Polling concurrent job {i+1}: {cyan(jid)} ...")
            final = poll_until_done(jid, token, f"concurrent job {i+1}")
            check(f"Concurrent job {i+1} DONE",
                  final is not None and final.get("status") == "DONE",
                  final.get("status") if final else "timeout")

    # ── Summary ───────────────────────────────────────────────────────────────
    section("Summary")

    passed = sum(1 for _, p, _ in _results if p)
    total  = len(_results)
    failed = total - passed

    for name, p, detail in _results:
        sym = green("✓") if p else red("✗")
        txt = green(name) if p else red(name)
        print(f"  {sym} {txt}{dim(' — ' + detail) if detail else ''}")

    print()
    if failed == 0:
        print(f"  {bold(green(f'All {total} checks passed'))} 🎉")
    else:
        print(f"  {bold(green(f'{passed} passed'))}, {bold(red(f'{failed} failed'))}")

    mlflow_experiment = get_bundle_experiment_name()
    if mlflow_experiment:
        print(f"\n  {dim('MLflow experiment:')} {cyan(mlflow_experiment)}")
    print()

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    run_tests()
