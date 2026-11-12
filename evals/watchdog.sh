#!/usr/bin/env bash
# Watches evals/campaign.py and exits (one line saying why) on: all done, the campaign gone without finishing, or a
# stall: no eval file written for STALL_MIN minutes AND the GPU idle for 3 minutes (llama-bench writes nothing for
# minutes while it works, so files alone aren't enough). On this box `find` is bfs: -mmin, not -newermt.
cd "$(dirname "$0")/.."
ST=evals/runs/campaign-2026-11/status.json
STALL_MIN=${STALL_MIN:-20}
idle=0
while true; do
  state=$(python3 -c "import json;print(json.load(open('$ST')).get('state',''))" 2>/dev/null)
  pid=$(python3 -c "import json;print(json.load(open('$ST')).get('pid',''))" 2>/dev/null)
  case "$state" in
    "all done"|"finished with steps left"|interrupted)
      echo "CAMPAIGN $state: $(tail -3 evals/runs/campaign-2026-11/log.txt | tr '\n' ' ')"; exit 0 ;;
  esac
  if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
    echo "CRASH: campaign pid $pid is gone, state '$state'. $(tail -3 evals/runs/campaign-2026-11/log.txt | tr '\n' ' ')"; exit 1
  fi
  util=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | tr -d ' ')
  [ "${util:-0}" -lt 10 ] && idle=$((idle+1)) || idle=0
  recent=$(find evals/runs evals/speed -type f -mmin -"$STALL_MIN" -print -quit 2>/dev/null)
  if [ -z "$recent" ] && [ "$idle" -ge 3 ]; then
    echo "STALL: nothing written for ${STALL_MIN} min, GPU idle ${util}%. step: $(cat $ST)"; exit 2
  fi
  sleep 60
done
