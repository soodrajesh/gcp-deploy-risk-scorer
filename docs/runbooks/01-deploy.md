# Runbook 01: deploy

1. `cp deploy.env.example deploy.env`; `gcloud auth login`.
2. `./scripts/up.sh` (foundation, image, service; saved plans; destroys need `ALLOW_DESTROY=1`).
3. `gcloud run services proxy deploy-risk --region us-central1 --port 8081`; try the examples in the UI.
4. Call it from CI with an identity token: `curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" -d '{"diff": "..."}' $URL/api/score`.
