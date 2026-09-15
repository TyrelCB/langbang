#!/usr/bin/env bash
# Samples sglang decode/prefill stats from spark-ee93 every SAMPLE_SECS into
# data/perf.log (JSONL). Run: tools/watch_sglang.sh &
HOST=${SPARK_HOST:-spark-ee93}
CONTAINER=${SGLANG_CONTAINER:-sglang-qwen38-flash-next}
SAMPLE_SECS=${SAMPLE_SECS:-30}
OUT="$(dirname "$0")/../data/perf.log"
mkdir -p "$(dirname "$OUT")"

while true; do
  ssh -o BatchMode=yes -o ConnectTimeout=5 "$HOST" \
    "docker logs --since ${SAMPLE_SECS}s --tail 50 $CONTAINER 2>&1 | grep -E 'Decode batch|Prefill batch' | tail -4" \
    | while IFS= read -r line; do
        ts=$(date -u +%FT%TZ)
        tok=$(echo "$line" | grep -oP 'token usage: [0-9.]+' | cut -d' ' -f3)
        rate=$(echo "$line" | grep -oP '(gen|input) throughput \(token/s\): [0-9.]+' | grep -oP '[0-9.]+$')
        kind=$(echo "$line" | grep -oP 'Decode|Prefill' | head -1)
        echo "{\"ts\":\"$ts\",\"kind\":\"$kind\",\"kv_usage\":$tok,\"throughput\":$rate}"
      done >> "$OUT"
  sleep "$SAMPLE_SECS"
done
