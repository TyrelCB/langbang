# keep_reasoning A/B

`ab_reasoning.py REPS OUT.json` runs 5 read-only multi-step tasks × {off, turn}
× REPS in throwaway threads (per-thread `keep_reasoning` override, 2 at a time),
`ab_analyze.py RUNS.json [...] ROWS.json` grades the `ANSWER:` lines and prints
steps / tool calls / errors / duplicate calls / prompt tokens / wall time.

Ground truths are hard-coded for the repo as of 2026-10-07 (route counts, file
line counts, ttsnorm.py's first commit, the 10-01..10-06 churn) — recompute
them before re-running on a newer tree. Pause `skill_review` while it runs, and
delete the `lbtest ab …` threads afterwards.

## 2026-10-07 result (Qwen3.8-Flash-Next-NVFP4, thinking on, xhigh; 24 runs per arm)

| arm  | perfect | steps | tool calls | tool errors | dup calls | max ctx | Σ prompt tok | out tok | wall s |
|------|---------|-------|-----------|-------------|-----------|---------|-------------|---------|--------|
| off  | 23/24   | 5.8   | 6.2       | 0.12        | 0         | 17.4k   | 98.2k       | 1778    | 116    |
| turn | 24/24   | 5.8   | 6.4       | 0.08        | 0         | 18.2k   | 101.4k      | 1943    | 122    |

The single "off" miss ended without an answer (not a wrong one). Within noise:
no measurable gain on short (4–11 step) tasks, ~3% more prompt tokens. Long runs
(50+ steps, where the Check Email plan-mode flip-flop happened) weren't tested.
Default stays `off`; the option + per-thread override remain.
