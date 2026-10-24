#!/bin/bash
# JARVIS Start Script
# Launches backend (python3.11) + frontend dev server

JARVIS_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$JARVIS_DIR"

echo "======================================"
echo "  J.A.R.V.I.S. Starting Up"
echo "======================================"

# Check SSL certs
if [ ! -f cert.pem ] || [ ! -f key.pem ]; then
    echo "[ssl] Generating self-signed certificate..."
    openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem \
        -days 365 -nodes -subj '/CN=localhost' 2>/dev/null
    echo "[ssl] Done"
fi

# Start backend
echo "[backend] Starting on https://localhost:8340 ..."
if launchctl list com.jarvis.server >/dev/null 2>&1; then
    echo "[backend] com.jarvis.server LaunchAgent is running it already — open https://localhost:8340"
    exit 0
fi
python3.11 server.py &
BACKEND_PID=$!
echo "[backend] PID $BACKEND_PID"

# Start frontend
echo "[frontend] Starting on https://localhost:5173 ..."
cd frontend && npm run dev &
FRONTEND_PID=$!
echo "[frontend] PID $FRONTEND_PID"

echo ""
echo "======================================"
echo "  JARVIS is running"
echo "  Open: https://localhost:5173"
echo "  Click anywhere to initialize"
echo "======================================"
echo ""
echo "Press Ctrl+C to stop both servers"

# Wait — kill both on exit
trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit" INT TERM
wait
