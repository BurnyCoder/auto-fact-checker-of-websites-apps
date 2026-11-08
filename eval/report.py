"""Regenerate every number, table and figure from results/ (single command: `uv run python -m eval.report`).

Local context: load per-claim result files, run the preregistered analysis (eval/metrics.py), write
results/summary.json, reports/generated/*.md tables, reports/figures/*.png, and fill the
<!-- GEN:name --> ... <!-- /GEN:name --> blocks in README.md, reports/*.md and the paper.
Global context: nothing in the reports is typed by hand; re-running this after new results updates all of them.
Chart style follows the dataviz reference palette (categorical slots blue/orange/aqua for conditions,
thin marks, recessive grid); verdict labels keep the app's semantic colours.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from eval import metrics as m
from factcheck.models import LABELS

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
FIG = ROOT / "reports" / "figures"
GEN = ROOT / "reports" / "generated"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]            # categorical slots 1-3 (validated all-pairs, light mode)
TEXT, MUTED, GRID = "#0b0b0b", "#52514e", "#e6e5e1"
ARM_NAMES = {"closed_book": "Closed-book (no web)", "web_blocked": "Web, fact-check sites blocked",
             "web_default": "Web, default (product)", "h3_clean": "Clean text", "h3_ocr": "Rendered → OCR"}
SHORT = {"Supported": "Supp.", "Refuted": "Ref.", "Not Enough Evidence": "NEE", "Conflicting Evidence/Cherrypicking": "Confl."}
AUDIT_TERMS = ("fact-check", "factcheck", "fact check", "fact checked", "fact-checked")


# ---------------------------------------------------------------------------------------------- loading
def load(exp: str, cond: str, repeat: int | None = 1) -> list[dict]:
    path = RESULTS / exp / f"{cond}.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r for r in rows if repeat is None or r.get("repeat", 1) == repeat]


def by_id(rows: list[dict]) -> dict[str, dict]:
    return {r["item_id"]: r for r in rows}


def pred_or_none(r: dict) -> str:
    return r.get("pred") or "NONE"


# ---------------------------------------------------------------------------------------------- analyses
def arm_summary(rows: list[dict]) -> dict:
    gold = [r["gold"] for r in rows]
    pred = [pred_or_none(r) for r in rows]
    correct = [bool(r["correct"]) for r in rows]
    conf = [float(r.get("confidence") or 0.0) for r in rows]
    sr = [(g, p) for g, p in zip(gold, pred) if g in ("Supported", "Refuted")]
    sources = [s for r in rows for s in (r.get("verdict") or {}).get("sources", [])]
    checked = [s for s in sources if s.get("quote_verified") is not None]
    sandbox = [r for r in rows if (r.get("tool_counts") or {}).get("mcp__sandbox__bash")]
    fetched = [r for r in rows if (r.get("tool_counts") or {}).get("mcp__sandbox__fetch_file")]
    return {
        "n": len(rows),
        "accuracy": m.accuracy(gold, pred), "accuracy_ci95": m.bootstrap_ci(m.accuracy, gold, pred),
        "macro_f1": m.macro_f1(gold, pred), "macro_f1_ci95": m.bootstrap_ci(m.macro_f1, gold, pred),
        "accuracy_3class": m.accuracy(m.three_class(gold), m.three_class(pred)),
        "macro_f1_3class": m.macro_f1(m.three_class(gold), m.three_class(pred), labels=("Supported", "Refuted", "Other")),
        "accuracy_supported_vs_refuted_subset": m.accuracy([g for g, _ in sr], [p for _, p in sr]) if sr else None,
        "n_supported_vs_refuted_subset": len(sr),
        "per_class": m.per_class(gold, pred), "confusion": m.confusion(gold, pred),
        "pred_distribution": {lab: pred.count(lab) for lab in [*LABELS, "NONE"]},
        "brier": m.brier(conf, correct), "ece": m.ece_equal_mass(conf, correct), "mean_confidence": float(np.mean(conf)),
        "reliability": m.reliability(conf, correct), "selective": m.selective_accuracy(conf, correct),
        "cost_per_claim_usd": float(np.mean([r.get("cost_usd") or 0 for r in rows])),
        "total_cost_usd": float(np.sum([r.get("cost_usd") or 0 for r in rows])),
        "latency_p50_s": float(np.percentile([r.get("wall_s") or 0 for r in rows], 50)),
        "latency_p95_s": float(np.percentile([r.get("wall_s") or 0 for r in rows], 95)),
        "searches_per_claim": float(np.mean([r.get("web_searches") or 0 for r in rows])),
        "errors": sum(1 for r in rows if r.get("error")),
        "sources_per_claim": len(sources) / max(1, len(rows)),
        "quote_checked_rate": len(checked) / max(1, len(sources)),
        "quote_verified_rate_of_checked": sum(bool(s["quote_verified"]) for s in checked) / max(1, len(checked)),
        "source_in_trace_rate": sum(bool(s.get("in_trace")) for s in sources) / max(1, len(sources)),
        "sandbox_use_rate": len(sandbox) / max(1, len(rows)),
        "accuracy_when_sandbox_used": float(np.mean([r["correct"] for r in sandbox])) if sandbox else None,
        "fetch_file_use_rate": len(fetched) / max(1, len(rows)),
    }


def paired(rows_a: list[dict], rows_b: list[dict]) -> dict:
    a, b = by_id(rows_a), by_id(rows_b)
    ids = sorted(set(a) & set(b))
    gold = [a[i]["gold"] for i in ids]
    pa, pb = [pred_or_none(a[i]) for i in ids], [pred_or_none(b[i]) for i in ids]
    return {"n_paired": len(ids),
            "mcnemar": m.mcnemar_one_sided([a[i]["correct"] for i in ids], [b[i]["correct"] for i in ids]),
            "accuracy_diff": m.paired_bootstrap(m.accuracy, gold, pa, pb),
            "macro_f1_diff": m.paired_bootstrap(m.macro_f1, gold, pa, pb)}


def leakage_audit(rows: list[dict], blocked_policy=None) -> dict:
    """Preregistered audit: per claim, did the agent's tool results show the benchmark, fact-check titles/URLs,
    or (blocked arm) a blocked domain? Returns rates and the flagged item ids for sensitivity analysis."""
    flagged_bench, flagged_fc, flagged_blocked = set(), set(), set()
    for r in rows:
        ev = (r.get("verdict") or {}).get("evidence", [])
        text = " ".join(" ".join(e.get("titles", [])) + " " + " ".join(e.get("urls", [])) + " " + e.get("output_head", "")
                        for e in ev).lower()
        if "averitec" in text or "checkthat" in text:
            flagged_bench.add(r["item_id"])
        titles = " ".join(" ".join(e.get("titles", [])) for e in ev).lower()
        urls = " ".join(" ".join(e.get("urls", [])) for e in ev).lower()
        if any(t in titles or t in urls for t in AUDIT_TERMS):
            flagged_fc.add(r["item_id"])
        if blocked_policy is not None and any(blocked_policy.check("https://" + u) for e in ev for u in e.get("urls", [])):
            flagged_blocked.add(r["item_id"])
    n = max(1, len(rows))
    return {"n": len(rows), "benchmark_seen_rate": len(flagged_bench) / n, "factcheck_seen_rate": len(flagged_fc) / n,
            "blocked_domain_seen_rate": len(flagged_blocked) / n,
            "flagged_ids": sorted(flagged_bench | flagged_fc | flagged_blocked)}


def h4_analysis(rows: list[dict], seed: int = 0) -> dict:
    gold = np.array([r["gold"] for r in rows], bool)
    pred = np.array([r["pred"] for r in rows], bool)
    texts = [r.get("text", "") for r in rows]
    rng = np.random.default_rng(seed)
    p = gold.mean()
    rand_f1 = [m.binary_f1(gold, rng.random(len(gold)) < p) for _ in range(1000)]
    heur = np.array([heuristic_checkworthy(t) for t in texts], bool)
    out = {"n": len(rows), "positive_rate": float(p), "f1": m.binary_f1(gold, pred),
           "precision": float((gold & pred).sum() / max(1, pred.sum())), "recall": float((gold & pred).sum() / max(1, gold.sum())),
           "baseline_all_positive_f1": m.binary_f1(gold, np.ones_like(gold)),
           "baseline_random_f1_mean": float(np.mean(rand_f1)), "baseline_random_f1_ci95": [float(np.percentile(rand_f1, 2.5)), float(np.percentile(rand_f1, 97.5))],
           "baseline_heuristic_f1": m.binary_f1(gold, heur)}
    best_name, best_pred = max((("all_positive", np.ones_like(gold)), ("heuristic", heur)), key=lambda kv: m.binary_f1(gold, kv[1]))
    out["best_baseline"] = best_name
    out["permutation_p_vs_best"] = permutation_f1(gold, pred, best_pred)
    out["f1_ci95"] = m.bootstrap_ci(lambda g, q: m.binary_f1(g, q), gold, pred)
    return out


NUMBER_WORDS = re.compile(r"\d|percent|million|billion|trillion|thousand|hundred|twice|half|double|triple|first|most|least|"
                          r"largest|highest|lowest|record|increase|decrease|cut|raise|grew|fell|rate", re.IGNORECASE)


def heuristic_checkworthy(text: str) -> bool:
    """Preregistered heuristic baseline: a sentence is check-worthy if it mentions a number or a quantity/comparison word."""
    return bool(NUMBER_WORDS.search(text or ""))


def permutation_f1(gold, pred_a, pred_b, n: int = 10_000, seed: int = 0) -> float:
    """One-sided approximate randomisation test: P(F1(A) - F1(B) >= observed under swapping)."""
    rng = np.random.default_rng(seed)
    obs = m.binary_f1(gold, pred_a) - m.binary_f1(gold, pred_b)
    hits = 0
    for _ in range(n):
        swap = rng.random(len(gold)) < 0.5
        a = np.where(swap, pred_b, pred_a)
        b = np.where(swap, pred_a, pred_b)
        hits += (m.binary_f1(gold, a) - m.binary_f1(gold, b)) >= obs
    return (hits + 1) / (n + 1)


# ---------------------------------------------------------------------------------------------- figures
def _style(ax):
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def fig_accuracy(arms: dict[str, dict], name: str, title: str, baselines: dict[str, float] | None = None) -> str:
    fig, ax = plt.subplots(figsize=(6.4, 3.4), dpi=150)
    names = list(arms)
    for i, k in enumerate(names):
        a = arms[k]
        lo, hi = a["accuracy_ci95"]
        ax.bar(i, a["accuracy"], width=0.6, color=SERIES[i % 3], edgecolor="white", linewidth=2)
        ax.errorbar(i, a["accuracy"], yerr=[[a["accuracy"] - lo], [hi - a["accuracy"]]], color=TEXT, capsize=4, linewidth=1)
        ax.text(i, hi + 0.02, f"{a['accuracy']:.0%}", ha="center", color=TEXT, fontsize=9)
    for label, val in (baselines or {}).items():
        ax.axhline(val, color=MUTED, linestyle="--", linewidth=1)
        ax.text(len(names) - 0.5, val + 0.01, label, ha="right", va="bottom", color=MUTED, fontsize=8)
    ax.set_xticks(range(len(names)), [ARM_NAMES.get(k, k) for k in names], fontsize=8.5, color=TEXT)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("Accuracy (95 % bootstrap CI)", color=MUTED, fontsize=9)
    ax.set_title(title, color=TEXT, fontsize=10, loc="left")
    _style(ax)
    fig.tight_layout()
    path = FIG / f"{name}.png"
    fig.savefig(path)
    plt.close(fig)
    return path.name


def fig_confusions(arms: dict[str, dict], name: str) -> str:
    fig, axes = plt.subplots(1, len(arms), figsize=(3.3 * len(arms), 3.2), dpi=150)
    axes = np.atleast_1d(axes)
    for ax, (k, a) in zip(axes, arms.items()):
        cm = np.array(a["confusion"], float)
        norm = cm / np.maximum(1, cm.sum(axis=1, keepdims=True))
        ax.imshow(norm, cmap="Blues", vmin=0, vmax=1)
        for i in range(len(LABELS)):
            for j in range(len(LABELS)):
                ax.text(j, i, int(cm[i, j]), ha="center", va="center", fontsize=8, color="white" if norm[i, j] > 0.55 else TEXT)
        ax.set_xticks(range(len(LABELS)), [SHORT[x] for x in LABELS], fontsize=8)
        ax.set_yticks(range(len(LABELS)), [SHORT[x] for x in LABELS], fontsize=8)
        ax.set_xlabel("Predicted", fontsize=8, color=MUTED)
        ax.set_title(ARM_NAMES.get(k, k), fontsize=9, color=TEXT)
    axes[0].set_ylabel("Gold", fontsize=8, color=MUTED)
    fig.tight_layout()
    path = FIG / f"{name}.png"
    fig.savefig(path)
    plt.close(fig)
    return path.name


def fig_reliability(arms: dict[str, dict], name: str) -> str:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(8, 3.4), dpi=150)
    ax1.plot([0, 1], [0, 1], color=MUTED, linestyle="--", linewidth=1)
    for i, (k, a) in enumerate(arms.items()):
        rel = a["reliability"]
        ax1.plot([b["mean_conf"] for b in rel], [b["accuracy"] for b in rel], marker="o", markersize=5, linewidth=2,
                 color=SERIES[i % 3], label=f"{ARM_NAMES.get(k, k)} (ECE {a['ece']:.2f})")
        sel = a["selective"]
        ax2.plot([s["coverage"] for s in sel], [s["accuracy"] for s in sel], marker="o", markersize=5, linewidth=2, color=SERIES[i % 3])
    ax1.set_xlabel("Stated confidence (bin mean)", fontsize=9, color=MUTED)
    ax1.set_ylabel("Observed accuracy", fontsize=9, color=MUTED)
    ax1.set_title("Reliability (10 equal-mass bins)", fontsize=10, color=TEXT, loc="left")
    ax1.legend(fontsize=7.5, frameon=False, loc="lower right")
    ax2.set_xlabel("Coverage (most confident fraction shown)", fontsize=9, color=MUTED)
    ax2.set_ylabel("Accuracy", fontsize=9, color=MUTED)
    ax2.set_title("Selective accuracy", fontsize=10, color=TEXT, loc="left")
    ax2.invert_xaxis()
    for ax in (ax1, ax2):
        _style(ax)
        ax.set_ylim(0, 1.02)
    fig.tight_layout()
    path = FIG / f"{name}.png"
    fig.savefig(path)
    plt.close(fig)
    return path.name


def fig_h4(h4: dict, name: str) -> str:
    items = [("Product extractor", h4["f1"], h4["f1_ci95"]), ("Heuristic", h4["baseline_heuristic_f1"], None),
             ("All positive", h4["baseline_all_positive_f1"], None), ("Random", h4["baseline_random_f1_mean"], h4["baseline_random_f1_ci95"])]
    fig, ax = plt.subplots(figsize=(6.0, 3.0), dpi=150)
    for i, (label, val, ci) in enumerate(items):
        ax.bar(i, val, width=0.6, color=SERIES[0] if i == 0 else "#b8b7b0", edgecolor="white", linewidth=2)
        if ci:
            ax.errorbar(i, val, yerr=[[val - ci[0]], [ci[1] - val]], color=TEXT, capsize=4, linewidth=1)
        ax.text(i, (ci[1] if ci else val) + 0.02, f"{val:.2f}", ha="center", fontsize=9, color=TEXT)
    ax.set_xticks(range(len(items)), [x[0] for x in items], fontsize=8.5)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("F1 (check-worthy class)", fontsize=9, color=MUTED)
    ax.set_title(f"CheckThat! 2024 Task 1 English test (n = {h4['n']})", fontsize=10, color=TEXT, loc="left")
    _style(ax)
    fig.tight_layout()
    path = FIG / f"{name}.png"
    fig.savefig(path)
    plt.close(fig)
    return path.name


# ---------------------------------------------------------------------------------------------- tables
def pct(x, digits=1):
    return "–" if x is None else f"{100 * x:.{digits}f} %"


def ci(c):
    return f"[{100 * c[0]:.1f}, {100 * c[1]:.1f}]"


def table_arms(arms: dict[str, dict]) -> str:
    head = "| Condition | n | Accuracy [95 % CI] | Macro-F1 [95 % CI] | 3-class acc. | Brier | ECE | $/claim | p50 latency |\n|---|---|---|---|---|---|---|---|---|\n"
    rows = [f"| {ARM_NAMES.get(k, k)} | {a['n']} | {pct(a['accuracy'])} {ci(a['accuracy_ci95'])} | {pct(a['macro_f1'])} "
            f"{ci(a['macro_f1_ci95'])} | {pct(a['accuracy_3class'])} | {a['brier']:.3f} | {a['ece']:.3f} | "
            f"${a['cost_per_claim_usd']:.4f} | {a['latency_p50_s']:.0f} s |" for k, a in arms.items()]
    return head + "\n".join(rows) + "\n"


def table_per_class(arms: dict[str, dict]) -> str:
    head = "| Condition | " + " | ".join(f"F1 {SHORT[lab]} (n={next(iter(arms.values()))['per_class'][lab]['support']})" for lab in LABELS) + " |\n"
    head += "|---|" + "---|" * len(LABELS) + "\n"
    return head + "\n".join(f"| {ARM_NAMES.get(k, k)} | " + " | ".join(f"{a['per_class'][lab]['f1']:.2f}" for lab in LABELS) + " |"
                            for k, a in arms.items()) + "\n"


# ---------------------------------------------------------------------------------------------- template filling
def fill(path: Path, blocks: dict[str, str]) -> None:
    """Replace <!-- GEN:key --> ... <!-- /GEN:key --> regions in a markdown file."""
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    for key, value in blocks.items():
        body = value.strip()
        text = re.sub(rf"(<!-- GEN:{key} -->)(.*?)(<!-- /GEN:{key} -->)",
                      lambda mt, body=body: f"{mt.group(1)}\n{body}\n{mt.group(3)}", text, flags=re.DOTALL)
    path.write_text(text, encoding="utf-8")


def main() -> None:
    FIG.mkdir(parents=True, exist_ok=True)
    GEN.mkdir(parents=True, exist_ok=True)
    summary: dict = {}
    blocks: dict[str, str] = {}
    for exp in ("pilot", "confirmatory"):
        arms = {c: arm_summary(load(exp, c)) for c in ("closed_book", "web_blocked", "web_default") if load(exp, c)}
        if not arms:
            continue
        rows = {c: load(exp, c) for c in arms}
        s = {"arms": arms}
        if {"web_blocked", "closed_book"} <= set(arms):
            s["H1"] = paired(rows["web_blocked"], rows["closed_book"])
        if {"web_default", "web_blocked"} <= set(arms):
            s["H2"] = paired(rows["web_default"], rows["web_blocked"])
        if exp == "confirmatory":
            from eval.conditions import factcheck_blocked_policy
            s["audit"] = {c: leakage_audit(rows[c], factcheck_blocked_policy() if c == "web_blocked" else None)
                          for c in ("web_blocked", "web_default") if c in rows}
            flagged = set().union(*(a["flagged_ids"] for a in s["audit"].values())) if s.get("audit") else set()
            clean = {c: [r for r in rows[c] if r["item_id"] not in flagged] for c in rows}
            s["sensitivity_excluding_flagged"] = {"n_excluded": len(flagged)}
            if {"web_blocked", "closed_book"} <= set(clean) and clean["web_blocked"]:
                s["sensitivity_excluding_flagged"]["H1"] = paired(clean["web_blocked"], clean["closed_book"])
            if {"web_default", "web_blocked"} <= set(clean) and clean["web_default"]:
                s["sensitivity_excluding_flagged"]["H2"] = paired(clean["web_default"], clean["web_blocked"])
        dev_counts = {lab: sum(1 for r in next(iter(rows.values())) if r["gold"] == lab) for lab in LABELS}
        majority = max(dev_counts.values()) / max(1, sum(dev_counts.values()))
        s["baseline_majority_accuracy"] = majority
        summary[exp] = s
        fig_accuracy(arms, f"{exp}_accuracy", f"AVeriTeC {'train pilot' if exp == 'pilot' else 'dev sample'}: label accuracy",
                     {"majority class": majority})
        fig_confusions(arms, f"{exp}_confusion")
        fig_reliability(arms, f"{exp}_calibration")
        (GEN / f"{exp}_arms.md").write_text(table_arms(arms), encoding="utf-8")
        (GEN / f"{exp}_per_class.md").write_text(table_per_class(arms), encoding="utf-8")
        blocks[f"{exp}_arms"] = table_arms(arms)
        blocks[f"{exp}_per_class"] = table_per_class(arms)
    h3 = {c: load("h3", c) for c in ("h3_clean", "h3_ocr")}
    if all(h3.values()):
        s3 = {"arms": {c: {"n": len(r), "accuracy": m.accuracy([x["gold"] for x in r], [pred_or_none(x) for x in r]),
                           "extraction_failure_rate": float(np.mean([x["extraction_failed"] for x in r])),
                           "cer_mean": float(np.mean([x["cer"] for x in r])), "wer_mean": float(np.mean([x["wer"] for x in r]))}
                       for c, r in h3.items()}}
        s3["diff"] = paired(h3["h3_ocr"], h3["h3_clean"])
        ocr = h3["h3_ocr"]
        s3["by_cell"] = {f"{lay}-{px}px": {"n": len(g), "accuracy": float(np.mean([x["correct"] for x in g])),
                                           "cer_mean": float(np.mean([x["cer"] for x in g]))}
                         for lay in ("chat", "article") for px in (13, 15, 18)
                         if (g := [x for x in ocr if x["layout"] == lay and x["font_px"] == px])}
        summary["h3"] = s3
    h4_rows = load("h4-test", "extractor")
    if h4_rows:
        from eval.datasets import load_checkthat
        texts = {s.sentence_id: s.text for s in load_checkthat("test")}
        for r in h4_rows:
            r["text"] = texts.get(r["item_id"], "")
        summary["h4"] = h4_analysis(h4_rows)
        fig_h4(summary["h4"], "h4_checkworthiness")
    rep1 = by_id(load("confirmatory", "closed_book"))
    e2 = {rep: by_id(load("e2", "closed_book", repeat=rep)) for rep in (2, 3)}
    if rep1 and all(e2.values()):
        ids = sorted(set(rep1) & set(e2[2]) & set(e2[3]))
        mat = [[pred_or_none(rep1[i]), pred_or_none(e2[2][i]), pred_or_none(e2[3][i])] for i in ids]
        accs = [float(np.mean([d[i]["correct"] for i in ids])) for d in (rep1, e2[2], e2[3])]
        summary["e2"] = {"n": len(ids), "fleiss_kappa": m.fleiss(mat), "accuracy_by_repeat": accs, "accuracy_sd": float(np.std(accs, ddof=1)),
                         "all_three_agree_rate": float(np.mean([len(set(row)) == 1 for row in mat]))}
    pvals = {}
    if "confirmatory" in summary:
        for h in ("H1", "H2"):
            if h in summary["confirmatory"]:
                pvals[h] = summary["confirmatory"][h]["mcnemar"]["p_one_sided"]
    if "h4" in summary:
        pvals["H4"] = summary["h4"]["permutation_p_vs_best"]
    if pvals:
        summary["holm"] = m.holm(pvals)
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    for target in [ROOT / "README.md", *sorted((ROOT / "reports").glob("*.md")), ROOT / "reports" / "paper" / "paper.md"]:
        fill(target, blocks)
    print(json.dumps({k: (v.get("H1", {}).get("accuracy_diff") if isinstance(v, dict) else v) for k, v in summary.items()}, indent=1)[:1500])


if __name__ == "__main__":
    main()
