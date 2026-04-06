#!/usr/bin/env bash
# stop_demo.sh — Stop (and optionally destroy) the lakeflow-job demo
#
# By default: stops the app and leaves all resources in place (safe, fast).
# With --destroy: also runs `bundle destroy` to remove the app, worker job,
#                 reconciler job, Lakebase project, MLflow experiment, and
#                 schema migration job.
#
# Usage:
#   bash stop_demo.sh
#   bash stop_demo.sh --profile my-profile
#   bash stop_demo.sh --profile my-profile --destroy

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

ok()   { printf "  ${G}✓${NC} %s\n" "$1"; }
fail() { printf "  ${R}✗${NC} %s\n" "$1"; }
warn() { printf "  ${Y}⚠${NC}  %s\n" "$1"; }
info() { printf "  ${DIM}→${NC} %s\n" "$1"; }
ask()  { printf "  ${Y}?${NC} %s " "$1"; }

die() { printf "\n${R}${BOLD}Error:${NC}${R} %s${NC}\n\n" "$1"; exit 1; }

spin_start() { _SPIN_MSG="$1"; printf "  ${DIM}…${NC} %s" "$_SPIN_MSG"; }
spin_ok()    { printf "\r  ${G}✓${NC} %s\n" "$_SPIN_MSG"; }
spin_fail()  { printf "\r  ${Y}⚠${NC}  %s\n" "$_SPIN_MSG"; }

require_cmd() {
  command -v "$1" &>/dev/null || die "'$1' not found — please install it first."
}

prompt() {
  local var="$1" question="$2" default="${3:-}"
  ask "$question${default:+ [${default}]}:"
  read -r input
  input="${input:-$default}"
  [ -z "$input" ] && die "Required — please provide a value."
  printf -v "$var" '%s' "$input"
}

prompt_yn() {
  local question="$1" default="${2:-Y}"
  ask "$question [${default}]:"
  read -r ans
  ans="${ans:-$default}"
  [[ "$ans" =~ ^[Yy] ]] && return 0 || return 1
}

# ── Parse flags ───────────────────────────────────────────────────────────────

ARG_PROFILE=""; DO_DESTROY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) ARG_PROFILE="$2"; shift 2 ;;
    --destroy) DO_DESTROY=1; shift ;;
    *) die "Unknown flag: $1" ;;
  esac
done

# ── Banner ────────────────────────────────────────────────────────────────────

clear
printf "${BOLD}${Y}"
cat << 'EOF'
  ╔══════════════════════════════════════════════════════════╗
  ║   lakeflow-job  —  demo stop                            ║
  ╚══════════════════════════════════════════════════════════╝
EOF
printf "${NC}\n"

if [ "$DO_DESTROY" = "1" ]; then
  printf "  ${R}${BOLD}Destroy mode:${NC} This will remove the app, worker job, reconciler job,\n"
  printf "  Lakebase project, MLflow experiment, and schema migration job.\n"
  printf "  ${Y}Postgres data (job_requests / job_chunks / job_results) will be lost.${NC}\n"
else
  printf "  ${DIM}Stopping the app. All resources (worker job, reconciler, Lakebase, MLflow) are kept.\n"
  printf "  Run with ${NC}${BOLD}--destroy${NC}${DIM} to also remove all resources.${NC}\n"
fi
echo ""

# ── Prerequisites ─────────────────────────────────────────────────────────────

require_cmd databricks

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Profile ───────────────────────────────────────────────────────────────────

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

spin_start "Verifying profile '${PROFILE}'..."
if ! databricks auth token --profile "$PROFILE" --output json &>/dev/null; then
  spin_fail
  die "Profile '${PROFILE}' is not authenticated. Run: databricks auth login --profile ${PROFILE}"
fi
spin_ok

# ── App name ──────────────────────────────────────────────────────────────────

echo ""
prompt APP_NAME "App name" "lakeflow-job"
APP_RESOURCE_NAME="${APP_NAME}-dev"

# ── Destroy confirmation ──────────────────────────────────────────────────────

if [ "$DO_DESTROY" = "1" ]; then
  echo ""
  printf "  ${R}${BOLD}You are about to permanently destroy:${NC}\n"
  printf "  ${R}  • Databricks App:          %s${NC}\n" "$APP_RESOURCE_NAME"
  printf "  ${R}  • Lakeflow worker job${NC}\n"
  printf "  ${R}  • Reconciler job${NC}\n"
  printf "  ${R}  • Lakebase project:        %s${NC}\n" "$APP_NAME"
  printf "  ${R}  • MLflow experiment:       %s-dev${NC}\n" "$APP_NAME"
  printf "  ${R}  • Schema migration job${NC}\n"
  printf "  ${R}  • All Postgres data${NC}\n"
  echo ""
  if ! prompt_yn "${BOLD}${R}Are you sure?${NC} This cannot be undone." "n"; then
    printf "\n  ${G}Cancelled.${NC}\n\n"
    exit 0
  fi
fi

# ── Stop app ──────────────────────────────────────────────────────────────────

header "Stopping app"

info "Stopping '${APP_RESOURCE_NAME}'..."
echo ""

if databricks apps stop "$APP_RESOURCE_NAME" --profile "$PROFILE" 2>&1 | \
    while IFS= read -r line; do printf "  ${DIM}│${NC} %s\n" "$line"; done; then
  echo ""
  ok "App stopped"
else
  echo ""
  warn "App stop returned non-zero — it may already be stopped"
fi

# ── Destroy (optional) ────────────────────────────────────────────────────────

if [ "$DO_DESTROY" = "1" ]; then
  header "Destroying bundle resources"

  info "Running bundle destroy..."
  echo ""

  if databricks bundle destroy --profile "$PROFILE" --auto-approve 2>&1 | \
      while IFS= read -r line; do printf "  ${DIM}│${NC} %s\n" "$line"; done; then
    echo ""
    ok "All bundle resources destroyed"
  else
    echo ""
    fail "Bundle destroy encountered errors — check output above"
    exit 1
  fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
if [ "$DO_DESTROY" = "1" ]; then
  printf "${BOLD}${G}"
  cat << 'EOF'
  ╔══════════════════════════════════════════════════════════╗
  ║   All resources destroyed.                              ║
  ╚══════════════════════════════════════════════════════════╝
EOF
  printf "${NC}\n"
  info "To redeploy, run: bash start_demo.sh --profile ${PROFILE}"
else
  printf "${BOLD}${G}"
  cat << 'EOF'
  ╔══════════════════════════════════════════════════════════╗
  ║   App stopped. Resources are intact.                    ║
  ╚══════════════════════════════════════════════════════════╝
EOF
  printf "${NC}\n"
  info "To restart the app: databricks apps start ${APP_RESOURCE_NAME} --profile ${PROFILE}"
  info "To redeploy + start:  bash start_demo.sh --profile ${PROFILE}"
  info "To destroy everything: bash stop_demo.sh --profile ${PROFILE} --destroy"
fi
echo ""
