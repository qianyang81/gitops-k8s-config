#!/usr/bin/env bash
set -euo pipefail

# ---------- Colors ----------
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[1;34m'
NC='\033[0m' # No Color

log()  { echo -e "${BLUE}==> $1${NC}"; }
ok()   { echo -e "${GREEN}✔ $1${NC}"; }
fail() { echo -e "${RED}✖ $1${NC}"; exit 1; }

run_kustomize_build() {
  local target="$1"
  local logfile
  logfile="$(mktemp)"

  log "Kustomize build ($target)..."
  if ! kustomize build "$target" >"$logfile" 2>&1; then
    echo
    echo "----- kustomize error output ($target) -----"
    cat "$logfile"
    echo "-------------------------------------------"
    rm -f "$logfile"
    fail "Kustomize build failed: $target"
  fi

  rm -f "$logfile"
  ok "$target build OK"
}


# ---------- Tool check ----------
log "Checking required tools..."
for cmd in kustomize kubeconform flux yamllint; do
  command -v $cmd >/dev/null || fail "Missing tool: $cmd"
done
ok "All tools present"

# ---------- YAML lint ----------
log "YAML lint..."
if ! yamllint .; then
  fail "YAML issues found (see above)"
fi
ok "YAML syntax OK"

# ---------- Kustomize build checks (root) ----------
run_kustomize_build "clusters/aks/root"

# ---------- Kustomize build + Schema validation (envs) ----------
for env in dev staging prod; do
  target="clusters/aks/env/$env"
  manifest="$(mktemp)"

  log "Kustomize build ($target)..."
  if ! kustomize build "$target" >"$manifest" 2>&1; then
    echo
    echo "----- kustomize error output ($target) -----"
    cat "$manifest"
    echo "-------------------------------------------"
    rm -f "$manifest"
    fail "Kustomize build failed: $target"
  fi
  ok "$env build OK"

  log "Schema validation ($env)..."
  if ! kubeconform -strict -ignore-missing-schemas <"$manifest"; then
    echo
    echo "----- kubeconform error output ($env) -----"
    echo "------------------------------------------"
    rm -f "$manifest"
    fail "Schema validation failed: $env"
  fi
  ok "$env schema OK"

  rm -f "$manifest"
done

