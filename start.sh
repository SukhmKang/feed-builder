#!/bin/bash
# Start backend and frontend dev servers
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "Starting backend on http://localhost:8000 ..."
source "$ROOT/venv/bin/activate"
cd "$ROOT"
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload &
BACKEND_PID=$!

echo "Starting frontend on http://localhost:5173 ..."
cd "$ROOT/frontend"
npm run dev &
FRONTEND_PID=$!

echo ""
echo "  Backend:  http://localhost:8000"
echo "  Frontend: http://localhost:5173"
echo ""
echo "Press Ctrl+C to stop both servers."

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null" EXIT INT TERM
wait
