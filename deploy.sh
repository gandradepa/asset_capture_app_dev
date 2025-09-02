cat > /home/developer/asset_capture_app_dev/deploy.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

# ===== Config =====
APP_DIR="/home/developer/asset_capture_app_dev"
VENV="${APP_DIR}/venv"
SERVICE="assetcap"
DEFAULT_BRANCH="Dev_environment"
DEFAULT_HOST="appprod.assetcap.facilities.ubc.ca"   # used for optional HTTPS check
DEPLOY_LOG="${APP_DIR}/.deploy_history"

# ===== Args =====
BRANCH="${1:-$DEFAULT_BRANCH}"
HOST="${2:-$DEFAULT_HOST}"

log()  { printf "\n\033[1;34m[DEPLOY]\033[0m %s\n" "$*"; }
err()  { printf "\n\033[1;31m[ERROR]\033[0m %s\n" "$*" >&2; }
ok()   { printf "\033[1;32m[OK]\033[0m %s\n" "$*\n"; }

# ===== Pre-flight =====
if [[ ! -d "$APP_DIR/.git" ]]; then
  err "Git repo not found at ${APP_DIR}"
  exit 1
fi

cd "$APP_DIR"

if ! command -v git >/dev/null 2>&1; then err "git not installed"; exit 1; fi
if ! command -v python3 >/dev/null 2>&1; then err "python3 not installed"; exit 1; fi
if ! command -v pip >/dev/null 2>&1; then
  if ! command -v python3 -m pip >/dev/null 2>&1; then
    err "pip not installed"; exit 1
  fi
fi

# ===== Remember current commit for rollback =====
PREV_COMMIT="$(git rev-parse HEAD)"
log "Current commit: ${PREV_COMMIT}"

# ===== Update code from branch =====
log "Fetching and checking out branch: ${BRANCH}"
git fetch --all --prune
if git show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  git checkout "${BRANCH}"
else
  git checkout -b "${BRANCH}" "origin/${BRANCH}"
fi
git reset --hard "origin/${BRANCH}"
ok "Code updated to $(git rev-parse --short HEAD) on ${BRANCH}"

# ===== Python venv & dependencies =====
if [[ ! -d "$VENV" ]]; then
  log "Creating virtualenv at ${VENV}"
  python3 -m venv "$VENV"
fi

# shellcheck disable=SC1090
source "${VENV}/bin/activate"
python -m pip install --upgrade pip
if [[ -f requirements.txt ]]; then
  log "Installing requirements"
  pip install -r requirements.txt
else
  log "No requirements.txt found — skipping"
fi
deactivate
ok "Python deps installed"

# ===== Ensure Nginx can read static (ACLs) =====
# (safe to re-apply every deploy)
log "Applying ACLs for Nginx static access"
sudo setfacl -m u:www-data:rx /home || true
sudo setfacl -m u:www-data:rx /home/developer || true
sudo setfacl -m u:www-data:rx "${APP_DIR}" || true
sudo setfacl -R -m u:www-data:rX "${APP_DIR}/static" || true
sudo setfacl -dR -m u:www-data:rX "${APP_DIR}/static" || true
ok "ACLs ensured"

# ===== Restart (or reload) service =====
log "Reloading ${SERVICE}"
if sudo systemctl reload "${SERVICE}"; then
  ok "Reloaded ${SERVICE}"
else
  log "Reload failed, restarting ${SERVICE}"
  sudo systemctl restart "${SERVICE}"
  ok "Restarted ${SERVICE}"
fi

# ===== Health checks =====
log "Health check: gunicorn (127.0.0.1:8000)"
if curl -fsI "http://127.0.0.1:8000" >/dev/null; then
  ok "Gunicorn up on :8000"
else
  err "Gunicorn not responding on :8000 — rolling back"
  git reset --hard "${PREV_COMMIT}"
  sudo systemctl restart "${SERVICE}"
  exit 1
fi

log "Health check: HTTPS via Nginx (${HOST})"
if curl -fsI "https://${HOST}" >/dev/null; then
  ok "Nginx + TLS OK for ${HOST}"
else
  err "HTTPS check failed (continuing, as TLS may be propagating or cert was not issued for this host)"
fi

# ===== Record deploy =====
{
  echo "timestamp: $(date -Is)"
  echo "branch: ${BRANCH}"
  echo "commit: $(git rev-parse HEAD)"
  echo "user: $(whoami)"
  echo "-----"
} >> "${DEPLOY_LOG}"

ok "Deployment completed successfully."
EOF

chmod +x /home/developer/asset_capture_app_dev/deploy.sh
