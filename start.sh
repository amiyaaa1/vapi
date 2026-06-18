#!/bin/sh
set -eu

start_solver() {
  if [ "${TURNSTILE_SOLVER_ENABLED:-1}" = "0" ]; then
    return 0
  fi

  solver_port="${TURNSTILE_SOLVER_PORT:-5000}"
  solver_threads="${TURNSTILE_SOLVER_THREADS:-1}"
  solver_browser="${TURNSTILE_SOLVER_BROWSER_TYPE:-chromium}"
  solver_dir="/app/nexos_solver"
  debug_flag=""
  proxy_flag=""
  headed_flag="--no-headless"
  random_flag=""

  is_direct_proxy() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
      ""|0|none|no|off|direct|direct://) return 0 ;;
      *) return 1 ;;
    esac
  }

  if [ "${TURNSTILE_SOLVER_DEBUG:-0}" = "1" ]; then
    debug_flag="--debug"
  fi

  if [ "${TURNSTILE_SOLVER_HEADLESS:-0}" = "1" ]; then
    headed_flag=""
  fi

  solver_proxy=""
  force_solver_proxy="${TURNSTILE_SOLVER_FORCE_PROXY:-1}"
  for candidate in "${TURNSTILE_SOLVER_PROXY:-}" "${WARP_PROXY_URL:-}" "${SOCKS5_PROXY:-}" "${BILLING_BIND_PROXY:-}"; do
    if [ "${force_solver_proxy}" = "1" ] || [ "${force_solver_proxy}" = "true" ] || [ "${force_solver_proxy}" = "TRUE" ] || [ "${force_solver_proxy}" = "yes" ] || [ "${force_solver_proxy}" = "on" ]; then
      if ! is_direct_proxy "${candidate}"; then
        solver_proxy="${candidate}"
        break
      fi
    elif [ -n "${candidate}" ]; then
      solver_proxy="${candidate}"
      break
    fi
  done

  if [ -n "${solver_proxy}" ]; then
    printf '%s\n' "${solver_proxy}" > "${solver_dir}/proxies.txt"
    proxy_flag="--proxy"
  fi

  if [ "${TURNSTILE_SOLVER_RANDOM:-1}" = "1" ]; then
    random_flag="--random"
  fi

  if [ "${TURNSTILE_SOLVER_CLOAK:-0}" = "1" ]; then
    solver_browser="cloak"
  fi

  cd "${solver_dir}"
  if [ -n "${headed_flag}" ]; then
    xvfb-run -a python3 api_solver.py ${headed_flag} --browser_type "${solver_browser}" --thread "${solver_threads}" --host 127.0.0.1 --port "${solver_port}" ${proxy_flag} ${random_flag} ${debug_flag} > /tmp/turnstile-solver.log 2>&1 &
  else
    python3 api_solver.py --browser_type "${solver_browser}" --thread "${solver_threads}" --host 127.0.0.1 --port "${solver_port}" ${proxy_flag} ${random_flag} ${debug_flag} > /tmp/turnstile-solver.log 2>&1 &
  fi
  cd /app
}

start_chat_helper() {
  if [ "${VAPI_CHAT_HELPER_DAEMON_ENABLED:-1}" = "0" ]; then
    return 0
  fi

  helper_host="${VAPI_CHAT_HELPER_HOST:-127.0.0.1}"
  helper_port="${VAPI_CHAT_HELPER_PORT:-8099}"
  helper_log="${VAPI_CHAT_HELPER_LOG:-/tmp/vapi-chat-helper.log}"

  python3 /app/scripts/vapi_chat_daemon.py --host "${helper_host}" --port "${helper_port}" > "${helper_log}" 2>&1 &

  wait_seconds="${VAPI_CHAT_HELPER_STARTUP_WAIT_SECONDS:-10}"
  i=0
  while [ "${i}" -lt "${wait_seconds}" ]; do
    if python3 - "${helper_host}" "${helper_port}" <<'PYWAIT' >/dev/null 2>&1
import sys
import urllib.request
host, port = sys.argv[1], sys.argv[2]
urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=1).read()
PYWAIT
    then
      return 0
    fi
    i=$((i + 1))
    sleep 1
  done
  echo "WARNING: vapi chat helper did not become ready within ${wait_seconds}s, gateway will rely on fallback if enabled" >&2
}

start_solver
start_chat_helper
exec /app/gateway
