#!/usr/bin/env bash
# smoke_test.sh — Shell smoke test for background-async-inference
#
# Mirrors smoke_test.py using curl. Use as a fallback when Python SSL issues
# prevent smoke_test.py from running, or for quick ad-hoc checks.
#
# Usage:
#   # Option A: let the script grab the token via the CLI
#   export APP_URL=https://background-async-inference-dev-<workspace>.databricksapps.com
#   export PROFILE=partner-old
#   bash smoke_test.sh
#
#   # Option B: supply the token directly
#   export APP_URL=https://...
#   export TOKEN=<bearer_token>
#   bash smoke_test.sh
#
#   # Override poll timeout (default 300s):
#   POLL_TIMEOUT=120 bash smoke_test.sh

set -uo pipefail

# ── Config ────────────────────────────────────────────────────────────────────

APP_URL="${APP_URL:-}"
TOKEN="${TOKEN:-}"
PROFILE="${PROFILE:-DEFAULT}"
POLL_TIMEOUT="${POLL_TIMEOUT:-300}"

# ── Colours ───────────────────────────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

# ── Counters & result list ────────────────────────────────────────────────────

PASSED=0; FAILED=0
declare -a RESULT_NAMES=()
declare -a RESULT_PASS=()
declare -a RESULT_DETAIL=()

# ── Helpers ───────────────────────────────────────────────────────────────────

ok()      { printf "  \033[0;32m✓\033[0m %s\n" "$1"; }
fail()    { printf "  \033[0;31m✗\033[0m %s\n" "$1"; }
warn()    { printf "  \033[0;33m⚠\033[0m  %s\n" "$1"; }
info()    { printf "  \033[0;36m·\033[0m %s\n" "$1"; }
section() { printf "\n${BOLD}%s${NC}\n" "$1"; printf "${DIM}"; printf '─%.0s' {1..50}; printf "${NC}\n"; }

check() {
  local name="$1" pass="$2" detail="${3:-}"
  RESULT_NAMES+=("$name"); RESULT_PASS+=("$pass"); RESULT_DETAIL+=("$detail")
  if [ "$pass" = "1" ]; then
    PASSED=$((PASSED + 1))
    printf "  \033[0;32m✓\033[0m %s\033[2m%s\033[0m\n" "$name" "${detail:+ — $detail}"
  else
    FAILED=$((FAILED + 1))
    printf "  \033[0;31m✗\033[0m %s\033[0;31m%s\033[0m\n" "$name" "${detail:+ — $detail}"
  fi
}

# Extract a field from a JSON file using python3
json_get() {
  local file="$1" expr="$2"
  python3 -c "import json; d=json.load(open('$file')); print($expr)" 2>/dev/null || echo ""
}

# Generate a random UUID
rand_uuid() {
  python3 -c "import uuid; print(uuid.uuid4())"
}

# curl wrapper: saves body to $RESP_FILE, returns HTTP status code
RESP_FILE=$(mktemp /tmp/smoke_resp.XXXXXX.json)
trap 'rm -f "$RESP_FILE" /tmp/smoke_submit.json /tmp/smoke_long.json /tmp/smoke_poll.json /tmp/smoke_c1.json /tmp/smoke_c2.json' EXIT

http() {
  local method="$1" path="$2"; shift 2
  curl -s -k -X "$method" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    -o "$RESP_FILE" -w "%{http_code}" \
    "$@" \
    "${APP_URL}${path}"
}

# ── Token resolution ──────────────────────────────────────────────────────────

get_token() {
  if [ -n "$TOKEN" ]; then return; fi
  printf "${DIM}Getting auth token from Databricks CLI (profile: %s)...${NC}\n" "$PROFILE"
  TOKEN=$(databricks auth token --profile "$PROFILE" --output json \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')
}

# ── Poll helpers ──────────────────────────────────────────────────────────────

# poll_until_done <job_id> <label>
# Sets FINAL_STATUS. Returns 0 on DONE, 1 on FAILED or timeout.
FINAL_STATUS=""
poll_until_done() {
  local job_id="$1" label="$2"
  local deadline=$(( $(date +%s) + POLL_TIMEOUT ))
  local start=$(date +%s) elapsed=0 status="" interval=2

  while [ "$(date +%s)" -lt "$deadline" ]; do
    elapsed=$(( $(date +%s) - start ))
    local code
    code=$(curl -s -k -o /tmp/smoke_poll.json -w "%{http_code}" \
      -H "Authorization: Bearer $TOKEN" \
      "${APP_URL}/jobs/status/${job_id}")

    if [ "$code" != "200" ]; then
      printf "\n  ${RED}Poll error: HTTP %s${NC}\n" "$code"; return 1
    fi

    status=$(json_get /tmp/smoke_poll.json "d.get('status','?')")
    printf "\r  ${DIM}[%3ds]${NC} %s → ${CYAN}%-16s${NC}" "$elapsed" "$label" "$status"

    if [ "$status" = "DONE" ] || [ "$status" = "FAILED" ]; then
      printf "\n"; FINAL_STATUS="$status"; return 0
    fi

    interval=2; [ "$elapsed" -ge 30 ] && interval=5
    sleep "$interval"
  done
  printf "\n  ${RED}Timed out after %ss (last: %s)${NC}\n" "$POLL_TIMEOUT" "$status"
  return 1
}

# poll_long_job <job_id>
# Like poll_until_done but also tracks RUNNING/STREAMING and prints partial previews.
SAW_RUNNING=0; SAW_STREAMING=0
poll_long_job() {
  local job_id="$1"
  local deadline=$(( $(date +%s) + POLL_TIMEOUT ))
  local start=$(date +%s) elapsed=0 status="" interval=2
  SAW_RUNNING=0; SAW_STREAMING=0; FINAL_STATUS=""

  while [ "$(date +%s)" -lt "$deadline" ]; do
    elapsed=$(( $(date +%s) - start ))
    local code
    code=$(curl -s -k -o /tmp/smoke_poll.json -w "%{http_code}" \
      -H "Authorization: Bearer $TOKEN" \
      "${APP_URL}/jobs/status/${job_id}")

    if [ "$code" != "200" ]; then
      printf "\n  ${RED}Poll error: HTTP %s${NC}\n" "$code"; return 1
    fi

    status=$(json_get /tmp/smoke_poll.json "d.get('status','?')")
    printf "\r  ${DIM}[%3ds]${NC} long job → ${CYAN}%-16s${NC}" "$elapsed" "$status"

    [ "$status" = "RUNNING"   ] && SAW_RUNNING=1
    if [ "$status" = "STREAMING" ]; then
      SAW_STREAMING=1
      # Fetch partial result while streaming
      local pcode
      pcode=$(curl -s -k -o /tmp/smoke_poll.json -w "%{http_code}" \
        -H "Authorization: Bearer $TOKEN" \
        "${APP_URL}/jobs/result/${job_id}")
      if [ "$pcode" = "200" ]; then
        local partial
        partial=$(json_get /tmp/smoke_poll.json "str(d.get('partial',''))[:60]")
        [ -n "$partial" ] && printf "\r  ${DIM}[partial preview]${NC} ${CYAN}'%s'...${NC}\n" "$partial"
      fi
    fi

    if [ "$status" = "DONE" ] || [ "$status" = "FAILED" ]; then
      printf "\n"; FINAL_STATUS="$status"; return 0
    fi

    interval=2; [ "$elapsed" -ge 30 ] && interval=5
    sleep "$interval"
  done
  printf "\n  ${RED}Timed out after %ss${NC}\n" "$POLL_TIMEOUT"
  return 1
}

# ── Guard ─────────────────────────────────────────────────────────────────────

if [ -z "$APP_URL" ]; then
  printf "${RED}APP_URL is not set. Export it and retry:${NC}\n"
  printf "  export APP_URL=https://background-async-inference-dev-<workspace>.databricksapps.com\n"
  exit 1
fi

get_token

printf "\n${BOLD}╔══════════════════════════════════════════════════╗${NC}\n"
printf "${BOLD}║   background-async-inference  smoke test (sh)   ║${NC}\n"
printf "${BOLD}╚══════════════════════════════════════════════════╝${NC}\n"
printf "\n  ${DIM}APP_URL:${NC} %s\n" "$APP_URL"
printf "  ${DIM}PROFILE:${NC} %s\n" "$PROFILE"
printf "  ${DIM}Timeout:${NC} %ss per job\n" "$POLL_TIMEOUT"

# ── 1. Health & Readiness ─────────────────────────────────────────────────────

section "1 · Health & Readiness"

code=$(http GET /health)
status=$(json_get "$RESP_FILE" "d.get('status','')")
check "GET /health → 200" "$([ "$code" = "200" ] && echo 1 || echo 0)" "status=${status}"

code=$(http GET /ready)
status=$(json_get "$RESP_FILE" "d.get('status','')")
check "GET /ready → 200 (DB reachable)" "$([ "$code" = "200" ] && echo 1 || echo 0)" "status=${status}"
[ "$code" = "503" ] && warn "/ready returned 503 — DB may be unreachable. Continuing anyway."

# ── 2. OpenAPI docs ───────────────────────────────────────────────────────────

section "2 · OpenAPI docs"

code=$(http GET /openapi.json)
paths=$(json_get "$RESP_FILE" "len(d.get('paths',{}))")
check "GET /openapi.json → 200" "$([ "$code" = "200" ] && echo 1 || echo 0)" "paths=${paths}"

# ── 3. Input validation ───────────────────────────────────────────────────────

section "3 · Input validation (should all be 422)"

code=$(http POST /jobs/submit -d '{}')
check "Missing messages → 422" "$([ "$code" = "422" ] && echo 1 || echo 0)" "got ${code}"

code=$(http POST /jobs/submit -d '{"messages":[]}')
check "Empty messages array → 422" "$([ "$code" = "422" ] && echo 1 || echo 0)" "got ${code}"

code=$(http POST /jobs/submit -d '{"messages":[{"role":"user","content":"hi"}],"max_tokens":0}')
check "max_tokens=0 → 422" "$([ "$code" = "422" ] && echo 1 || echo 0)" "got ${code}"

code=$(http POST /jobs/submit -d '{"messages":[{"role":"user","content":"hi"}],"max_tokens":99999}')
check "max_tokens=99999 → 422" "$([ "$code" = "422" ] && echo 1 || echo 0)" "got ${code}"

LONG_ID=$(python3 -c "print('x'*256)")
code=$(http POST /jobs/submit -d "{\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"caller_id\":\"${LONG_ID}\"}")
check "caller_id too long → 422" "$([ "$code" = "422" ] && echo 1 || echo 0)" "got ${code}"

# ── 4. 404 for unknown job IDs ────────────────────────────────────────────────

section "4 · 404 for unknown job IDs"

BOGUS=$(rand_uuid)

code=$(http GET "/jobs/status/${BOGUS}")
detail=$(json_get "$RESP_FILE" "d.get('detail','')")
check "GET /jobs/status/<random-uuid> → 404" "$([ "$code" = "404" ] && echo 1 || echo 0)" "$detail"

code=$(http GET "/jobs/result/${BOGUS}")
detail=$(json_get "$RESP_FILE" "d.get('detail','')")
check "GET /jobs/result/<random-uuid> → 404" "$([ "$code" = "404" ] && echo 1 || echo 0)" "$detail"

# ── 5. Short job: submit → poll → result ──────────────────────────────────────

section "5 · Short job  (submit → poll → result)"

SHORT_PAYLOAD='{"messages":[{"role":"system","content":"You are a concise assistant."},{"role":"user","content":"Say exactly: '"'"'Hello, world!'"'"'"}],"max_tokens":30,"caller_id":"smoke-test-short"}'
code=$(http POST /jobs/submit -d "$SHORT_PAYLOAD")
cp "$RESP_FILE" /tmp/smoke_submit.json

short_job_id=$(json_get /tmp/smoke_submit.json "str(d.get('job_id',''))")
check "POST /jobs/submit → 202" "$([ "$code" = "202" ] && echo 1 || echo 0)" "job_id=${short_job_id}"

if [ "$code" = "202" ]; then
  check "Response has job_id" "$([ -n "$short_job_id" ] && echo 1 || echo 0)"
  init_status=$(json_get /tmp/smoke_submit.json "d.get('status','')")
  check "Initial status is PENDING" "$([ "$init_status" = "PENDING" ] && echo 1 || echo 0)" "$init_status"

  printf "  \033[0;36m·\033[0m Polling job \033[0;36m%s\033[0m ...\n" "$short_job_id"
  if poll_until_done "$short_job_id" "short job"; then
    check "Short job reached DONE" "$([ "$FINAL_STATUS" = "DONE" ] && echo 1 || echo 0)" "status=${FINAL_STATUS}"

    code=$(http GET "/jobs/result/${short_job_id}")
    cp "$RESP_FILE" /tmp/smoke_submit.json
    result_status=$(json_get /tmp/smoke_submit.json "d.get('status','')")
    result_text=$(json_get /tmp/smoke_submit.json "d.get('result','')[:80]")
    total_tok=$(json_get /tmp/smoke_submit.json "d.get('total_tokens',0)")
    latency=$(json_get /tmp/smoke_submit.json "d.get('latency_ms',0)")

    check "GET /jobs/result → 200" "$([ "$code" = "200" ] && echo 1 || echo 0)" "status=${result_status}"
    check "Result has full text" "$([ -n "$result_text" ] && echo 1 || echo 0)" "'${result_text}'"
    check "Result has total_tokens > 0" "$([ "${total_tok:-0}" -gt 0 ] 2>/dev/null && echo 1 || echo 0)" "$total_tok"
    check "Result has latency_ms > 0" "$([ "${latency:-0}" -gt 0 ] 2>/dev/null && echo 1 || echo 0)" "${latency}ms"

    printf "\n  ${DIM}Response text:${NC} ${CYAN}'%s'${NC}\n" "$result_text"
  else
    check "Short job reached DONE" "0" "timed out"
  fi
else
  fail "Cannot continue short-job test — submit failed (${code})"
fi

# ── 6. Long job: checks STREAMING state ───────────────────────────────────────

section "6 · Long job  (checks STREAMING state is visible)"

LONG_PAYLOAD='{"messages":[{"role":"user","content":"Write a detailed 400-word essay on why async programming patterns matter for AI inference workloads."}],"max_tokens":600,"caller_id":"smoke-test-long"}'
code=$(http POST /jobs/submit -d "$LONG_PAYLOAD")
cp "$RESP_FILE" /tmp/smoke_long.json

long_job_id=$(json_get /tmp/smoke_long.json "str(d.get('job_id',''))")
check "POST /jobs/submit (long) → 202" "$([ "$code" = "202" ] && echo 1 || echo 0)" "job_id=${long_job_id}"

if [ "$code" = "202" ]; then
  printf "  \033[0;36m·\033[0m Polling job \033[0;36m%s\033[0m (watching for STREAMING state) ...\n" "$long_job_id"
  poll_long_job "$long_job_id" || true

  check "Long job reached DONE" "$([ "$FINAL_STATUS" = "DONE" ] && echo 1 || echo 0)" "status=${FINAL_STATUS}"

  if [ "$SAW_RUNNING" = "1" ]; then
    ok "Observed RUNNING state"
  else
    warn "Did not observe RUNNING state (transitions faster than poll interval — not a failure)"
  fi
  if [ "$SAW_STREAMING" = "1" ]; then
    ok "Observed STREAMING state"
  else
    warn "Did not observe STREAMING state (timing-dependent — not a failure)"
  fi

  if [ "$FINAL_STATUS" = "DONE" ]; then
    code=$(http GET "/jobs/result/${long_job_id}")
    cp "$RESP_FILE" /tmp/smoke_long.json
    result_text=$(json_get /tmp/smoke_long.json "d.get('result','')[:200]")
    result_len=$(json_get /tmp/smoke_long.json "len(d.get('result',''))")
    total_tok=$(json_get /tmp/smoke_long.json "d.get('total_tokens',0)")
    check "Long job result has text" "$([ -n "$result_text" ] && echo 1 || echo 0)" "${result_len} chars"
    check "Long job total_tokens > 0" "$([ "${total_tok:-0}" -gt 0 ] 2>/dev/null && echo 1 || echo 0)" "$total_tok"
  fi
else
  fail "Cannot continue long-job test — submit failed (${code})"
fi

# ── 7. Concurrent submits ─────────────────────────────────────────────────────

section "7 · Concurrent submits (two jobs at once)"

CONC_PAYLOAD='{"messages":[{"role":"user","content":"Name any one planet in one word."}],"max_tokens":10,"caller_id":"smoke-test-concurrent"}'

code1=$(http POST /jobs/submit -d "$CONC_PAYLOAD"); cp "$RESP_FILE" /tmp/smoke_c1.json
code2=$(http POST /jobs/submit -d "$CONC_PAYLOAD"); cp "$RESP_FILE" /tmp/smoke_c2.json

cjid1=$(json_get /tmp/smoke_c1.json "str(d.get('job_id',''))")
cjid2=$(json_get /tmp/smoke_c2.json "str(d.get('job_id',''))")

both_202=0; [ "$code1" = "202" ] && [ "$code2" = "202" ] && both_202=1
distinct=0; [ "$cjid1" != "$cjid2" ] && [ -n "$cjid1" ] && [ -n "$cjid2" ] && distinct=1

check "Both submits → 202"         "$both_202"  "${code1} ${code2}"
check "Both have distinct job_ids" "$distinct"  "${cjid1} ${cjid2}"

if [ "$both_202" = "1" ]; then
  printf "  \033[0;36m·\033[0m Polling concurrent job 1: \033[0;36m%s\033[0m ...\n" "$cjid1"
  if poll_until_done "$cjid1" "concurrent job 1"; then
    check "Concurrent job 1 DONE" "$([ "$FINAL_STATUS" = "DONE" ] && echo 1 || echo 0)" "$FINAL_STATUS"
  else
    check "Concurrent job 1 DONE" "0" "timed out"
  fi

  printf "  \033[0;36m·\033[0m Polling concurrent job 2: \033[0;36m%s\033[0m ...\n" "$cjid2"
  if poll_until_done "$cjid2" "concurrent job 2"; then
    check "Concurrent job 2 DONE" "$([ "$FINAL_STATUS" = "DONE" ] && echo 1 || echo 0)" "$FINAL_STATUS"
  else
    check "Concurrent job 2 DONE" "0" "timed out"
  fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────

section "Summary"

for i in "${!RESULT_NAMES[@]}"; do
  name="${RESULT_NAMES[$i]}"
  pass="${RESULT_PASS[$i]}"
  detail="${RESULT_DETAIL[$i]}"
  if [ "$pass" = "1" ]; then
    printf "  \033[0;32m✓\033[0m \033[0;32m%s\033[0m\033[2m%s\033[0m\n" "$name" "${detail:+ — $detail}"
  else
    printf "  \033[0;31m✗\033[0m \033[0;31m%s\033[0m\033[0;31m%s\033[0m\n" "$name" "${detail:+ — $detail}"
  fi
done

echo ""
if [ "$FAILED" = "0" ]; then
  printf "  ${BOLD}${GREEN}All %s checks passed${NC} 🎉\n\n" "$PASSED"
else
  printf "  ${BOLD}${GREEN}%s passed${NC}, ${BOLD}${RED}%s failed${NC}\n\n" "$PASSED" "$FAILED"
fi

[ "$FAILED" = "0" ] && exit 0 || exit 1
