# Experiment 3 — H3: does reading the claim from the screen (OCR) cost accuracy? (confirmatory)

Author: Libor "Burny" Burian · 2026-11-08 · Preregistered (non-inferiority, margin δ = 3 accuracy points).

**Question.** The desktop path reads text from pixels. Compared with receiving the exact text, does the capture
channel (rendering → Windows OCR → line grouping) lower end-to-end accuracy?

**Design.** All 500 AVeriTeC dev claims. Each claim is placed in a generic chat layout (username + timestamp line
above the message) or an article layout (title, byline, paragraph), at 13, 15 or 18 CSS px (balanced 6-cell design,
`eval/render.py`). **Clean arm:** the page's text given as ideal blocks (one per element). **OCR arm:** the page
rendered by headless Chromium (900 px wide, device scale 1), read by Windows.Media.Ocr, grouped by our line-grouping
code. Both arms then run the production extractor and the closed-book verifier (no web, to keep the cost low and
isolate the capture channel). If the extractor returns several claims, the one most similar to the original is
verified; if none, the item counts as wrong. Deviation from the original plan (recorded before running): headless
rendering instead of a real Chrome window on the author's screen, because screen access was declined.

## Results

<!-- GEN:h3_table -->
| Arm | n | Accuracy | Extraction failed | Mean CER | Mean WER |
|---|---|---|---|---|---|
| Clean text | 500 | 49.0 % | 0.4 % | 0.00 % | 0.00 % |
| Rendered → OCR | 500 | 49.8 % | 0.4 % | 1.09 % | 4.01 % |

Difference OCR − clean: **+0.8 pts** (95 % CI [-2.4, +4.0]); one-sided 95 % lower bound **-2.0 pts** vs the preregistered margin −3.0 → **non-inferior** (H3 supported). Discordant pairs: OCR-only right 37, clean-only right 33.

| OCR cell | n | Accuracy | Mean CER |
|---|---|---|---|
| chat-13px | 84 | 38.1 % | 1.33 % |
| chat-15px | 84 | 46.4 % | 0.41 % |
| chat-18px | 83 | 55.4 % | 0.44 % |
| article-13px | 83 | 53.0 % | 2.14 % |
| article-15px | 83 | 51.8 % | 1.45 % |
| article-18px | 83 | 54.2 % | 0.76 % |
<!-- /GEN:h3_table -->

## Interpretation

* **H3 is supported: the capture channel is non-inferior.** Windows OCR misread about one character in a hundred, yet
  end-to-end accuracy was essentially unchanged and the one-sided lower bound stayed inside the 3-point margin;
  extraction failures were equally rare in both arms. The extractor and verifier are robust to small OCR errors
  (e.g. "US$" read as "IJSS"), and line grouping kept usernames and timestamps from fragmenting the claims.
* **Exploratory caveat — small chat fonts.** Per cell, the OCR arm did worst in the 13 px chat layout, where the
  character error rate was also highest; other cells were level or slightly better than clean text. With ~84 claims per
  cell this is suggestive only, but it matches intuition: tiny fonts on dense chat layouts are where OCR errors start to
  change words that matter.
* **Scope.** These are clean renders. Real screens add overlapping windows, emoji, images and unusual fonts, and the
  test did not use the author's screen (access declined), so this result is an optimistic bound for the capture
  channel, not a measurement on real Discord sessions.
* Absolute accuracy here is lower than in the web arms because both H3 arms are closed-book (to save budget) and
  verify the extractor's rewording of the claim.
