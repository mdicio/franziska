#!/usr/bin/env bash
# Overnight model sweep: speed of the 27B placements, then the mail eval on the candidates. Each stage appends its
# run dir to evals/runs/sweep.dirs and rewrites docs/MODEL_FINDINGS.md, so a shutdown loses at most one stage.
cd "$(dirname "$0")/.."
DEADLINE=${DEADLINE:-$(date -d '02:55' +%s)}
DIRS=evals/runs/sweep.dirs
LOG=evals/runs/sweep.log
touch $DIRS

report() { uv run --quiet python -m evals.model_report $(cat $DIRS) > docs/MODEL_FINDINGS.md.tmp && mv docs/MODEL_FINDINGS.md.tmp docs/MODEL_FINDINGS.md; }

stage() {  # stage <label> <mail_eval args...>
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then echo "$(date +%T) skip $1 (deadline)" >> $LOG; return; fi
  echo "$(date +%T) start $1" >> $LOG
  uv run --quiet python -m evals.mail_eval "${@:2}" >> $LOG 2>&1
  ls -td evals/runs/*/ | head -1 >> $DIRS
  report
  echo "$(date +%T) done $1" >> $LOG
}

echo "$(date +%T) sweep start" >> $LOG
uv run --quiet python evals/speed/speed_bench.py q38iq3-24kq8 q38iq3-16kq4 q38iq3-16kq4-mtp q38q4-32kq4 q38q4-ffncpu \
  >> $LOG 2>&1
report
stage "q36 hard x2" --servers qwen3.6-35b-a3b --only hard --repeats 2
stage "g26 hard x2" --servers gemma4-26b-a4b --only hard --repeats 2
stage "q38 hard low/medium/xhigh x1" --servers qwen3.8-27b-iq3 --only hard --reasoning low,medium,xhigh --repeats 1
stage "q36+g26 base x1" --servers qwen3.6-35b-a3b,gemma4-26b-a4b --repeats 1
stage "q38 base medium x1" --servers qwen3.8-27b-iq3 --reasoning medium --repeats 1
stage "gemma4-12b hard x1 (current bot model)" --servers gemma4-12b --only hard --repeats 1
stage "q38 hard medium extra" --servers qwen3.8-27b-iq3 --only hard --reasoning medium --repeats 1
echo "$(date +%T) sweep end" >> $LOG
