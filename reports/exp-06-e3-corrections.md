# Experiment 6 — E3: are the corrections faithful? (exploratory)

Author: Libor "Burny" Burian · 2026-11-08 · Exploratory (preregistered as such).

**Why.** For false or cherry-picked claims the app shows a corrected claim. A correction that contradicts the
professional fact-check would mislead more than no correction. **Design.** For every confirmatory `web_default` claim
the agent labelled Refuted or Conflicting, a tool-less Haiku judge compared the agent's corrected claim with AVeriTeC's
gold justification (`eval/judge.py`): agrees / partially / disagrees / gold says the claim is true. 20 random judgements
are listed in [`results/e3/hand_check.md`](../results/e3/hand_check.md) for reading by hand. Limitation: the judge is the
same model family as the system being judged and may be lenient.

## Results

<!-- GEN:e3_table -->
| Judge verdict | Count | Share |
|---|---|---|
| agrees | 45 | 51.7 % |
| partially | 33 | 37.9 % |
| gold_supports_claim | 6 | 6.9 % |
| disagrees | 3 | 3.4 % |

87 corrections judged (web default arm, predicted Refuted or Conflicting). Correction sources whose quote could be checked on the live page: 89, of which verified 60.7 %.
<!-- /GEN:e3_table -->

## Interpretation

* Most corrections agree fully or partially with the professional fact-checkers; outright contradictions are rare.
  The remaining cases are mostly claims the professionals rated true, i.e. the label was wrong rather than the
  correction being unfaithful.
* **Hand check of the judge** ([`results/e3/hand_check.md`](../results/e3/hand_check.md); read by the AI agent that built this project, not a human
  annotator): the judge was correct or defensible on most of the 20 sampled items and **too strict** on the rest
  (rating as "partially" or "disagrees" corrections that state the same finding as the professionals), never too
  lenient. The share of faithful corrections is therefore more likely under- than over-estimated.
* Many correction sources could be checked on the live page and most of those quotes were found; the rest are flagged in
  the UI.
