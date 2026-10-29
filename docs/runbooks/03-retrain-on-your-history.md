# Runbook 03: retrain on your own history

1. `pip install -e ".[train]"` (numpy), then `deployrisk train /path/to/repo1 /path/to/repo2 ...`. It writes `deployrisk/data/model_{diff_only,full}.json` and `eval.json`.
2. Read the evaluation (`python evals/run.py`): check that AUC beats the lines-changed baseline on the chronological holdout for YOUR repos, and that Brier beats the base rate.
3. If your fix commits do not say "fix"/"revert", adjust `FIX_MSG` in `deployrisk/features.py` or label from your incident tracker instead.
4. Rebuild the image and redeploy. Keep the evaluation with the model in version control.
