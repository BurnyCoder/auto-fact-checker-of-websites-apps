"""Eval metrics against hand-computed values, and dataset/policy sanity checks."""

from __future__ import annotations

import pytest

from eval import metrics as m


def test_accuracy_and_macro_f1_hand_computed():
    gold = ["Supported", "Refuted", "Refuted", "Not Enough Evidence"]
    pred = ["Supported", "Refuted", "Supported", "Refuted"]
    assert m.accuracy(gold, pred) == 0.5
    # per-class F1: Supported 2/3, Refuted 2/3 (P=1/2,R=1/2 -> 0.5? compute: Refuted TP=1, FP=1, FN=1 -> F1=0.5),
    # Supported TP=1, FP=1, FN=0 -> 2/3; NEE 0; Conflicting 0 (no support, zero_division=0) -> mean over 4 labels
    assert m.macro_f1(gold, pred) == pytest.approx((2 / 3 + 0.5 + 0 + 0) / 4)


def test_mcnemar_exact():
    a = [1, 1, 1, 1, 1, 0, 1, 1]
    b = [0, 0, 0, 1, 1, 1, 1, 1]
    res = m.mcnemar_one_sided(a, b)                  # b=3 (A only), c=1 (B only): P(X>=3 | n=4) = 5/16
    assert (res["b_a_only"], res["c_b_only"]) == (3, 1) and res["p_one_sided"] == pytest.approx(5 / 16)


def test_brier_ece_selective():
    conf = [0.9, 0.9, 0.6, 0.6]
    corr = [1, 0, 1, 1]
    assert m.brier(conf, corr) == pytest.approx((0.01 + 0.81 + 0.16 + 0.16) / 4)
    # 2 equal-mass bins: {0.6,0.6 -> acc 1.0} and {0.9,0.9 -> acc 0.5}: ECE = 0.5*0.4 + 0.5*0.4
    assert m.ece_equal_mass(conf, corr, n_bins=2) == pytest.approx(0.4)
    sel = m.selective_accuracy(conf, corr, coverages=(1.0, 0.5))
    assert sel[0]["accuracy"] == 0.75 and sel[1]["accuracy"] == 0.5


def test_paired_bootstrap_identical_arms_is_zero():
    gold = ["Supported", "Refuted"] * 20
    res = m.paired_bootstrap(m.accuracy, gold, gold, gold, n_boot=200)
    assert res["diff"] == 0 and res["ci95"] == [0.0, 0.0]


def test_holm_and_fleiss():
    out = m.holm({"H1": 0.01, "H2": 0.04, "H3": 0.03})
    assert out["H1"]["p_holm"] == pytest.approx(0.03) and out["H1"]["reject"]
    assert m.fleiss([["a", "a", "a"], ["b", "b", "b"], ["a", "a", "a"]]) == pytest.approx(1.0)


def test_three_class_merge():
    assert m.three_class(["Not Enough Evidence", "Refuted"]) == ["Other", "Refuted"]


def test_datasets_and_policies():
    from eval.conditions import factcheck_blocked_policy, leakage_policy
    from eval.datasets import load_averitec, load_checkthat

    dev = load_averitec("dev")
    assert len(dev) == 500 and {c.label for c in dev} == {"Supported", "Refuted", "Not Enough Evidence",
                                                          "Conflicting Evidence/Cherrypicking"}
    assert len(load_checkthat("test")) == 341
    assert leakage_policy().check("https://huggingface.co/datasets/x") and leakage_policy().check("https://www.bbc.com/") is None
    fb = factcheck_blocked_policy()
    assert fb.check("https://www.politifact.com/x") and fb.check("https://web.archive.org/web/1/https://fullfact.org/a")
    assert fb.check("https://example-news.com/fact-check/foo") and fb.check("https://www.who.int/") is None


def test_vectorised_metrics_match_sklearn_and_loops():
    import numpy as np
    from sklearn.metrics import f1_score

    from factcheck.models import LABELS
    rng = np.random.default_rng(1)
    gold = list(rng.choice(LABELS, 200, p=[.25, .6, .08, .07]))
    pred = list(rng.choice([*LABELS, "NONE"], 200))
    (g, p), classes = m._encode(gold, pred)
    assert m._macro_f1_rows(g[None], p[None], classes)[0] == pytest.approx(
        f1_score(gold, pred, labels=list(LABELS), average="macro", zero_division=0))
    # bootstrap CI from the vectorised path equals an explicit loop with the same indices
    idx = np.random.default_rng(0).integers(0, 200, (500, 200))
    loop = [m.macro_f1(list(np.array(gold)[i]), list(np.array(pred)[i])) for i in idx]
    assert m.bootstrap_ci(m.macro_f1, gold, pred, n_boot=500) == pytest.approx(
        [float(np.percentile(loop, 2.5)), float(np.percentile(loop, 97.5))])


def test_permutation_f1_detects_clear_difference():
    import numpy as np
    gold = np.array([1, 0] * 100, bool)
    good, bad = gold.copy(), np.zeros(200, bool)
    assert m.permutation_f1(gold, good, bad, n=2000) < 0.01
    assert m.permutation_f1(gold, good, good, n=2000) == pytest.approx(1.0)


def test_ece_does_not_depend_on_row_order_with_ties():
    import numpy as np
    conf = np.array([0.85] * 6 + [0.6] * 4)
    corr = np.array([1, 0, 1, 1, 0, 1, 1, 0, 0, 1])
    perm = np.random.default_rng(3).permutation(10)
    assert m.ece_equal_mass(conf, corr) == pytest.approx(m.ece_equal_mass(conf[perm], corr[perm]))
