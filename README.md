# fact-check-websites-apps

A Windows app that fact-checks the claims you see — in Chrome (via an extension that underlines claims
inline) and in any desktop app such as Discord (via on-screen OCR with an overlay and a side panel).
Each claim is checked by a [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview.md) agent running Haiku 5.5 with web search, page fetch,
an audited file-download tool and a sandboxed bash shell (WSL2 + bubblewrap). Every verdict uses the
four [AVeriTeC](https://arxiv.org/abs/2305.13117) labels and comes with a confidence score, the reasoning for that confidence, sources,
and — for false or cherry-picked claims — a sourced correction.

**Status:** work in progress. See [`reports/literature-review.md`](reports/literature-review.md) for prior work. A full README
(architecture, setup, results) follows as the components land.
