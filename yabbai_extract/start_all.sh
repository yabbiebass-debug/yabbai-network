#!/usr/bin/env bash
# YABBAI Network V2 — Unix/Mac/Linux launcher
# Starts all Python backends in background processes

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "============================================================"
echo " YABBAI NETWORK V2 — starting all services"
echo "============================================================"

# Load .env
if [ -f .env ]; then
  export $(grep -v '^#' .env | grep -v '^$' | xargs)
fi

if [ -z "$ADMIN_API_KEY" ]; then
  echo "WARNING: ADMIN_API_KEY not set. Copy .env.example to .env first."
fi

LOG_DIR="$SCRIPT_DIR/.logs"
mkdir -p "$LOG_DIR"

start_svc() {
  local name="$1" module="$2" port="$3"
  echo "  starting $name → http://localhost:$port"
  python -m uvicorn "$module" \
    --host 0.0.0.0 --port "$port" --log-level warning \
    > "$LOG_DIR/${name}.log" 2>&1 &
  echo $! > "$LOG_DIR/${name}.pid"
}

start_svc "revenue"   "revenue_system.unified_server:app"  "${REVENUE_PORT:-7870}"
start_svc "ai"        "yabbai_local.api.server:app"         "${AI_PORT:-7860}"
start_svc "defi"      "defi_simulator.api.server:app"       "${DEFI_PORT:-8002}"
start_svc "goldscout" "goldscout.server:app"                 "${GOLDSCOUT_PORT:-8001}"
start_svc "ops"       "yabbai_ops.server:app"               "${OPS_PORT:-7880}"

sleep 2
echo ""
echo " All services started. Logs → .logs/"
echo "   Revenue System  → http://localhost:${REVENUE_PORT:-7870}"
echo "   YABBAI AI       → http://localhost:${AI_PORT:-7860}"
echo "   DeFi Simulator  → http://localhost:${DEFI_PORT:-8002}"
echo "   GoldScout       → http://localhost:${GOLDSCOUT_PORT:-8001}"
echo "   Ops Cockpit     → http://localhost:${OPS_PORT:-7880}"
echo ""
echo " Open hub/index.html in your browser or:"
echo "   python yabbai_network_server.py   # unified gateway on :8080"
echo "============================================================"
echo ""
echo " To stop all:  kill \$(cat .logs/*.pid 2>/dev/null)"
