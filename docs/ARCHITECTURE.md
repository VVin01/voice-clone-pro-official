# Architecture note — large files (documented, not refactored)

This note exists because several files in this project are very large.
Splitting them is a separate, higher-risk change and was deliberately
NOT done in polish-premium-2026-10-28. Do not refactor them in a
security or polish branch without a dedicated test plan.

## Large files (sizes on the security branch, approximate)

- `app/handlers/admin.py` — about 183 KB. Telegram admin console:
  user management, slips, broadcast, settings.
- `app/webapp/api.py` — about 136 KB. Mini App REST API: users, voices,
  recap, history, VIP slips, admin endpoints, worker upload.
- `app/services/recap_engine/core.py` — about 133 KB. Recap rendering
  core: transcription, subtitles, blur/mirror/zoom, ffmpeg.
- `app/services/recap_engine/recap_planner.py` — about 120 KB.
  Recap story planning and narration continuity.
- `app/webapp/static/js/app.js` — about 219 KB. Mini App frontend logic.
- `app/webapp/static/index.html` — about 118 KB. Mini App markup.
- `app/webapp/static/css/style.css` — about 105 KB. Mini App theme.
  A premium polish layer is appended at the end of this file
  (polish-premium-2026-10-28) instead of rewriting it.

## Why they grew

Features were added in place (bot + Mini App + recap + admin in one
service, mode `all`), so handlers, API routes, and frontend logic
accumulated in a small number of files. The ledger, queue claim,
stale-job recovery, and access-control middleware are the strongest
parts of the codebase and should not be moved casually.

## If you split them later

1. Split one file at a time, on its own branch, with its tests green
   before and after.
2. Keep API endpoint names, response shapes, and i18n keys identical.
3. Keep the security-branch behaviours: guest fallback removed,
   ownership checks, queue validation, separate WORKER_TOKEN,
   gate fail-closed, Telegram-only CORS.

## Deployment shape (live, 2026-10-28)

- Render service: `voice-clone-studio`, web_service, Oregon, standard
  plan, autoDeploy on, health endpoint `/health` in code.
- `render.yaml` in this repo is kept in sync with that shape, but it
  is not automatically the live definition — see its header comment.
- Database persistence is still the top unverified item: confirm in
  the Render Dashboard whether DATABASE_URL points to Render Postgres
  (free instance expiring 2026-11-18, per the 2026-10-28 check) or to
  local SQLite, and whether a persistent disk exists. Do not assume
  either from the API alone.
