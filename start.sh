#!/bin/bash
# Start backend, worker, and frontend dev servers
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"

source "$ROOT/venv/bin/activate"
cd "$ROOT"

echo "Starting API server on http://localhost:8000 ..."
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload &
BACKEND_PID=$!

echo "Starting worker on http://localhost:8001 ..."
python -m uvicorn app.worker.app:app --host 0.0.0.0 --port 8001 --reload &
WORKER_PID=$!

echo "Starting frontend on http://localhost:5173 ..."
cd "$ROOT/frontend"
npm run dev &
FRONTEND_PID=$!

echo ""
echo "  API server: http://localhost:8000"
echo "  Worker:     http://localhost:8001"
echo "  Frontend:   http://localhost:5173"
echo ""
echo "Press Ctrl+C to stop all servers."

trap "kill $BACKEND_PID $WORKER_PID $FRONTEND_PID 2>/dev/null" EXIT INT TERM
wait
