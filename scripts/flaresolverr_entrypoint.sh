#!/bin/bash
set -e

# Start Xvfb and FlareSolverr in the background
/usr/bin/dumb-init -- /usr/local/bin/python -u /app/flaresolverr.py &
FS_PID=$!

# Wait for Xvfb and FlareSolverr to start
sleep 4

# Start the HAZ Turnstile Solver service on port 8192
python /app/haz_solver.py &
HAZ_PID=$!

# Handle graceful shutdown
trap "kill -TERM $FS_PID $HAZ_PID 2>/dev/null || true" SIGTERM SIGINT

wait -n $FS_PID $HAZ_PID
