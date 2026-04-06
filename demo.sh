#!/usr/bin/env bash
# demo.sh — Centralized runner for all async-inference patterns
#
# Usage:
#   ./demo.sh <command> <pattern> [passthrough options]
#
# Commands:
#   start   Deploy and start a pattern (runs start_demo.sh)
#   stop    Stop (and optionally destroy) a pattern (runs stop_demo.sh)
#   smoke   Run the smoke test suite (runs smoke_test.sh)
#
# Examples:
#   ./demo.sh start lakeflow-job
#   ./demo.sh start background-task --profile myprofile --smoke
#   ./demo.sh stop  lakeflow-job    --profile myprofile --destroy
#   ./demo.sh smoke background-task

set -uo pipefail

BOLD='\033[1m'; R='\033[0;31m'; C='\033[0;36m'; DIM='\033[2m'; NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATTERNS_ROOT="${SCRIPT_DIR}/async-inference"

# ── Collect available patterns ────────────────────────────────────────────────

available_patterns() {
  find "$PATTERNS_ROOT" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort
}

# ── Usage ─────────────────────────────────────────────────────────────────────

usage() {
  printf "\n${BOLD}Usage:${NC}  ./demo.sh <command> <pattern> [options]\n\n"
  printf "${BOLD}Commands:${NC}\n"
  printf "  start   Deploy and start a pattern\n"
  printf "  stop    Stop (optionally destroy) a pattern\n"
  printf "  smoke   Run the smoke test suite\n"
  printf "\n${BOLD}Available patterns:${NC}\n"
  available_patterns | while read -r p; do
    printf "  ${C}%s${NC}\n" "$p"
  done
  printf "\n${BOLD}Examples:${NC}\n"
  printf "  ${DIM}./demo.sh start lakeflow-job${NC}\n"
  printf "  ${DIM}./demo.sh start background-task --profile myprofile --smoke${NC}\n"
  printf "  ${DIM}./demo.sh stop  lakeflow-job    --profile myprofile --destroy${NC}\n"
  printf "  ${DIM}./demo.sh smoke background-task${NC}\n\n"
  exit 1
}

# ── Validate args ─────────────────────────────────────────────────────────────

[ $# -lt 2 ] && usage

COMMAND="$1"; PATTERN="$2"; shift 2

case "$COMMAND" in
  start) SCRIPT="start_demo.sh" ;;
  stop)  SCRIPT="stop_demo.sh"  ;;
  smoke) SCRIPT="smoke_test.sh" ;;
  *)
    printf "\n${R}Unknown command: %s${NC}\n" "$COMMAND"
    usage
    ;;
esac

PATTERN_DIR="${PATTERNS_ROOT}/${PATTERN}"

if [ ! -d "$PATTERN_DIR" ]; then
  printf "\n${R}Pattern not found: %s${NC}\n" "$PATTERN"
  printf "Available patterns:\n"
  available_patterns | while read -r p; do printf "  %s\n" "$p"; done
  echo ""
  exit 1
fi

SCRIPT_PATH="${PATTERN_DIR}/${SCRIPT}"

if [ ! -f "$SCRIPT_PATH" ]; then
  printf "\n${R}Script not found: %s/%s${NC}\n" "$PATTERN" "$SCRIPT"
  exit 1
fi

# ── Dispatch ──────────────────────────────────────────────────────────────────

exec bash "$SCRIPT_PATH" "$@"
