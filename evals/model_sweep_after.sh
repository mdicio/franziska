#!/usr/bin/env bash
# Second half of the sweep (model_sweep.sh was stopped at 02:25 during "q38 base medium", which runs on): the bot's
# current gemma4:12b on the hard set as a baseline, the q36 hard set again (its first stage ran before the schedule
# lock fix, 01:07), a third hard pass for gemma4-26b; then rescore triage rows judged before m24 was allowed.
cd "$(dirname "$0")/.."
DEADLINE=$(date -d '03:05' +%s); DIRS=evals/runs/sweep.dirs; LOG=evals/runs/sweep.log
report() { uv run --quiet python -m evals.model_report $(cat $DIRS) > docs/MODEL_FINDINGS.md.tmp && mv docs/MODEL_FINDINGS.md.tmp docs/MODEL_FINDINGS.md; }
while pgrep -f "python -m evals.mail_eval" >/dev/null; do sleep 15; done
echo "evals/runs/20261028-022003/" >> $DIRS; report
echo "$(date +%T) done q38 base medium x1" >> $LOG
run() {  # run <label> <mail_eval args...>
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then echo "$(date +%T) skip $1 (deadline)" >> $LOG; return; fi
  echo "$(date +%T) start $1" >> $LOG
  uv run --quiet python -m evals.mail_eval "${@:2}" >> $LOG 2>&1
  ls -td evals/runs/*/ | head -1 >> $DIRS; report
  echo "$(date +%T) done $1" >> $LOG
}
run "gemma4-12b hard x1 (current bot model)" --servers gemma4-12b --only hard --repeats 1
run "q36 hard x1 after lock fix" --servers qwen3.6-35b-a3b --only hard --repeats 1
run "g26 hard x1 (third pass)" --servers gemma4-26b-a4b --only hard --repeats 1
uv run --quiet python evals/rescore_triage.py $(sed 's|/$||;s|$|/results.jsonl|' $DIRS); report
echo "$(date +%T) all done" >> $LOG
