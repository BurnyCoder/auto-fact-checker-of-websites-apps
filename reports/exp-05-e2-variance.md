# Experiment 5 — E2: run-to-run variance (exploratory)

Author: Libor "Burny" Burian · 2026-11-08 · Exploratory (preregistered as such).

**Why.** The Agent SDK exposes no temperature or seed ([configuration docs](https://code.claude.com/docs/en/agent-sdk/configuration.md)), so the same claim can get a
different verdict on a second run. Without measuring this, a difference between two arms could be mistaken for an
effect of the tools. **Design.** The `closed_book` arm was run twice more on the first 100 claims of the
preregistered dev sample (repeat 1 = the confirmatory run). Fleiss' κ across the three runs; accuracy per run.

## Results

<!-- GEN:e2_table -->
Closed-book verifier run 3 times on the first 100 sampled claims: Fleiss' κ = **0.685**, identical label in all 3 runs for 68.0 % of claims, accuracy per run 51.0 %, 52.0 %, 50.0 % (SD 1.0 pts).
<!-- /GEN:e2_table -->

## Interpretation

* Agreement across runs is substantial but far from perfect: about a third of claims received a different label in at
  least one of three runs, mostly on borderline claims.
* Aggregate accuracy barely moved between runs, so benchmark-level comparisons are stable; the H1 difference is many
  times larger than the run-to-run spread of the closed-book arm.
* For users, a single verdict on a borderline claim should not be taken as definitive; the stated confidence and the
  sources matter more than the label alone. (Measured on the closed-book arm only, to save budget.)
