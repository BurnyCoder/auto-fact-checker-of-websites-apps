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
from factcheck.config import Settings
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


URL_RE = re.compile(r"https?://[^\s\"'<>\]\)\}]+")


def leakage_audit(rows: list[dict], blocked_policy=None) -> dict:
    """Preregistered audit: per claim, did the agent's tool *results* show the benchmark name, a fact-check title/URL,
    or (blocked arm) a blocked domain?

    Only result content counts (titles + URLs found in each tool's output head), not tool *inputs*: a WebFetch the
    hook refused, or a URL the agent merely tried, is not something it saw. (A first implementation also counted
    input URLs; fixed to match the preregistered definition before the analysis was finalised.) Output heads are the
    first 1,500 characters of each tool result, so links beyond that are not audited (a conservative undercount).
    Blocked hits are split into whole-domain hits and fact-check URL-path hits (WebSearch can only filter domains)."""
    flagged_bench, flagged_fc, flagged_domain, flagged_path = set(), set(), set(), set()
    domain_only = None
    if blocked_policy is not None:
        from factcheck.pipeline.policy import UrlPolicy
        domain_only = UrlPolicy(blocked_domains=blocked_policy.blocked_domains)
    for r in rows:
        ev = (r.get("verdict") or {}).get("evidence", [])
        heads = " ".join(e.get("output_head", "") for e in ev)
        titles = " ".join(" ".join(e.get("titles", [])) for e in ev).lower()
        result_urls = URL_RE.findall(heads)
        if "averitec" in heads.lower() or "checkthat" in heads.lower():
            flagged_bench.add(r["item_id"])
        if any(t in titles or t in u.lower() for t in AUDIT_TERMS for u in result_urls) or any(t in titles for t in AUDIT_TERMS):
            flagged_fc.add(r["item_id"])
        if blocked_policy is not None:
            if any(domain_only.check(u) for u in result_urls):
                flagged_domain.add(r["item_id"])
            elif any(blocked_policy.check(u) for u in result_urls):
                flagged_path.add(r["item_id"])
    n = max(1, len(rows))
    return {"n": len(rows), "benchmark_seen_rate": len(flagged_bench) / n, "factcheck_seen_rate": len(flagged_fc) / n,
            "blocked_domain_seen_rate": len(flagged_domain) / n, "blocked_path_only_seen_rate": len(flagged_path) / n,
            "flagged_ids": sorted(flagged_bench | flagged_fc | flagged_domain | flagged_path)}


def h4_analysis(rows: list[dict], seed: int = 0) -> dict:
    gold = np.array([r["gold"] for r in rows], bool)
    pred = np.array([r["pred"] for r in rows], bool)
    texts = [r.get("text", "") for r in rows]
    rng = np.random.default_rng(seed)
    p = gold.mean()
    rand_f1 = list(m._binary_f1_rows(np.broadcast_to(gold, (1000, len(gold))), rng.random((1000, len(gold))) < p))
    heur = np.array([heuristic_checkworthy(t) for t in texts], bool)
    out = {"n": len(rows), "positive_rate": float(p), "f1": m.binary_f1(gold, pred),
           "precision": float((gold & pred).sum() / max(1, pred.sum())), "recall": float((gold & pred).sum() / max(1, gold.sum())),
           "baseline_all_positive_f1": m.binary_f1(gold, np.ones_like(gold)),
           "baseline_random_f1_mean": float(np.mean(rand_f1)), "baseline_random_f1_ci95": [float(np.percentile(rand_f1, 2.5)), float(np.percentile(rand_f1, 97.5))],
           "baseline_heuristic_f1": m.binary_f1(gold, heur)}
    best_name, best_pred = max((("all_positive", np.ones_like(gold)), ("heuristic", heur)), key=lambda kv: m.binary_f1(gold, kv[1]))
    out["best_baseline"] = best_name
    out["permutation_p_vs_best"] = m.permutation_f1(gold, pred, best_pred)
    out["f1_ci95"] = m.bootstrap_ci(m.binary_f1, gold, pred)
    return out


NUMBER_WORDS = re.compile(r"\d|percent|million|billion|trillion|thousand|hundred|twice|half|double|triple|first|most|least|"
                          r"largest|highest|lowest|record|increase|decrease|cut|raise|grew|fell|rate", re.IGNORECASE)


def heuristic_checkworthy(text: str) -> bool:
    """Preregistered heuristic baseline: a sentence is check-worthy if it mentions a number or a quantity/comparison word."""
    return bool(NUMBER_WORDS.search(text or ""))


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


def fig_architecture(name: str = "architecture") -> str:
    """Static architecture diagram for the paper (the README uses a mermaid version of the same graph)."""
    from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

    fig, ax = plt.subplots(figsize=(10, 4.6), dpi=150)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 46)
    ax.axis("off")
    boxes = {
        "ext": (2, 32, 18, 9, "Chrome extension\ncontent script → service worker"),
        "ocr": (2, 6, 18, 11, "Desktop capture\nscreenshot → Windows OCR\n→ line grouping"),
        "api": (25, 32, 15, 9, "Localhost server\nWebSocket + token"),
        "svc": (44, 19, 15, 10, "FactCheckService\ndedup · cache · queue"),
        "exr": (63, 33, 16, 8, "Extract claims\nHaiku, no tools"),
        "ver": (63, 15, 16, 12, "Verify claim\nHaiku agent\n(schema-enforced verdict)"),
        "tools": (83, 15, 15, 21, "Tools\nWebSearch · WebFetch\nfetch_file → /work\nbash (bubblewrap,\nWSL2, offline)"),
        "chk": (63, 1, 16, 9, "Rules · repair pass\nURL-in-trace · quote check"),
        "ui": (25, 1, 15, 9, "Overlay · panel ·\nhover card"),
    }
    for key, (x, y, w, h, label) in boxes.items():
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.4,rounding_size=1.2",
                                    facecolor="#eef4fc" if key in ("ver", "exr") else "#f2f1ee", edgecolor="#b8b7b0", linewidth=1))
        ax.text(x + w / 2, y + h / 2, label, ha="center", va="center", fontsize=7.6, color=TEXT)

    def arrow(a, b, label=""):
        (x1, y1), (x2, y2) = a, b
        ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=9, color=MUTED, linewidth=1))
        if label:
            ax.text((x1 + x2) / 2, (y1 + y2) / 2 + 1, label, fontsize=6.5, color=MUTED, ha="center")

    arrow((20.6, 36.5), (24.6, 36.5), "Port")
    arrow((40.6, 35), (44, 27.5))
    arrow((20.6, 11.5), (44, 21), "text blocks")
    arrow((59.6, 27), (63, 35), "new blocks")
    arrow((59.6, 23), (63, 21), "claims")
    arrow((79.6, 21), (82.6, 23))
    arrow((71, 14.6), (71, 10.4))
    arrow((62.6, 5.5), (40.6, 5.5), "verdict events")
    arrow((40.6, 7.5), (40.6, 32), "")
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


# ---------------------------------------------------------------------------------------------- narrative blocks
def fmt_p(p: float | None) -> str:
    return "–" if p is None else ("< 0.001" if p < 0.001 else f"{p:.3f}")


def p_text(p: float | None) -> str:
    """'p < 0.001' or 'p = 0.412' style fragment."""
    f = fmt_p(p)
    return f"p {f}" if f.startswith("<") else f"p = {f}"


def pts(x: float) -> str:
    return f"{100 * x:+.1f}"


def decision_h(p_holm: float | None, diff: float) -> str:
    if p_holm is None:
        return "not run"
    return "**supported**" if p_holm < 0.05 and diff > 0 else "not supported (inconclusive at this sample size)"


def blocks_confirmatory(s: dict) -> dict[str, str]:
    out = {}
    holm = s.get("holm_ref", {})
    rows = ["| Hypothesis | Comparison | n | Accuracy diff (pts) [95 % CI] | Macro-F1 diff (pts) [95 % CI] | A-only / B-only | p (one-sided) | p (Holm) | Decision |",
            "|---|---|---|---|---|---|---|---|---|"]
    for h, label in (("H1", "web, fact-checks blocked − closed-book"), ("H2", "web default − web, fact-checks blocked")):
        if h not in s:
            continue
        t = s[h]
        ad, fd, mc = t["accuracy_diff"], t["macro_f1_diff"], t["mcnemar"]
        ph = holm.get(h, {}).get("p_holm")
        rows.append(f"| {h} | {label} | {t['n_paired']} | {pts(ad['diff'])} [{pts(ad['ci95'][0])}, {pts(ad['ci95'][1])}] | "
                    f"{pts(fd['diff'])} [{pts(fd['ci95'][0])}, {pts(fd['ci95'][1])}] | {mc['b_a_only']} / {mc['c_b_only']} | "
                    f"{fmt_p(mc['p_one_sided'])} | {fmt_p(ph)} | {decision_h(ph, ad['diff'])} |")
    out["h12_tests"] = "\n".join(rows) + "\n"
    sens = s.get("sensitivity_excluding_flagged", {})
    srows = [(f"Claims flagged by the leakage audit in either web arm: **{sens.get('n_excluded', 0)}**. Re-running the "
              "paired tests without them:"), "","| Hypothesis | n | Accuracy diff (pts) [95 % CI] | p (one-sided, uncorrected) |", "|---|---|---|---|"]
    for h in ("H1", "H2"):
        if h in sens:
            t = sens[h]
            srows.append(f"| {h} | {t['n_paired']} | {pts(t['accuracy_diff']['diff'])} [{pts(t['accuracy_diff']['ci95'][0])}, "
                         f"{pts(t['accuracy_diff']['ci95'][1])}] | {fmt_p(t['mcnemar']['p_one_sided'])} |")
    ex = s.get("exploratory_h1_blocked_arm_flags_only")
    if ex:
        srows += ["", (f"*Exploratory (not preregistered):* H1 excluding only the {ex['n_excluded']} claims flagged in the blocked arm: "
                       f"n = {ex['n_paired']}, {pts(ex['accuracy_diff']['diff'])} pts [{pts(ex['accuracy_diff']['ci95'][0])}, "
                       f"{pts(ex['accuracy_diff']['ci95'][1])}], one-sided p = {fmt_p(ex['mcnemar']['p_one_sided'])}. The excluded "
                       "claims are not a random subset (claims with published fact-checks are the better-known ones), so "
                       "neither restricted estimate is an unbiased estimate for new claims.")]
    out["sensitivity"] = "\n".join(srows) + "\n"
    arows = ["| Web arm | n | benchmark name seen | fact-check title/URL seen | blocked domain seen |", "|---|---|---|---|---|"]
    for arm, a in (s.get("audit") or {}).items():
        arows.append(f"| {ARM_NAMES[arm]} | {a['n']} | {pct(a['benchmark_seen_rate'])} | {pct(a['factcheck_seen_rate'])} | "
                     f"{pct(a['blocked_domain_seen_rate']) if arm == 'web_blocked' else '(not blocked)'} |")
    out["audit"] = "\n".join(arows) + "\n"
    crows = ["| Condition | Mean stated confidence | Accuracy | Brier | ECE (10 equal-mass bins) | Accuracy of the 50 % most confident |",
             "|---|---|---|---|---|---|"]
    for k, a in s["arms"].items():
        half = next((x["accuracy"] for x in a["selective"] if x["coverage"] == 0.5), None)
        crows.append(f"| {ARM_NAMES[k]} | {pct(a['mean_confidence'])} | {pct(a['accuracy'])} | {a['brier']:.3f} | {a['ece']:.3f} | {pct(half)} |")
    out["calibration"] = "\n".join(crows) + "\n"
    xrows = [("| Condition | 3-class acc. | Supported-vs-Refuted acc. (n) | Predicted S / R / NEE / C | Searches/claim | Sources/claim | "
              "Source URL in agent's results | Quotes checkable | Quotes verified (of checkable) | Sandbox used | p95 latency | Failed checks |"),
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, a in s["arms"].items():
        d = a["pred_distribution"]
        xrows.append(f"| {ARM_NAMES[k]} | {pct(a['accuracy_3class'])} | {pct(a['accuracy_supported_vs_refuted_subset'])} "
                     f"({a['n_supported_vs_refuted_subset']}) | {d['Supported']} / {d['Refuted']} / {d['Not Enough Evidence']} / "
                     f"{d['Conflicting Evidence/Cherrypicking']} | {a['searches_per_claim']:.2f} | {a['sources_per_claim']:.2f} | "
                     f"{pct(a['source_in_trace_rate'])} | {pct(a['quote_checked_rate'])} | {pct(a['quote_verified_rate_of_checked'])} | "
                     f"{pct(a['sandbox_use_rate'])} | {a['latency_p95_s']:.0f} s | {a['errors']} |")
    out["secondary"] = "\n".join(xrows) + "\n"
    return out


def blocks_h3(s3: dict) -> dict[str, str]:
    d = s3["diff"]["accuracy_diff"]
    lb = d["lower_one_sided_95"]
    rows = ["| Arm | n | Accuracy | Extraction failed | Mean CER | Mean WER |", "|---|---|---|---|---|---|"]
    for k, a in s3["arms"].items():
        rows.append(f"| {ARM_NAMES[k]} | {a['n']} | {pct(a['accuracy'])} | {pct(a['extraction_failure_rate'])} | "
                    f"{pct(a['cer_mean'], 2)} | {pct(a['wer_mean'], 2)} |")
    rows += ["", (f"Difference OCR − clean: **{pts(d['diff'])} pts** (95 % CI [{pts(d['ci95'][0])}, {pts(d['ci95'][1])}]); "
                  f"one-sided 95 % lower bound **{pts(lb)} pts** vs the preregistered margin −3.0 → "
                  f"{'**non-inferior** (H3 supported)' if lb > -0.03 else '**non-inferiority not shown** (H3 not supported)'}. "
                  f"Discordant pairs: OCR-only right {s3['diff']['mcnemar']['b_a_only']}, clean-only right {s3['diff']['mcnemar']['c_b_only']}."),
             "", "| OCR cell | n | Accuracy | Mean CER |", "|---|---|---|---|"]
    rows += [f"| {cell} | {v['n']} | {pct(v['accuracy'])} | {pct(v['cer_mean'], 2)} |" for cell, v in s3["by_cell"].items()]
    return {"h3_table": "\n".join(rows) + "\n"}


def blocks_h4(h4: dict, holm: dict) -> dict[str, str]:
    ph = holm.get("H4", {}).get("p_holm")
    best = max(h4["baseline_all_positive_f1"], h4["baseline_heuristic_f1"])
    rows = ["| System | F1 (check-worthy) | Precision | Recall |", "|---|---|---|---|",
            f"| Product extractor (Haiku, zero-shot) | {h4['f1']:.3f} [{h4['f1_ci95'][0]:.3f}, {h4['f1_ci95'][1]:.3f}] | {h4['precision']:.3f} | {h4['recall']:.3f} |",
            f"| Number/quantity-word heuristic | {h4['baseline_heuristic_f1']:.3f} | – | – |",
            f"| All positive | {h4['baseline_all_positive_f1']:.3f} | {h4['positive_rate']:.3f} | 1.000 |",
            f"| Stratified random (1,000 draws) | {h4['baseline_random_f1_mean']:.3f} [{h4['baseline_random_f1_ci95'][0]:.3f}, {h4['baseline_random_f1_ci95'][1]:.3f}] | – | – |",
            "", (f"Permutation test vs the best baseline ({h4['best_baseline']}, F1 {best:.3f}): {p_text(h4['permutation_p_vs_best'])}, "
                 f"Holm-adjusted {p_text(ph)} → {decision_h(ph, h4['f1'] - best)}. For reference, published "
                 "fine-tuned systems reached F1 ≈ 0.75–0.80 on this test set (e.g. RoBERTa 0.753, [IAI 2024](https://arxiv.org/abs/2408.01118)).")]
    return {"h4_table": "\n".join(rows) + "\n"}


def blocks_e2(e2: dict) -> dict[str, str]:
    accs = ", ".join(pct(a) for a in e2["accuracy_by_repeat"])
    return {"e2_table": f"Closed-book verifier run 3 times on the first {e2['n']} sampled claims: Fleiss' κ = **{e2['fleiss_kappa']:.3f}**, "
                        f"identical label in all 3 runs for {pct(e2['all_three_agree_rate'])} of claims, accuracy per run {accs} "
                        f"(SD {100 * e2['accuracy_sd']:.1f} pts).\n"}


def blocks_e3(recs: list[dict]) -> dict[str, str]:
    from collections import Counter

    cnt = Counter(r.get("judgement") for r in recs)
    n = max(1, len(recs))
    rows = ["| Judge verdict | Count | Share |", "|---|---|---|"]
    rows += [f"| {k} | {v} | {pct(v / n)} |" for k, v in cnt.most_common()]
    verified = [q for r in recs for q in r.get("correction_sources_quote_verified", []) if q is not None]
    rows += ["", (f"{len(recs)} corrections judged (web default arm, predicted Refuted or Conflicting). Correction sources whose quote "
                  f"could be checked on the live page: {len(verified)}, of which verified {pct(sum(verified) / max(1, len(verified)))}.")]
    return {"e3_table": "\n".join(rows) + "\n"}


def blocks_budget() -> dict[str, str]:
    from collections import defaultdict

    path = ROOT / "budget_ledger.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    groups: dict[str, float] = defaultdict(float)
    for r in rows:
        p = r["purpose"]
        key = ("pilot" if p.startswith("pilot") else "evaluation" if p.startswith("eval") else
               "app & demos" if p.startswith("app") else "development & tests")
        groups[key] += r["cost_usd"]
    total = sum(groups.values())
    auth = defaultdict(float)
    for r in rows:
        auth[r["auth"]] += r["cost_usd"]
    lines = ["| Phase | API-equivalent USD |", "|---|---|"] + [f"| {k} | ${v:.2f} |" for k, v in sorted(groups.items())]
    api_part = f", via API key for ${auth['api_key']:.2f}" if auth.get("api_key") else ""
    cap = Settings.model_fields["total_budget_usd"].default        # the code default, not a local .env override
    lines += [f"| **total** | **${total:.2f}** of ${cap:.2f} (the evaluation ran under the earlier $10.00 cap) |", "",
              f"{len(rows)} model calls; billed via subscription for ${auth.get('subscription', 0):.2f}{api_part}."]
    return {"budget": "\n".join(lines) + "\n"}


def blocks_eval_table(summary: dict) -> dict[str, str]:
    """The one table CLAUDE.md asks for: method vs baselines, controls and ablations, with their role."""
    rows = ["| Role | System | Benchmark | Metric | Score |", "|---|---|---|---|---|"]
    c = summary.get("confirmatory", {})
    if c:
        rows.append(f"| Baseline | Always 'Refuted' (majority class) | AVeriTeC dev sample (n = {c['arms']['closed_book']['n']}) | accuracy | {pct(c['baseline_majority_accuracy'])} |")
        rows.append("| Baseline | Uniform random label | AVeriTeC dev sample | accuracy | 25.0 % (expected) |")
        for k, role in (("closed_book", "Control (no retrieval)"), ("web_blocked", "Ablation (no fact-check sites)"),
                        ("web_default", "**Method (product)**")):
            a = c["arms"].get(k)
            if a:
                rows.append(f"| {role} | {ARM_NAMES[k]} | AVeriTeC dev sample | accuracy / macro-F1 | {pct(a['accuracy'])} / {pct(a['macro_f1'])} |")
    if "h3" in summary:
        for k, role in (("h3_clean", "Control (ideal capture)"), ("h3_ocr", "**Method (screen path)**")):
            a = summary["h3"]["arms"][k]
            rows.append(f"| {role} | {ARM_NAMES[k]} → extract → closed-book verify | AVeriTeC dev (n = {a['n']}) | accuracy | {pct(a['accuracy'])} |")
    if "h4" in summary:
        h = summary["h4"]
        rows += [f"| **Method** | Product extractor | CheckThat! 2024 T1 EN test (n = {h['n']}) | F1 | {h['f1']:.3f} |",
                 f"| Baseline | Number/quantity heuristic | CheckThat! 2024 T1 EN test | F1 | {h['baseline_heuristic_f1']:.3f} |",
                 f"| Baseline | All positive | CheckThat! 2024 T1 EN test | F1 | {h['baseline_all_positive_f1']:.3f} |",
                 f"| Control | Stratified random | CheckThat! 2024 T1 EN test | F1 | {h['baseline_random_f1_mean']:.3f} |"]
    return {"eval_table": "\n".join(rows) + "\n"}


def blocks_readme(summary: dict) -> dict[str, str]:
    """Plain-language headline results (numbers from summary; wording of the decision rule from the preregistration)."""
    lines = []
    c, holm = summary.get("confirmatory", {}), summary.get("holm", {})
    for h, what in (("H1", "Web tools (fact-check sites blocked) vs answering from memory"),
                    ("H2", "Allowing existing fact-check sites vs blocking them")):
        if h in c:
            d = c[h]["accuracy_diff"]
            ph = holm.get(h, {}).get("p_holm")
            lines.append(f"* **{h}. {what}:** {pts(d['diff'])} accuracy points (95 % CI {pts(d['ci95'][0])} to "
                         f"{pts(d['ci95'][1])}; n = {c[h]['n_paired']}; Holm-adjusted {p_text(ph)}) → {decision_h(ph, d['diff'])}.")
    if "h3" in summary:
        d = summary["h3"]["diff"]["accuracy_diff"]
        ok = d["lower_one_sided_95"] > -0.03
        lines.append(f"* **H3. Reading the claim through rendering + Windows OCR vs exact text:** {pts(d['diff'])} points "
                     f"(one-sided 95 % lower bound {pts(d['lower_one_sided_95'])}; margin −3; n = {summary['h3']['diff']['n_paired']}; "
                     f"mean OCR character error rate {pct(summary['h3']['arms']['h3_ocr']['cer_mean'], 2)}) → "
                     f"{'**non-inferior**' if ok else '**non-inferiority not shown**'}.")
    if "h4" in summary:
        h = summary["h4"]
        best = max(h["baseline_all_positive_f1"], h["baseline_heuristic_f1"])
        ph = holm.get("H4", {}).get("p_holm")
        lines.append(f"* **H4. Finding check-worthy sentences (CheckThat! 2024):** F1 {h['f1']:.3f} vs best simple baseline "
                     f"{best:.3f} (Holm-adjusted {p_text(ph)}) → {decision_h(ph, h['f1'] - best)}.")
    if c.get("arms", {}).get("web_default"):
        a = c["arms"]["web_default"]
        lines.append(f"* **Confidence calibration (product setting):** mean stated confidence {pct(a['mean_confidence'])} vs "
                     f"accuracy {pct(a['accuracy'])}; Brier {a['brier']:.3f}, ECE {a['ece']:.3f}.")
    if "e2" in summary:
        lines.append(f"* **Run-to-run variance (E2):** Fleiss' κ = {summary['e2']['fleiss_kappa']:.2f} across 3 runs; same label in all "
                     f"runs for {pct(summary['e2']['all_three_agree_rate'])} of claims.")
    if "e3" in summary and summary["e3"]["n"]:
        j = summary["e3"]["judgements"]
        lines.append(f"* **Corrections (E3):** of {summary['e3']['n']} corrections, the judge found {j['agrees']} consistent with the "
                     f"professional fact-check, {j['partially']} partially, {j['disagrees']} contradicting, and {j['gold_supports_claim']} "
                     "where the professional verdict says the claim was true.")
    return {"readme_results": "\n".join(lines) + "\n"}


def blocks_abstract(summary: dict) -> dict[str, str]:
    c, holm = summary.get("confirmatory", {}), summary.get("holm", {})
    if not c or "h3" not in summary or "h4" not in summary:
        return {}
    a = c["arms"]
    h1, h2 = c["H1"]["accuracy_diff"], c["H2"]["accuracy_diff"]
    h3 = summary["h3"]["diff"]["accuracy_diff"]
    h4 = summary["h4"]
    best = max(h4["baseline_all_positive_f1"], h4["baseline_heuristic_f1"])
    p1, p2 = holm.get("H1", {}).get("p_holm"), holm.get("H2", {}).get("p_holm")
    text = (f"On {a['closed_book']['n']} preregistered AVeriTeC dev claims, accuracy was {pct(a['closed_book']['accuracy'])} without web "
            f"access, {pct(a['web_blocked']['accuracy'])} with web tools but all fact-checking sites blocked, and "
            f"{pct(a['web_default']['accuracy'])} with the product's default web access (majority-class baseline "
            f"{pct(c['baseline_majority_accuracy'])}). The retrieval effect with fact-checks blocked (H1) was {pts(h1['diff'])} points "
            f"(95 % CI {pts(h1['ci95'][0])} to {pts(h1['ci95'][1])}; Holm {p_text(p1)}) and the added effect of reaching existing "
            f"fact-checks (H2) was {pts(h2['diff'])} points (95 % CI {pts(h2['ci95'][0])} to {pts(h2['ci95'][1])}; Holm {p_text(p2)}). "
            f"Reading claims from rendered pages through Windows OCR changed end-to-end accuracy by {pts(h3['diff'])} points on 500 "
            f"claims (one-sided 95 % lower bound {pts(h3['lower_one_sided_95'])}; preregistered non-inferiority margin −3). The "
            f"zero-shot claim extractor reached F1 {h4['f1']:.3f} on CheckThat! 2024 check-worthiness (best simple baseline {best:.3f}).")
    return {"abstract_numbers": text + "\n"}


# ---------------------------------------------------------------------------------------------- site export
def _slim(r: dict) -> dict:
    v = r.get("verdict") or {}
    return {"pred": r.get("pred"), "correct": r.get("correct"), "confidence": r.get("confidence"),
            "reasoning": v.get("confidence_reasoning", ""), "explanation": v.get("explanation", ""),
            "correction": v.get("corrected_claim"), "flags": r.get("flags", []), "cost": r.get("cost_usd"),
            "sources": [{"url": s.get("url"), "title": s.get("title"), "quote": s.get("quote"), "supports": s.get("supports"),
                         "quote_verified": s.get("quote_verified"), "in_trace": s.get("in_trace")} for s in v.get("sources", [])],
            "computations": [c.get("command") for c in v.get("computations", [])][:5]}


def export_site(summary: dict) -> None:
    """Write site/data/{summary,claims,h3}.json for the GitHub Pages explorer (site/index.html)."""
    from eval.datasets import load_averitec

    out = ROOT / "site" / "data"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    claims = {c.item_id: c for c in load_averitec("dev")}
    arms = {c: by_id(load("confirmatory", c)) for c in ("closed_book", "web_blocked", "web_default")}
    ids = sorted(set().union(*arms.values()), key=lambda i: int(i.split("-")[1]))
    rows = [{"id": i, "claim": claims[i].claim, "gold": claims[i].label, "date": claims[i].claim_date,
             "speaker": claims[i].speaker, "arms": {c: _slim(arms[c][i]) for c in arms if i in arms[c]}} for i in ids]
    (out / "claims.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    h3 = {c: by_id(load("h3", c)) for c in ("h3_clean", "h3_ocr")}
    h3_rows = [{"id": i, "claim": claims[i].claim, "gold": claims[i].label, "layout": h3["h3_ocr"][i]["layout"],
                "font_px": h3["h3_ocr"][i]["font_px"], "cer": h3["h3_ocr"][i]["cer"], "ocr_text": h3["h3_ocr"][i]["ocr_text"],
                "clean_pred": h3["h3_clean"][i].get("pred"), "ocr_pred": h3["h3_ocr"][i].get("pred")}
               for i in sorted(set(h3["h3_clean"]) & set(h3["h3_ocr"]), key=lambda i: int(i.split("-")[1]))]
    (out / "h3.json").write_text(json.dumps(h3_rows, ensure_ascii=False), encoding="utf-8")


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
    path.write_text(text, encoding="utf-8", newline="\n")   # LF: avoid CRLF churn on Windows


def main() -> None:
    FIG.mkdir(parents=True, exist_ok=True)
    fig_architecture()
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
            # EXPLORATORY (not preregistered): for H1 only the blocked arm's flags are relevant
            if "web_blocked" in s.get("audit", {}):
                fb = set(s["audit"]["web_blocked"]["flagged_ids"])
                cb = {c: [r for r in rows[c] if r["item_id"] not in fb] for c in ("web_blocked", "closed_book")}
                s["exploratory_h1_blocked_arm_flags_only"] = {"n_excluded": len(fb), **paired(cb["web_blocked"], cb["closed_book"])}
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
    if "confirmatory" in summary:
        summary["confirmatory"]["holm_ref"] = summary.get("holm", {})
        blocks.update(blocks_confirmatory(summary["confirmatory"]))
    if "h3" in summary:
        blocks.update(blocks_h3(summary["h3"]))
    if "h4" in summary:
        blocks.update(blocks_h4(summary["h4"], summary.get("holm", {})))
    if "e2" in summary:
        blocks.update(blocks_e2(summary["e2"]))
    e3_file = RESULTS / "e3" / "judgements.jsonl"
    if e3_file.exists():
        recs = [json.loads(x) for x in e3_file.read_text(encoding="utf-8").splitlines() if x.strip()]
        summary["e3"] = {"n": len(recs), "judgements": {k: sum(r.get("judgement") == k for r in recs)
                                                       for k in ("agrees", "partially", "disagrees", "gold_supports_claim", "error")}}
        blocks.update(blocks_e3(recs))
    blocks.update(blocks_budget())
    blocks.update(blocks_eval_table(summary))
    blocks.update(blocks_readme(summary))
    blocks.update(blocks_abstract(summary))
    for key, text in blocks.items():
        (GEN / f"{key}.md").write_text(text, encoding="utf-8")
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    export_site(summary)
    for target in [ROOT / "README.md", *sorted((ROOT / "reports").glob("*.md")), ROOT / "reports" / "paper" / "paper.md"]:
        fill(target, blocks)
    print(json.dumps({k: (v.get("H1", {}).get("accuracy_diff") if isinstance(v, dict) else v) for k, v in summary.items()}, indent=1)[:1500])


if __name__ == "__main__":
    main()
