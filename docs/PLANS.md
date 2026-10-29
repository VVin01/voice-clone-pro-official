# Plans, prices and limits — FINAL for the 2026-11-01 premium release

Status: **FINAL, decided. Not live yet** — this tree is not merged or
deployed. These prices are implemented consistently in all places
from one catalogue (`PLAN_CATALOGUE` in `app/services/payments.py`):
DB seed minute packs, API `/vip/plans` (all 8 codes with categories),
Mini App prices in `app/webapp/static/js/i18n.js` + `index.html`
(category sections and buy buttons), and the bot buy menu (three
sections).

The code enforces two account states — Free vs PURCHASED (VIP) —
plus minute packs and bundles that an admin approves manually after
a slip upload. This document does NOT invent a subscription system.

## Final price table (MMK) — three separate paid categories

Free tier stays separate from all three paid categories. The single
source of truth is `PLAN_CATALOGUE` in `app/services/payments.py`;
API `/vip/plans`, Mini App buttons, bot buy menu and this document
must all match it.

### (a) Voice-only

| Plan | Price (MMK) | What you get | Per-unit | Why (one line) |
|---|---|---|---|---|
| Voice Pack MIN_10 | 8,000 | 10 voice minutes | 800/min | Kept old entry price: familiar, low barrier. |
| Voice Pack MIN_30 | 20,000 | 30 voice minutes | 667/min | Value improves with size. |
| Voice Pack MIN_60 | 38,000 | 60 voice minutes | 633/min | Best pack value before bundles. |
| Voice Pro | 75,000 | 250 voice minutes | 300/min | Bulk discount remains, no longer undercuts pack value. |

### (b) Recap-only

| Plan | Price (MMK) | What you get | Per-unit | Why (one line) |
|---|---|---|---|---|
| Recap Starter (NEW) | 20,000 | 5 recap credits, up to 10 min each | 4,000/credit | New low entry: previously recap started at 90,000, out of reach for many Myanmar users; per-credit is higher than Pro, so the tier ladder still falls with size. |
| Recap Pro | 90,000 | 25 recap credits, up to 10 min each | 3,600/credit | A 10-min HD recap is a premium product, not a cheap add-on. |

### (c) Combined Voice+Recap

| Plan | Price (MMK) | What you get | Per-unit | Why (one line) |
|---|---|---|---|---|
| Combined Starter (NEW) | 30,000 | 30 voice min + 5 recap credits | 25% below separate (40,000) | New low combined entry: previously combined started at 180,000; this lets a user try both products in one plan without the Master price. |
| Master | 180,000 | 500 voice min + 60 recap credits | far below separate at bundle rates (366,000) | Still the agency bundle; discount deepens with size. |

### Free (separate)

| Plan | Price (MMK) | What you get | Per-unit | Why (one line) |
|---|---|---|---|---|
| Free | 0 | 3 min/month, 5 jobs/day, 1000 chars/gen, recap 2 min watermarked | — | Low barrier to try the product. |

### Curated voices — human-only, count stays 12

All 12 curated VoxCPM2 personas are human voices. Two slots were
previously presented as non-human characters and were CONVERTED
(same slot id and name, underlying voice remapped to an existing
human persona — no new audio file, no model id invented, nothing
re-recorded or re-trained):

- `komyat` (Ko Myat): was ghost-story character → now human voice
  of `komin` (male human storyteller). Reference audio + transcript,
  control instruction, pitch/formant and display all human.
- `machaw` (Ma Chaw): was anime/cartoon character → now human voice
  of `mamay` (female human friendly guide). Same remap mechanism.

The other 10 personas were already natural human voices and are
unchanged. Legacy character sample slugs (kartoon, ant-movie,
the-light) no longer serve character samples through
`/voices/sample` — they resolve to the default human sample.

Limits (unchanged): Free/PURCHASED chars 1000/5000 per generation,
daily jobs 5/unlimited, concurrency 1 job/user, rate limits generate
30/min, recap 10/min, extract 30/min, slip 20/min; upload caps slip
image-only 10 MB, reference audio 50 MB, recap video 1 GB.

## Competitor comparison — verified vs not-found

- Team Alpha AI, Transcript AI, Mel Voice Clone: **no verified
  Myanmar price found in public search on 2026-10-29.** The names
  return unrelated or differently-named products (a US AI receptionist
  service, generic transcription tools, and a voice workspace with no
  prices shown). No price is claimed for them here, and no price in
  this table is based on an invented competitor price.
- Verified global proxies (public pages, 2026-10-29): a video-dubbing
  voice-clone service at $9/month for 60 minutes and $39/month for
  160 minutes; a US AI receptionist with voice cloning at
  $39.99/month for 300 call minutes; AssemblyAI Burmese STT at
  $0.15–$0.21/hour. These show the old Voice Pro rate (180 MMK/min)
  was far below global per-minute value, which is why bundles were
  raised while the familiar 8,000 MMK entry pack was kept.
- Verified Myanmar context: KBZPay/WavePay are the dominant mobile
  wallets; cards are not widespread for ordinary users. All prices
  are round, payable in one wallet transfer.
- Groq Whisper free-tier figures seen in secondary summaries are
  NOT quoted as a guarantee; confirm current limits on the Groq
  console before promising volume.

## Payment — release blocker, unchanged by prices

Payment account details live ONLY in env (`PAYMENT_KPAY_NUMBER`,
`PAYMENT_KPAY_NAME`, `PAYMENT_WAVE_NUMBER`, `PAYMENT_WAVE_NAME`) or
in admin dynamic settings. The `/vip/plans` endpoint no longer falls
back to hard-coded account details when env is empty — it returns
empty values instead. **Before any deploy, payment env/admin settings
must be filled, or the payment display will be blank.** Never put
account details in this document, in chat, or in code defaults.
