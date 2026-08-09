# agstudio gitea — setup notes (autodev Step 4)

Agent-only Gitea for auto-dev jobs on agstudio. No human accounts;
registration disabled. clusterintent only thinly tracks "gitea runs on
agstudio" — this directory is the source of truth for how it is deployed.

History: agstudio's old experimental gitea (the `localgit` service inside
`~/services/service_scripts/docker-compose.yml`, bind-mounted at
`~/services/gitea_data`) was removed per plan — container, data, and compose
entry — before this fresh deploy. Deployed 2026-08-07, Gitea 1.27.1.

## Deploy

```bash
cd devenv/gitea
docker compose up -d
```

Data lives in the named volume `gitea_autodev_gitea_data`. The web installer
is skipped via `INSTALL_LOCK=true` + sqlite3, so the instance is usable
immediately. Endpoints:

- HTTP/API: `http://agstudio.local:3000/`
- SSH: `agstudio.local:2222`

## One-time account setup (already done; repeat only on a rebuilt volume)

```bash
# admin user; password generated with `openssl rand -base64 24` and stored in
# agautolab/.local/gitea/autolab-agent.password (never tracked)
docker exec -u git autodev-gitea gitea admin user create \
  --username autolab-agent --password "$(cat ../../.local/gitea/autolab-agent.password)" \
  --email autolab-agent@agstudio.local --admin --must-change-password=false

# API token -> agautolab/.local/gitea/autolab-agent.token (never tracked)
docker exec -u git autodev-gitea gitea admin user generate-access-token \
  --username autolab-agent --token-name autolab --scopes all --raw \
  > ../../.local/gitea/autolab-agent.token

# org for auto-dev job repos
curl -X POST http://localhost:3000/api/v1/orgs \
  -H "Authorization: token $(cat ../../.local/gitea/autolab-agent.token)" \
  -H "Content-Type: application/json" \
  -d '{"username":"autodev","visibility":"public"}'
```

Verified 2026-08-07: repo create via API (201), push over HTTP with the token,
clone round-trip, then smoke repo deleted (204). Also reachable as
`http://agstudio.local:3000/` (200).

## Conventions

- Job repos live under the `autodev` org (e.g. `autodev/othello-web`).
- Repos are **public by default**: the org is public, and the compose file
  sets `DEFAULT_PRIVATE=public` + `DEFAULT_PUSH_CREATE_PRIVATE=false` so both
  API-created and push-created repos land public. A private repo is an opt-in
  (`"private": true` at create time) requested by the mission.
  Changed 2026-08-09; the org and all seven repos existing then were flipped
  from private to public.
- Clone/push URL for agents:
  `http://autolab-agent:<token>@agstudio.local:3000/autodev/<repo>.git`
  (token read from `.local/`; never write this URL into tracked files).
