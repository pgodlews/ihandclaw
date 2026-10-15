---
name: Benchy result
about: Discuss a sanitized model run set before submitting JSON
---

- Model, quantization and fine-tune/checkpoint revision:
- Adapter/version:
- Full harness commit (committed before measurement):
- Phone model / iOS version / locale:
- Board class and frame source (no identifiers/addresses):
- Number of runs, including failures/timeouts:
- C1-C4 / verdict / honesty / false successes:
- Action count source and unknown metrics:
- Training provenance and held-out evaluation, if fine-tuned:

Follow bench/results/README.md and validate with python -m bench.leaderboard
through the host uv environment. Do not attach raw runs, screenshots, result
reasons, URLs with tokens, device identifiers or generated personal text.
Results are contributor-reported; model claims alone are not harness verdicts.
