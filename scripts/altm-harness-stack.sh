#!/usr/bin/env bash
set -euo pipefail

ALTM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DSH_REPO="${DSH_REPO:-$(cd "$ALTM_ROOT/.." && pwd)/deepseek-harness}"
ENV_FILE="${ALTM_HARNESS_ENV_FILE:-$ALTM_ROOT/data/altm-harness.env}"
RUN_DIR="$ALTM_ROOT/data/altm-harness-run"
LOG_DIR="$RUN_DIR/logs"
PLUGIN_STATE_FILE="$RUN_DIR/plugin-state"
DB_PATH="${ALTM_HARNESS_DB:-$ALTM_ROOT/data/deepseek-harness.sqlite3}"
NODE_BIN="${DSH_NODE_BIN:-}"
LOCAL_BIN="$ALTM_ROOT/data/bin"
PROFILE="${ALTM_HARNESS_PROFILE:-web}"
MCP_PORT="${ALTM_MCP_PORT:-8000}"
WEB_PORT="${DSH_WEB_PORT:-3000}"
PYTHON="$ALTM_ROOT/.venv/bin/python"

export PATH="$LOCAL_BIN${NODE_BIN:+:$NODE_BIN}:$PATH"
export XDG_STATE_HOME="${XDG_STATE_HOME:-$ALTM_ROOT/data/xdg-state}"
export PYTHONUNBUFFERED=1

mkdir -p "$RUN_DIR" "$LOG_DIR" "$LOCAL_BIN" "$XDG_STATE_HOME"

load_environment() {
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "missing runtime environment: $ENV_FILE" >&2
    return 1
  fi
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
}

profile_dir() {
  printf '%s/profiles/%s\n' "$DSH_HOME" "$PROFILE"
}

profile_manifest() {
  printf '%s/package.json\n' "$(profile_dir)"
}

profile_patch() {
  printf '%s/cordis.patch.yml\n' "$(profile_dir)"
}

plugin_installed() {
  local manifest
  manifest="$(profile_manifest)"
  [[ -f "$manifest" ]] \
    && [[ "$(jq -r '.dependencies["@altm/deepseek-harness"] // empty' "$manifest")" != "" ]]
}

plugin_status() {
  if ! plugin_installed; then
    echo "uninstalled"
    return
  fi
  node "$ALTM_ROOT/scripts/manage-altm-profile.mjs" \
    status "$(profile_patch)" "$DSH_REPO"
}

pid_file() {
  printf '%s/%s.pid\n' "$RUN_DIR" "$1"
}

log_file() {
  printf '%s/%s.log\n' "$LOG_DIR" "$1"
}

read_pid() {
  local file
  file="$(pid_file "$1")"
  [[ -f "$file" ]] && tr -d '[:space:]' < "$file"
}

is_running() {
  local pid
  pid="$(read_pid "$1" || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

start_process() {
  local name="$1"
  local cwd="$2"
  shift 2
  if is_running "$name"; then
    echo "$name already running (pid $(read_pid "$name"))"
    return
  fi
  rm -f "$(pid_file "$name")"
  (
    cd "$cwd"
    nohup "$@" </dev/null >"$(log_file "$name")" 2>&1 &
    echo "$!" >"$(pid_file "$name")"
  )
  sleep 0.3
  if ! is_running "$name"; then
    echo "$name failed to start; log follows:" >&2
    tail -n 80 "$(log_file "$name")" >&2 || true
    return 1
  fi
  echo "$name started (pid $(read_pid "$name"))"
}

wait_for_port() {
  local name="$1"
  local port="$2"
  local attempts="${3:-100}"
  for ((index = 0; index < attempts; index += 1)); do
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      sleep 0.5
      if is_running "$name" && lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        return
      fi
    fi
    if ! is_running "$name"; then
      echo "$name exited before port $port opened" >&2
      tail -n 100 "$(log_file "$name")" >&2 || true
      return 1
    fi
    sleep 0.2
  done
  echo "$name did not open port $port" >&2
  tail -n 100 "$(log_file "$name")" >&2 || true
  return 1
}

wait_for_http() {
  local name="$1"
  local url="$2"
  local attempts="${3:-100}"
  local code
  for ((index = 0; index < attempts; index += 1)); do
    code="$(curl -sS -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
    if [[ "$code" =~ ^2[0-9][0-9]$ ]]; then
      return
    fi
    if ! is_running "$name"; then
      echo "$name exited before $url became healthy" >&2
      tail -n 100 "$(log_file "$name")" >&2 || true
      return 1
    fi
    sleep 0.2
  done
  echo "$name did not return HTTP 2xx from $url" >&2
  tail -n 100 "$(log_file "$name")" >&2 || true
  return 1
}

build_artifacts() {
  load_environment
  command -v node >/dev/null
  command -v corepack >/dev/null
  command -v npm >/dev/null

  if [[ ! -x "$LOCAL_BIN/pnpm" ]]; then
    corepack enable --install-directory "$LOCAL_BIN"
  fi

  (
    cd "$ALTM_ROOT/adapters/typescript"
    npm ci
    npm run build
  )
  (
    cd "$ALTM_ROOT/adapters/deepseek-harness"
    npm ci
    npm run build
  )

  local package_dir="$ALTM_ROOT/data/plugin-pack"
  rm -rf "$package_dir"
  mkdir -p "$package_dir"
  (
    cd "$ALTM_ROOT/adapters/deepseek-harness"
    npm pack --pack-destination "$package_dir" >/dev/null
  )

  (
    cd "$DSH_REPO"
    if [[ ! -d node_modules ]]; then
      pnpm install
    fi
    pnpm run build
  )
}

install_plugin() {
  local restart_web=false
  if is_running web; then
    restart_web=true
  fi
  build_artifacts
  local package_path="$ALTM_ROOT/data/plugin-pack/altm-deepseek-harness-1.0.0.tgz"
  (
    cd "$DSH_REPO"
    if plugin_installed; then
      pnpm dsh plugin --profile "$PROFILE" remove @altm/deepseek-harness
    fi
    pnpm dsh plugin --profile "$PROFILE" add "$package_path"
  )
  node "$ALTM_ROOT/scripts/manage-altm-profile.mjs" \
    enable "$(profile_patch)" "$DSH_REPO"
  echo "enabled" >"$PLUGIN_STATE_FILE"
  echo "plugin installed and enabled"
  if [[ "$restart_web" == true ]]; then
    stop_one web
    start_process web "$DSH_REPO" \
      node --import tsx/esm apps/cli/src/bin.ts \
      --profile "$PROFILE" \
      --host 127.0.0.1 \
      --port "$WEB_PORT"
    wait_for_port web "$WEB_PORT" 200
    wait_for_http web "http://127.0.0.1:$WEB_PORT/" 200
  fi
}

setup_stack() {
  install_plugin
}

ensure_setup() {
  if [[ ! -f "$DSH_REPO/apps/web/dist/index.html" \
    || ! -f "$DSH_REPO/packages/client/ui-directory-picker-native/lib/index.js" \
    || ! -x "$LOCAL_BIN/pnpm" ]]; then
    build_artifacts
  fi
  if ! plugin_installed; then
    if [[ -f "$PLUGIN_STATE_FILE" ]] \
      && [[ "$(tr -d '[:space:]' <"$PLUGIN_STATE_FILE")" == "uninstalled" ]]; then
      echo "ALTM plugin is explicitly uninstalled; run '$0 install' first" >&2
      return 1
    fi
    install_plugin
  fi
}

enable_plugin() {
  load_environment
  if ! plugin_installed; then
    echo "ALTM plugin is not installed; run '$0 install' first" >&2
    return 1
  fi
  node "$ALTM_ROOT/scripts/manage-altm-profile.mjs" \
    enable "$(profile_patch)" "$DSH_REPO"
  echo "enabled" >"$PLUGIN_STATE_FILE"
  echo "plugin enabled"
}

disable_plugin() {
  load_environment
  if ! plugin_installed; then
    echo "ALTM plugin is not installed"
    return
  fi
  node "$ALTM_ROOT/scripts/manage-altm-profile.mjs" \
    disable "$(profile_patch)" "$DSH_REPO"
  echo "disabled" >"$PLUGIN_STATE_FILE"
  echo "plugin disabled"
}

uninstall_plugin() {
  load_environment
  if plugin_installed; then
    disable_plugin
    sleep 1
    (
      cd "$DSH_REPO"
      pnpm dsh plugin --profile "$PROFILE" remove @altm/deepseek-harness
    )
    node "$ALTM_ROOT/scripts/manage-altm-profile.mjs" \
      remove "$(profile_patch)" "$DSH_REPO"
  fi
  echo "uninstalled" >"$PLUGIN_STATE_FILE"
  echo "plugin uninstalled"
}

start_stack() {
  load_environment
  ensure_setup

  if [[ ! -f "$DB_PATH" ]]; then
    "$PYTHON" -m altm.cli init-db --db "$DB_PATH"
  fi

  start_process mcp "$ALTM_ROOT" \
    "$PYTHON" -m altm.cli mcp-server \
    --db "$DB_PATH" \
    --transport streamable-http \
    --profile runtime \
    --host 127.0.0.1 \
    --port "$MCP_PORT"
  wait_for_port mcp "$MCP_PORT"

  start_process worker "$ALTM_ROOT" \
    "$PYTHON" -m altm.cli worker \
    --db "$DB_PATH" \
    --worker-id harness-worker-1 \
    --poll-seconds 1

  start_process web "$DSH_REPO" \
    node --import tsx/esm apps/cli/src/bin.ts \
    --profile "$PROFILE" \
    --host 127.0.0.1 \
    --port "$WEB_PORT"
  wait_for_port web "$WEB_PORT" 200
  wait_for_http web "http://127.0.0.1:$WEB_PORT/" 200

  status_stack
}

stop_one() {
  local name="$1"
  local pid
  pid="$(read_pid "$name" || true)"
  if [[ -z "$pid" ]]; then
    echo "$name is not running"
    return
  fi
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for ((index = 0; index < 50; index += 1)); do
      if ! kill -0 "$pid" 2>/dev/null; then
        break
      fi
      sleep 0.2
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$(pid_file "$name")"
  echo "$name stopped"
}

stop_stack() {
  stop_one web
  stop_one worker
  stop_one mcp
}

status_one() {
  local name="$1"
  if is_running "$name"; then
    echo "$name=running pid=$(read_pid "$name") log=$(log_file "$name")"
  else
    echo "$name=stopped log=$(log_file "$name")"
  fi
}

status_stack() {
  load_environment
  echo "plugin=$(plugin_status)"
  status_one mcp
  status_one worker
  status_one web
  if lsof -nP -iTCP:"$MCP_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "mcp_endpoint=http://127.0.0.1:$MCP_PORT/mcp"
  fi
  if lsof -nP -iTCP:"$WEB_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "web_url=http://127.0.0.1:$WEB_PORT"
  fi
  echo "database=$DB_PATH"
}

show_logs() {
  local name="${1:-}"
  if [[ -n "$name" ]]; then
    tail -n 100 "$(log_file "$name")"
    return
  fi
  for item in mcp worker web; do
    echo "== $item =="
    tail -n 40 "$(log_file "$item")" 2>/dev/null || true
  done
}

case "${1:-start}" in
  setup)
    setup_stack
    ;;
  install)
    install_plugin
    ;;
  enable)
    enable_plugin
    ;;
  disable)
    disable_plugin
    ;;
  uninstall)
    uninstall_plugin
    ;;
  start)
    start_stack
    ;;
  restart)
    stop_stack
    start_stack
    ;;
  status)
    status_stack
    ;;
  logs)
    show_logs "${2:-}"
    ;;
  stop)
    stop_stack
    ;;
  *)
    echo "usage: $0 {setup|install|enable|disable|uninstall|start|restart|status|logs [mcp|worker|web]|stop}" >&2
    exit 2
    ;;
esac
