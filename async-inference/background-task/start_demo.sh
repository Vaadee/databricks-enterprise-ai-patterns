#!/usr/bin/env bash
# start_demo.sh — Deploy and start the background-task demo
#
# Walks through every setup step interactively, asks only for what it needs,
# and prints the app URL + bearer token at the end ready to use.
#
# Usage:
#   bash start_demo.sh
#   bash start_demo.sh --profile my-profile          # skip profile prompt
#   bash start_demo.sh --profile my-profile --smoke  # run smoke test at the end

set -uo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────

R='\033[0;31m'; G='\033[0;32m'; Y='\033[0;33m'; C='\033[0;36m'
B='\033[0;34m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

# ── Helpers ───────────────────────────────────────────────────────────────────

header() {
  echo ""
  printf "${BOLD}${B}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
  printf "${BOLD}${B}  %s${NC}\n" "$1"
  printf "${BOLD}${B}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
}

step() { printf "\n${BOLD}${C}[Step %s]${NC} %s\n" "$1" "$2"; }
ok()   { printf "  ${G}✓${NC} %s\n" "$1"; }
fail() { printf "  ${R}✗${NC} %s\n" "$1"; }
info() { printf "  ${DIM}→${NC} %s\n" "$1"; }
ask()  { printf "  ${Y}?${NC} %s " "$1"; }

die() { printf "\n${R}${BOLD}Error:${NC}${R} %s${NC}\n\n" "$1"; exit 1; }

spin_start() { _SPIN_MSG="$1"; printf "  ${DIM}…${NC} %s" "$_SPIN_MSG"; }
spin_ok()    { printf "\r  ${G}✓${NC} %s\n" "$_SPIN_MSG"; }
spin_fail()  { printf "\r  ${R}✗${NC} %s\n" "$_SPIN_MSG"; }

require_cmd() {
  command -v "$1" &>/dev/null || die "'$1' not found — please install it first."
}

prompt() {
  # prompt <var_name> <question> [default]
  local var="$1" question="$2" default="${3:-}"
  ask "$question${default:+ [${default}]}:"
  read -r input
  input="${input:-$default}"
  [ -z "$input" ] && die "Required — please provide a value."
  printf -v "$var" '%s' "$input"
}

prompt_yn() {
  # prompt_yn <question> [Y/n]  → returns 0 for yes, 1 for no
  local question="$1" default="${2:-Y}"
  ask "$question [${default}]:"
  read -r ans
  ans="${ans:-$default}"
  [[ "$ans" =~ ^[Yy] ]] && return 0 || return 1
}

# ── Parse flags ───────────────────────────────────────────────────────────────

ARG_PROFILE=""; RUN_SMOKE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) ARG_PROFILE="$2"; shift 2 ;;
    --smoke)   RUN_SMOKE=1; shift ;;
    *) die "Unknown flag: $1" ;;
  esac
done

# ── Banner ────────────────────────────────────────────────────────────────────

clear
printf "${BOLD}${C}"
cat << 'EOF'
  ╔══════════════════════════════════════════════════════════╗
  ║   background-task  —  demo start             ║
  ║   Async LLM inference on Databricks Apps + Lakebase     ║
  ╚══════════════════════════════════════════════════════════╝
EOF
printf "${NC}\n"
printf "  This script will:\n"
printf "  ${DIM}1.${NC} Deploy the Databricks Asset Bundle (app + Lakebase + MLflow)\n"
printf "  ${DIM}2.${NC} Run the schema migration (create Postgres tables)\n"
printf "  ${DIM}3.${NC} Start the Databricks App\n"
printf "  ${DIM}4.${NC} Print your app URL + bearer token, ready to use\n"
echo ""

# ── Prerequisites ─────────────────────────────────────────────────────────────

require_cmd databricks
require_cmd python3

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Step 1: Profile ───────────────────────────────────────────────────────────

header "Configuration"

if [ -n "$ARG_PROFILE" ]; then
  PROFILE="$ARG_PROFILE"
  ok "Using profile: ${BOLD}${PROFILE}${NC}"
else
  info "Available Databricks profiles:"
  databricks auth profiles 2>/dev/null | tail -n +2 | while IFS= read -r line; do
    printf "    %s\n" "$line"
  done || true
  echo ""
  prompt PROFILE "Databricks CLI profile" "DEFAULT"
fi

# Verify the profile is valid
spin_start "Verifying profile '${PROFILE}'..."
if ! databricks auth token --profile "$PROFILE" --output json &>/dev/null; then
  spin_fail
  die "Profile '${PROFILE}' is not authenticated. Run: databricks auth login --profile ${PROFILE}"
fi
spin_ok

# ── Step 2: Inference provider ────────────────────────────────────────────────

echo ""
printf "  ${BOLD}Inference provider${NC}\n"
printf "  ${DIM}A)${NC} Databricks Foundation Models ${G}(recommended, no secrets needed)${NC}\n"
printf "  ${DIM}B)${NC} Azure OpenAI\n"
echo ""

USE_AZURE=0
if ! prompt_yn "Use Databricks Foundation Models?" "Y"; then
  USE_AZURE=1
fi

# ── Step 3: Optional vars override ───────────────────────────────────────────

echo ""
printf "  ${BOLD}Bundle variables${NC} ${DIM}(press Enter to use defaults)${NC}\n"
echo ""

prompt APP_NAME "App name" "background-task"

# Derive the target-qualified resource name (bundle appends -dev for dev target)
APP_RESOURCE_NAME="${APP_NAME}-dev"

# ── Step 4: Deploy bundle ─────────────────────────────────────────────────────

header "Step 1 of 4 — Deploy bundle"

info "Deploying: app, Lakebase project, MLflow experiment, schema migration job"
echo ""

DEPLOY_VARS="--var=app_name=${APP_NAME}"
[ "$USE_AZURE" = "1" ] && DEPLOY_VARS="${DEPLOY_VARS} --var=foundation_model_name="

if ! databricks bundle deploy --profile "$PROFILE" $DEPLOY_VARS 2>&1 | \
    while IFS= read -r line; do printf "  ${DIM}│${NC} %s\n" "$line"; done; then
  die "Bundle deploy failed."
fi
echo ""
ok "Bundle deployed"

# ── Step 5: Azure OpenAI secrets (if chosen) ──────────────────────────────────

if [ "$USE_AZURE" = "1" ]; then
  header "Step 1b — Azure OpenAI secrets"

  SCOPE="${APP_NAME}"

  spin_start "Creating secret scope '${SCOPE}'..."
  databricks secrets create-scope "$SCOPE" --profile "$PROFILE" 2>/dev/null || true
  spin_ok

  prompt AZ_ENDPOINT "Azure OpenAI endpoint" ""
  prompt AZ_KEY      "Azure OpenAI key" ""
  prompt AZ_DEPLOY   "Azure deployment name" ""

  databricks secrets put-secret "$SCOPE" azure-openai-endpoint \
    --string-value "$AZ_ENDPOINT" --profile "$PROFILE"
  databricks secrets put-secret "$SCOPE" azure-openai-key \
    --string-value "$AZ_KEY" --profile "$PROFILE"
  databricks secrets put-secret "$SCOPE" azure-deployment-name \
    --string-value "$AZ_DEPLOY" --profile "$PROFILE"

  ok "Secrets stored in scope '${SCOPE}'"
fi

# ── Step 6: Schema migration ──────────────────────────────────────────────────

header "Step 2 of 4 — Schema migration"
info "Creating Postgres tables, provisioning app SP role, granting MLflow permissions"
echo ""

if ! databricks bundle run schema_migration --profile "$PROFILE" 2>&1 | \
    while IFS= read -r line; do printf "  ${DIM}│${NC} %s\n" "$line"; done; then
  die "Schema migration failed."
fi
echo ""
ok "Schema migration complete"

# ── Step 7: Start app ─────────────────────────────────────────────────────────

header "Step 3 of 4 — Start app"
info "Starting Databricks App '${APP_RESOURCE_NAME}' (may take 1–2 min on cold start)"
echo ""

if ! databricks bundle run background_task_app --profile "$PROFILE" 2>&1 | \
    while IFS= read -r line; do printf "  ${DIM}│${NC} %s\n" "$line"; done; then
  die "App start failed."
fi
echo ""
ok "App started"

# ── Step 8: Fetch URL + token ─────────────────────────────────────────────────

header "Step 4 of 4 — Collect credentials"

spin_start "Fetching app URL..."
APP_URL=$(databricks apps get "$APP_RESOURCE_NAME" --profile "$PROFILE" --output json \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['url'])")
spin_ok

spin_start "Fetching bearer token..."
TOKEN=$(databricks auth token --profile "$PROFILE" --output json \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')
spin_ok

# Quick health check
spin_start "Health check..."
HTTP_CODE=$(curl -s -k -o /tmp/health_resp.json -w "%{http_code}" \
  -H "Authorization: Bearer $TOKEN" "${APP_URL}/health" || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
  spin_ok
else
  spin_fail
  printf "  ${Y}⚠${NC}  Health check returned HTTP %s — app may still be warming up\n" "$HTTP_CODE"
fi

# ── Summary ───────────────────────────────────────────────────────────────────

printf "\n${BOLD}${G}"
cat << 'EOF'
  ╔══════════════════════════════════════════════════════════╗
  ║   Demo is ready!                                        ║
  ╚══════════════════════════════════════════════════════════╝
EOF
printf "${NC}\n"

printf "  ${BOLD}APP_URL${NC}\n"
printf "  ${C}%s${NC}\n\n" "$APP_URL"

printf "  ${BOLD}TOKEN${NC} ${DIM}(copy and paste, or use the export below)${NC}\n"
printf "  ${DIM}%s...%s${NC}\n\n" "${TOKEN:0:20}" "${TOKEN: -10}"

printf "  ${BOLD}Quick start:${NC}\n"
printf "  ${DIM}export APP_URL=%s${NC}\n" "$APP_URL"
printf "  ${DIM}export TOKEN=%s${NC}\n\n" "$TOKEN"

printf "  ${BOLD}Try it:${NC}\n"
printf "  ${DIM}curl -s -X POST \$APP_URL/jobs/submit \\${NC}\n"
printf "  ${DIM}  -H \"Authorization: Bearer \$TOKEN\" \\${NC}\n"
printf "  ${DIM}  -H \"Content-Type: application/json\" \\${NC}\n"
printf "  ${DIM}  -d '{\"messages\":[{\"role\":\"user\",\"content\":\"Hello!\"}],\"max_tokens\":64}'${NC}\n\n"

printf "  ${BOLD}Stop the demo:${NC}\n"
printf "  ${DIM}bash stop_demo.sh --profile %s${NC}\n\n" "$PROFILE"

# ── Optional smoke test ───────────────────────────────────────────────────────

if [ "$RUN_SMOKE" = "1" ]; then
  printf "${BOLD}${C}Running smoke test...${NC}\n\n"
  APP_URL="$APP_URL" TOKEN="$TOKEN" bash smoke_test.sh
elif prompt_yn "Run smoke test now (26 checks, ~30s)?" "Y"; then
  echo ""
  APP_URL="$APP_URL" TOKEN="$TOKEN" bash smoke_test.sh
fi

echo ""
