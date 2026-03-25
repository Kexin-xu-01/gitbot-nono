# gitbot — GitHub Issue Triage Bot

An automated GitHub issue triage bot that labels incoming issues and posts an AI-generated first response using project docs and recent issues as context. Security-sensitive reports are flagged for human review.

Gitbot runs as a persistent process with credentials, makes outbound network calls, and loads an instruction file (`GEMINI.md`) whose integrity can be verified by `nono trust` before the process reads it.

```
GitHub Issue Opened
       │
       ▼
  smee.io / ngrok
  (webhook relay)
       │
       ▼
  POST /webhook
  ┌────────────────────────────────────────┐
  │  validate HMAC-SHA256 signature        │
  │  parse event (issues.opened only)      │
  │  build_context()                       │
  │    ├─ project docs  (DOCS_URL, cached) │
  │    ├─ recent issues  (GitHub, TTL 5m)  │
  │    └─ GEMINI.md  (trust-verified)      │
  │  run_triage()  ──► Gemini 2.5 Flash   │
  │  post_response()                       │
  │    ├─ ensure label exists              │
  │    ├─ apply label                      │
  │    ├─ post comment                     │
  │    └─ log escalation (if security)     │
  └────────────────────────────────────────┘
       │
       ▼
  200 OK  (always, to prevent GitHub retries)
```

To add an extra layer of security, nono sandbox has been used to confine Gitbot. Read more about nono at https://github.com/always-further/nono. If you want to read more on how to wrap an AI agent in the nono sandbox, here is a blog: https://nono.sh/blog/wrapping-github-bot-with-nono.

---

## Setup

### 1. Prerequisites

- Python 3.11+
- A GitHub repo with admin access
- A GitHub personal access token with `issues:write` and `repo` scopes
- A Gemini API key
- (Optional) `nono` CLI — for sandboxed execution with credential injection

### 2. Install

```bash
git clone https://github.com/always-further/gitbot-nono.git
cd gitbot-nono
uv sync
cp .env.example .env   # fill in values
```

### 3. Configure `.env`

```bash
WEBHOOK_SECRET=your_webhook_secret
GITHUB_REPO=owner/repo
DOCS_URL=https://your-project-docs.example.com   # optional — project docs for context enrichment
DEBUG=false
```

`DOCS_URL` is optional. When set, the bot fetches the page and includes it (truncated to 3000 chars) in the Gemini prompt so the LLM has project context when triaging issues. When unset, the docs section is simply omitted.

### 4. Configure GitHub webhook

1. Go to your GitHub repo → Settings → Webhooks → Add webhook
2. **Payload URL**: your smee.io channel URL (create one at [smee.io](https://smee.io))
3. **Content type**: `application/json`
4. **Secret**: the same value you put in `WEBHOOK_SECRET`
5. **Events**: select "Issues" only

### 5. Edit `GEMINI.md`

`GEMINI.md` is the LLM system prompt that controls triage behavior — labels, comment tone, escalation rules. Edit it for your project:

- Update the project name and description
- Update the security contact email
- Adjust the label taxonomy if needed

If using nono, re-sign after editing (see step 7).

### 6. Run (without nono)

```bash
export GITHUB_TOKEN=ghp_your_token
export GEMINI_API_KEY=your_gemini_key
source .env
.venv/bin/python3 bot.py
```

In a second terminal, forward webhooks:

```bash
npm install --global smee-client
smee --url https://smee.io/your-channel --port 5001 --path /webhook
```

Set `DEBUG=true` in `.env` for verbose logging.

---

## Running with nono (optional)

nono adds kernel-enforced sandboxing and credential injection. The bot never sees real API keys — only phantom tokens that are swapped for real credentials by a localhost proxy.

### Store credentials in the keychain

```bash
security add-generic-password -s "nono" -a "github_token" -w "ghp_your_real_token" -T /opt/homebrew/bin/nono
security add-generic-password -s "nono" -a "gemini" -w "your_gemini_key" -T /opt/homebrew/bin/nono
```

### Generate your signing key

The repo ships with the original author's public key in `trust-policy.json`. Replace it with your own:

```bash
nono trust keygen
nono trust export-key   # copy the output
```

Paste the output into `trust-policy.json` as `public_key`:

```json
{
  "version": 1,
  "publishers": [
    {
      "name": "local-dev",
      "key_id": "default",
      "public_key": "<paste nono trust export-key output here>"
    }
  ],
  "instruction_patterns": ["GEMINI.md"],
  "blocklist": { "digests": [] },
  "enforcement": "deny"
}
```

### Sign the files

```bash
nono trust sign --key default GEMINI.md
nono trust sign-policy --key default
nono trust verify GEMINI.md --policy ./trust-policy.json   # should exit 0
```

### Run under nono

```bash
nono run --profile gitbot-profile.json --allow-cwd --listen-port 5001 \
  --proxy-credential gemini \
  --proxy-credential github \
  -- .venv/bin/python3 bot.py
```

If your `DOCS_URL` points to a domain not in `gitbot-profile.json`'s `network.allow_hosts`, add it there.

### nono security features

- **Trust verification** — `GEMINI.md` is verified before the process can read it; tampering causes an immediate exit
- **Filesystem policy** — reads from `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gcloud`, `~/.kube` are blocked at the kernel level
- **Network policy** — only allowed hosts are reachable outbound
- **Phantom token proxy** — real credentials never enter the sandbox; the process only holds session-scoped phantom tokens

---

## Modifying bot behavior

Edit `GEMINI.md`, then (if using nono) re-sign and commit:

```bash
nono trust sign --key default GEMINI.md
git add GEMINI.md GEMINI.md.bundle
git commit -m "Update and re-sign bot instructions"
```

---

## Tests

```bash
uv run pytest tests/ -v
```

- `test_triage.py` — JSON parsing, label validation, prompt length budget
- `test_github_api.py` — label idempotency, label-before-comment ordering, escalation logging
- `test_bot.py` — HMAC validation, event filtering, pipeline smoke test

---

## Production Path

- Sign `GEMINI.md` automatically in CI using [nono-attest](https://github.com/marketplace/actions/nono-attest) and switch the trust policy to a keyless OIDC publisher
- Replace smee.io with a real public HTTPS endpoint
- Run under `nono wrap` as PID 1 in a container
- Store credentials in the cloud keychain, not Apple Passwords
