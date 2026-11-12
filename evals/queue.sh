#!/usr/bin/env bash
# The model runs waiting for a free GPU (evals/reports/2026-10-29/queue-results.md), in order. Waits until no other eval holds the GPU lock and
# no eval process runs, then runs each step with its own time cap; one failing step doesn't stop the next.
#
#   evals/queue.sh            # wait, then run everything (~60 min worst case, every step capped)
#   evals/queue.sh router     # only the steps whose label contains 'router'
#
# Log: evals/runs/queue.log. Each eval writes its own evals/runs/<name>-<time>/summary.md.
cd "$(dirname "$0")/.."
LOG=evals/runs/queue.log
mkdir -p evals/runs
busy() {
  pgrep -f "^[^ ]*python[^ ]* -m evals\.(mail_eval|week|memory|router_eval|bench)" >/dev/null && return 0
  uv run --quiet python -c "from franziska import gpu; import sys; sys.exit(0 if gpu.holder().startswith(gpu.SESSION) else 1)"
}
while busy; do sleep 60; done
echo "$(date '+%F %T') GPU free, starting" >> "$LOG"
step() {  # step <label> <minutes cap> <command...>
  [[ -n "$1" && -n "$FILTER" && "$1" != *"$FILTER"* ]] && return
  echo "$(date +%T) start $1" >> "$LOG"
  timeout "$(( $2 * 60 ))" uv run --env-file .env "${@:3}" >> "$LOG" 2>&1
  echo "$(date +%T) done $1 (exit $?)" >> "$LOG"
}
FILTER="$1"
# 1. The bot's own model first: the baselines everything else compares with.
step "week (resident model)" 25 python -m evals.week --budget-min 20
step "memory (resident model)" 15 python -m evals.memory --budget-min 10
step "router resident" 10 python -m evals.router_eval --resident
# 2. Router calibration on the other GGUFs (stops the resident server, starts it again after).
step "router models" 25 python -m evals.router_eval --servers gemma4-26b-a4b,qwen3.6-35b-a3b,qwen3.8-27b-iq3
# 3. Model bench (evals/bench.py): gemma vs qwen on the same ten coding/agent tasks, ~20 min per model. Rerun with `evals/queue.sh bench`.
step "bench (gemma4-26b vs qwen3.8-27b-gsq)" 45 python -m evals.bench
echo "$(date '+%F %T') queue done" >> "$LOG"
