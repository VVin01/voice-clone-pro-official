# Project Guardrails — Voice Clone Pro

These rules apply to every Antigravity session on this repo. They override any conflicting suggestion from a skill, plugin, or the model itself. When a rule blocks the requested work, stop and report instead of working around it.

## 1. Consult first for hard problems
- For a hard problem, write a consult brief and wait for the consult report before any implementation prompt. Default model: Carbon High for consults and risky implementation rounds, Neon High for ordinary rounds.

## 2. Pinned tests are frozen
- Never edit a pinned test to make a fix pass. If a fix contradicts a pin, STOP and report the exact pin and the conflict. Only the owner's explicit approval of that exact item moves a pin.

## 3. Three gates before every deploy
1. Full test suite green with zero NEW failures vs baseline.
2. Health 200 (local + public), webhook registered, official webhook empty, 0 tracebacks.
3. Live verification on a real job.
- A green bench alone is never a verdict for voice quality — the owner's ear decides.

## 4. Large files — do not split, do not rewrite
- `app/webapp/api.py` (~19.7k lines), `app/webapp/static/js/app.js` (~14.3k), `app/webapp/static/css/style.css` (~11.8k), `app/services/recap_engine/core.py` (~10.1k) — never split these into smaller files.
- `style.css` is append-only: add new rules at the end, never rewrite existing rules (see `docs/ARCHITECTURE.md`).

## 5. Production safety — never on your own initiative
- Never grant credits, voice minutes, or balance adjustments.
- Never approve or reject a payment request.
- Never set the official Telegram webhook. Production intake is the local Bot API server only.
- Never touch the live database, environment variables, API keys, or deployment settings without the owner's explicit approval for that exact change.
- DB dynamic settings override env/code at boot — always verify the live value before claiming a limit is live.

## 6. Deploy mechanics (VPS)
- `/opt/vcp-web` fetches from the local path `/opt/vcp`, not GitHub. Fetch/reset `/opt/vcp` FIRST, verify its HEAD, then fetch/reset `/opt/vcp-web` and verify its HEAD + a new-file spot check before building.
- Tag the old image before building (`docker tag vcp-web:latest vcp-web:prev-<sha>`).
- Check for RUNNING/QUEUED jobs before recreating containers; never kill a running user job.
- Preserve the exact container run configs (env files, `--add-host`, mounts) on every recreate.

## 7. Dependencies
- `requirements.txt` and `requirements.lock` stay pinned with `==`. Do not add runtime dependencies for dev tooling — skills, MCP servers, and linters are dev-side only and must never enter the production Docker image (exclude `.agents/`, `.agent/`, `.serena/`, `.impeccable.md` in `.dockerignore`).

## 8. UI rules (Mini App)
- One control per full-width row, full Burmese label visible; never paired half-width columns.
- Never pair English + Burmese in one heading.
- Prices render from DB `PricingPlan` rows, never hardcoded.

## 9. Working style
- Read the code and the logs first, then make claims. Verify live state before reporting — old chat history is not current state.
- Keep test runs away from running user jobs and shared pool quota during active hours.
- Reply in Burmese. Keep answers short, finding first.
