"""Eval gate: compare current run vs baseline; fail if regression > threshold.

Run in CI:
  python -m eval.gate --current eval/results.json --baseline eval/baseline.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

THRESHOLD = 0.02  # 2 percentage points


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--current", required=True)
    p.add_argument("--baseline", required=True)
    p.add_argument("--threshold", type=float, default=THRESHOLD)
    args = p.parse_args()

    cur = json.loads(Path(args.current).read_text())["summary"]["overall"]
    base = json.loads(Path(args.baseline).read_text())["summary"]["overall"]

    regressions = []
    for metric in ["recall@10", "mrr", "ndcg@10", "faithfulness"]:
        delta = cur.get(metric, 0) - base.get(metric, 0)
        line = f"  {metric:14s}  baseline={base[metric]:.3f}  current={cur[metric]:.3f}  delta={delta:+.3f}"
        if delta < -args.threshold:
            regressions.append(line)
            print("❌" + line)
        else:
            print("✅" + line)

    if regressions:
        print(f"\nFAIL: {len(regressions)} metric(s) regressed by more than {args.threshold}.")
        sys.exit(1)
    print("\nPASS")


if __name__ == "__main__":
    main()
