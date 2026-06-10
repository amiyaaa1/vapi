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

  if [ "${TURNSTILE_SOLVER_DEBUG:-0}" = "1" ]; then
    debug_flag="--debug"
  fi

  if [ "${TURNSTILE_SOLVER_HEADLESS:-0}" = "1" ]; then
    headed_flag=""
  fi

  if [ -n "${SOCKS5_PROXY:-}" ]; then
    printf '%s\n' "${SOCKS5_PROXY}" > "${solver_dir}/proxies.txt"
    proxy_flag="--proxy"
  fi

  if [ "${TURNSTILE_SOLVER_RANDOM:-1}" = "1" ]; then
    random_flag="--random"
  fi

  cd "${solver_dir}"
  if [ -n "${headed_flag}" ]; then
    xvfb-run -a python3 api_solver.py ${headed_flag} --browser_type "${solver_browser}" --thread "${solver_threads}" --host 127.0.0.1 --port "${solver_port}" ${proxy_flag} ${random_flag} ${debug_flag} > /tmp/turnstile-solver.log 2>&1 &
  else
    python3 api_solver.py --browser_type "${solver_browser}" --thread "${solver_threads}" --host 127.0.0.1 --port "${solver_port}" ${proxy_flag} ${random_flag} ${debug_flag} > /tmp/turnstile-solver.log 2>&1 &
  fi
  cd /app
}

start_solver
exec /app/gateway
