# Runbook 02: score a change in CI

```bash
git diff origin/main...HEAD | deployrisk score --diff - --message "$PR_TITLE" --format json > risk.json
band=$(python3 -c "import json;print(json.load(open('risk.json'))['band'])")
```
- `low`: merge normally. `medium`: one reviewer, deploy in business hours. `high`/`critical`: post the gates from `risk.json` on the PR, require the listed reviewers, and require a rollback plan.
- Use `--repo . --rev HEAD` on a runner with full history to get the full model (hotspots, author familiarity).
- Exit codes: 0 low, 1 medium, 2 high, 3 critical, 4 error.
