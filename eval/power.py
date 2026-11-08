"""Power simulation for the paired McNemar comparisons, from pilot discordance rates.

Usage:  uv run python -m eval.power [--budget 6.0] [--freeze]
Local context: estimate P(A right, B wrong) and P(A wrong, B right) from the pilot, simulate trials of size
n (multinomial draws), and report the power of the one-sided exact McNemar test at alpha = 0.05, plus the
minimum detectable effect (MDE) for the chosen n at 80 % power. Also prices the confirmatory run from the
pilot's measured cost per claim.
Global context: its output fixes N_web in reports/preregistration.md before any confirmatory run.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy.stats import binomtest

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "results" / "pilot"
OUT = ROOT / "results" / "power.json"


def load(cond: str) -> dict[str, dict]:
    return {r["item_id"]: r for r in map(json.loads, (PILOT / f"{cond}.jsonl").read_text(encoding="utf-8").splitlines())}


def discordance(a: dict, b: dict) -> tuple[float, float, int]:
    ids = sorted(set(a) & set(b))
    pb = np.mean([a[i]["correct"] and not b[i]["correct"] for i in ids])
    pc = np.mean([b[i]["correct"] and not a[i]["correct"] for i in ids])
    return float(pb), float(pc), len(ids)


def power(pb: float, pc: float, n: int, sims: int = 4000, alpha: float = 0.05, seed: int = 0) -> float:
    rng = np.random.default_rng(seed)
    draws = rng.multinomial(n, [pb, pc, max(0.0, 1 - pb - pc)], size=sims)
    hits = 0
    cache: dict[tuple[int, int], bool] = {}
    for b, c, _ in draws:
        key = (int(b), int(c))
        if key not in cache:
            cache[key] = b + c > 0 and binomtest(int(b), int(b + c), 0.5, alternative="greater").pvalue < alpha
        hits += cache[key]
    return hits / sims


def mde(discordant_rate: float, n: int, target: float = 0.8) -> float:
    """Smallest accuracy difference (pb - pc) detectable with `target` power, holding pb + pc fixed."""
    for delta in np.arange(0.0, discordant_rate + 1e-9, 0.005):
        pb, pc = (discordant_rate + delta) / 2, (discordant_rate - delta) / 2
        if power(pb, pc, n, sims=2000) >= target:
            return float(delta)
    return float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=6.0, help="USD reserved for the confirmatory eval (H1-H4, E2)")
    ap.add_argument("--h3-and-other-usd", type=float, default=None,
                    help="USD to reserve for H3/H4/E2/E3 (default: estimated from pilot closed-book cost)")
    ap.add_argument("--freeze", action="store_true", help="also draw the dev sample and fill the preregistration")
    args = ap.parse_args()
    arms = {c: load(c) for c in ("closed_book", "web_blocked", "web_default")}
    cost = {c: float(np.mean([r["cost_usd"] for r in arms[c].values()])) for c in arms}
    acc = {c: float(np.mean([r["correct"] for r in arms[c].values()])) for c in arms}
    # H3 = 2 arms x 500 claims x (extraction + closed-book verify); H4 ~ 23 extraction calls; E2 = 200 closed-book;
    # E3 ~ 60 judge calls. Extraction cost per claim is taken as ~0.6x a closed-book verification (pilot smoke runs).
    other = args.h3_and_other_usd or (2 * 500 * cost["closed_book"] * 1.6 + 23 * 0.002 + 200 * cost["closed_book"] + 60 * 0.001)
    web_per_item = cost["web_blocked"] + cost["web_default"] + cost["closed_book"]
    n_affordable = int((args.budget - other) / web_per_item)
    n_web = max(0, min(200, n_affordable - n_affordable % 10))   # round down to a multiple of 10
    comparisons = {"H1 web_blocked vs closed_book": ("web_blocked", "closed_book"),
                   "H2 web_default vs web_blocked": ("web_default", "web_blocked")}
    report = {"pilot_n": len(arms["closed_book"]), "pilot_accuracy": acc, "pilot_cost_per_claim_usd": cost,
              "reserved_other_usd": round(other, 3), "web_cost_per_item_usd": round(web_per_item, 4),
              "n_web": n_web, "comparisons": {}}
    for name, (a, b) in comparisons.items():
        pb, pc, _n = discordance(arms[a], arms[b])
        report["comparisons"][name] = {
            "pilot_p_a_only": pb, "pilot_p_b_only": pc, "pilot_effect": pb - pc,
            "power_at_n_web_if_pilot_effect_true": power(pb, pc, n_web) if n_web else None,
            "mde_at_n_web_80pct_power": mde(pb + pc, n_web) if n_web and pb + pc > 0 else None,
            "power_curve": {str(k): power(pb, pc, k) for k in (50, 80, 100, 120, 150, 200)},
        }
    OUT.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    if args.freeze:
        write_sample(n_web)
        fill_prereg(report)


def write_sample(n: int, seed: int = 2026) -> None:
    """Label-stratified random sample of the AVeriTeC dev set (proportional allocation, largest remainder)."""
    from eval.datasets import load_averitec

    dev = load_averitec("dev")
    rng = np.random.default_rng(seed)
    labels = sorted({c.label for c in dev})
    quotas = {lab: n * sum(c.label == lab for c in dev) / len(dev) for lab in labels}
    alloc = {lab: int(q) for lab, q in quotas.items()}
    for lab in sorted(labels, key=lambda lab: quotas[lab] - alloc[lab], reverse=True)[: n - sum(alloc.values())]:
        alloc[lab] += 1
    ids = []
    for lab in labels:
        pool = [c.item_id for c in dev if c.label == lab]
        ids += list(rng.choice(pool, size=alloc[lab], replace=False))
    ids = [str(i) for i in rng.permutation(ids)]
    path = ROOT / "results" / "samples" / "dev_web_sample.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"seed": seed, "n": n, "allocation": alloc, "item_ids": ids}, indent=1))
    print("sample:", alloc)


def fill_prereg(r: dict) -> None:
    from eval.report import fill

    lines = [f"Pilot: {r['pilot_n']} AVeriTeC *train* claims × 3 arms (`results/pilot/`). Measured cost per claim: "
             + ", ".join(f"{k} ${v:.4f}" for k, v in r["pilot_cost_per_claim_usd"].items())
             + f". Budget reserved for H3/H4/E2/E3: ${r['reserved_other_usd']:.2f}; remaining eval budget buys "
             f"**N_web = {r['n_web']}** claims × 3 arms (${r['web_cost_per_item_usd']:.4f} per claim).", "",
             "| Comparison | pilot P(A only) | pilot P(B only) | power at N_web if the pilot effect is real | MDE at N_web (80 % power) |",
             "|---|---|---|---|---|"]
    for name, c in r["comparisons"].items():
        power_at = c["power_at_n_web_if_pilot_effect_true"]
        mde_val = c["mde_at_n_web_80pct_power"]
        lines.append(f"| {name} | {c['pilot_p_a_only']:.2f} | {c['pilot_p_b_only']:.2f} | "
                     f"{'–' if power_at is None else f'{power_at:.2f}'} | "
                     f"{'–' if mde_val is None or np.isnan(mde_val) else f'{100 * mde_val:.1f} accuracy points'} |")
    lines += ["", ("Effects smaller than the MDE will be reported as inconclusive, not as evidence of no effect. "
                   "Generated by `uv run python -m eval.power --freeze` from `results/power.json`.")]
    fill(ROOT / "reports" / "preregistration.md", {"power": "\n".join(lines)})


if __name__ == "__main__":
    main()
