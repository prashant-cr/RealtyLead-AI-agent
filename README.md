# RealtyLead AI Agent

An AI lead-qualification and follow-up agent for real estate agents and small
brokerages. A property enquiry arrives on WhatsApp, gets an instant reply, is
qualified through a natural conversation, scored with reasons, booked onto the
agent's calendar, and nurtured with spaced follow-ups if it goes quiet — all
logged to a dashboard the human agent can take over at any moment.

Built for India first (WhatsApp-centric, English/Hindi/Gujarati), with pluggable
channels so SMS and email can follow.

---

## Contents

- [What it does](#what-it-does)
- [Run it locally in 5 minutes](#run-it-locally-in-5-minutes)
- [How to test it](#how-to-test-it) ← **start here**
- [Connecting the real integrations](#connecting-the-real-integrations)
- [How it works](#how-it-works)
- [API reference](#api-reference)
- [Project layout](#project-layout)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [What is not done yet](#what-is-not-done-yet)

---

## What it does

```
   WhatsApp message
          │
          ▼
   ┌──────────────┐   signature check, dedup, 200 in <100ms
   │   Webhook    │──────────────────────────────────────┐
   └──────────────┘                                      │
                                                         ▼
                                             ┌───────────────────────┐
                                             │  Conversation engine  │
                                             │  (Claude + 6 tools)   │
                                             └───────────┬───────────┘
             ┌───────────────────────────────────────────┼─────────────┐
             ▼                    ▼                      ▼             ▼
      look up listings    score the lead          book a visit    escalate to
      (never invents)     (explainable)           (Google Cal)     the human
             │                    │                      │             │
             └────────────────────┴──────────┬───────────┴─────────────┘
                                             ▼
                                  ┌─────────────────────┐
                                  │  Postgres           │
                                  │  leads, messages,   │
                                  │  appointments,      │
                                  │  follow-up queue    │
                                  └──────────┬──────────┘
                       ┌─────────────────────┴───────────────────┐
                       ▼                                         ▼
             ┌───────────────────┐                   ┌────────────────────┐
             │ Follow-up worker  │                   │  Next.js dashboard │
             │ day 1,3,7,14,30…  │                   │  pipeline · take   │
             │ approved templates│                   │  over · transcripts│
             └───────────────────┘                   └────────────────────┘
```

**Rules that are enforced in code, not left to the model:**

- **Opt-out** ("STOP", "बंद करो", "બંધ કરો", "not interested") is detected and
  honoured *before* the model is called at all.
- **High-value leads** above the agent's budget threshold escalate to a human
  whatever the model decides.
- **Property facts** can only come from the agent's own listings — the assistant
  is told to say "let me check with [agent]" rather than guess.
- **Quiet hours** (21:00–09:00 in the lead's timezone) defer outbound nudges.
- **Tenant isolation** — every dashboard query filters by agent in its WHERE
  clause; there is no path that loads another agent's lead and then decides.

---

## Run it locally in 5 minutes

**Prerequisites:** Docker, Python 3.11+, Node 20+. No API keys needed to start.

```bash
git clone https://github.com/prashant-cr/RealtyLead-AI-agent.git
cd RealtyLead-AI-agent

make setup      # python venv + backend deps + creates .env from .env.example
make up         # Postgres + Redis in Docker
make migrate    # create the schema
make seed       # a demo agent, 3 listings, 2 leads
```

Then, in two terminals:

```bash
make run              # API on http://localhost:8000  (docs at /docs)

make dashboard-setup  # once: npm install + Playwright browser
make dashboard        # dashboard on http://localhost:3000
```

`make help` lists every target.

> **Ports 5432 or 6379 already in use?** Set `POSTGRES_PORT=5433` and
> `REDIS_PORT=6380` in `.env`, and update `DATABASE_URL` / `REDIS_URL` to match.
> See [Troubleshooting](#troubleshooting).

Prefer everything in Docker? `docker compose up --build` runs Postgres, Redis,
the API (migrations included) and the follow-up worker together.

---

## How to test it

### 1. See the whole product work — no API keys required

```bash
make demo
```

This plays a complete lead journey against your real database: an enquiry
arrives, the assistant looks up a listing, qualifies the buyer, scores them
80/100 with reasons, books a site visit, queues a follow-up, sends it as an
approved template, honours an opt-out, and escalates a price negotiation.

```
1. A serious buyer enquires
───────────────────────────
     lead │ Hi, is the Bopal flat still available?
      [tools: get_listing_details]
assistant │ Hi Priya! I'm Rohan Mehta's assistant at Sunrise Homes. Yes, the
            3 BHK in Bopal is available. What budget are you working with?
     lead │ Around 90 lakhs. Buying in 2 months, my home loan is approved.
      [tools: update_lead_profile, score_lead]
assistant │ That fits nicely. Would you like to see it in person this week?

2. What the human agent sees
────────────────────────────
  status      booked
  score       80/100 (hot)
    +30  Budget matches 2 of 3 active listings
    +25  Buying within 2 months
    +20  Home loan pre-approved
```

The model's *replies* are scripted — everything else (tools, database, scoring,
scheduling, the follow-up worker) is the real thing. This is the fastest way to
understand what the system does.

Then look at the result in the dashboard:

```bash
make token       # prints a token — copy it
make dashboard   # http://localhost:3000, paste the token
```

### 2. Run the test suite

```bash
make check       # backend: ruff + mypy --strict + 366 tests
make check-all   # the above plus the dashboard build and 34 Playwright specs
```

Tests need **no external services** — they run against in-memory SQLite. If
Postgres is running, `test_migrations.py` additionally verifies the migrations
against the real target:

```bash
cd backend
TEST_POSTGRES_URL="postgresql+asyncpg://realtylead:realtylead@localhost:5432/realtylead" \
  .venv/bin/pytest -q
```

### 3. Talk to the assistant yourself (needs an Anthropic key)

Put `ANTHROPIC_API_KEY=sk-ant-...` in `.env`, then:

```bash
make chat
```

You are the lead; type as if you were messaging on WhatsApp. Inside the chat:
`/profile` shows what the assistant has learned and why the lead scored what it
did, `/reset` starts over, `/quit` exits.

This is the only way to evaluate the assistant's actual writing and judgement.
Worth trying: give a budget, ask for a viewing, ask to speak to a person, ask for
a discount, and reply "STOP".

### 4. Test the WhatsApp webhook without a Meta account

The webhook verifies an HMAC signature over the raw body, so you can exercise it
with `curl`. Set `WHATSAPP_APP_SECRET=test-secret` in `.env`, recreate the API,
and map an agent to a phone number id:

```bash
docker compose exec -T postgres psql -U realtylead -d realtylead -c \
  "update agents set whatsapp_phone_number_id='PNID_TEST' where email='demo.agent@sunrisehomes.example';"
```

```bash
BODY='{"object":"whatsapp_business_account","entry":[{"id":"W","changes":[{"field":"messages","value":{
  "metadata":{"phone_number_id":"PNID_TEST"},
  "contacts":[{"profile":{"name":"Test Lead"},"wa_id":"919812340000"}],
  "messages":[{"from":"919812340000","id":"wamid.TEST1","timestamp":"1786500000",
               "type":"text","text":{"body":"Is the Bopal flat available?"}}]}}]}]}'

SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "test-secret" | awk '{print $2}')"

curl -s -X POST http://localhost:8000/webhooks/whatsapp \
  -H "Content-Type: application/json" -H "X-Hub-Signature-256: $SIG" -d "$BODY"
# {"accepted":1,"statuses_applied":0}
```

Send it twice — the second returns `"accepted":0`, because Meta retries are
deduplicated. Drop the signature header and it returns 401.

### 5. Test the dashboard against the real backend

The Playwright suite stubs the API by default (fast, hermetic). To drive the
real stack instead:

```bash
cd frontend
LIVE_TOKEN=rl_...  LIVE_URL=http://localhost:3000  npx playwright test live
```

This is what caught a missing CORS configuration that 19 stubbed tests could
not — worth keeping green.

### 6. Test the follow-up worker

```bash
make worker-once   # one pass over the due queue, then exits
make worker        # runs continuously, polling every 60s
```

`make demo` already exercises the full cadence with a jumped clock.

### 7. Test signup and onboarding

Open <http://localhost:3000/signin?mode=signup> and create an account. The
checklist walks through working hours, listings, WhatsApp and tone. Download the
sample CSV from the listings panel, edit it, and upload it back — try breaking a
price to see the per-line errors.

---

## Connecting the real integrations

All three are optional — the system runs without them, and degrades rather than
breaks when they are unavailable.

### Anthropic (required for real conversations)

```bash
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-opus-5     # default
ANTHROPIC_EFFORT=low              # low|medium|high|xhigh|max — raise if quality disappoints
```

### WhatsApp Business Cloud API

1. Create a Meta app with WhatsApp, and note the **Phone Number ID**.
2. Set in `.env`:
   ```bash
   WHATSAPP_ACCESS_TOKEN=...    # system user token, whatsapp_business_messaging scope
   WHATSAPP_VERIFY_TOKEN=...    # any string you choose
   WHATSAPP_APP_SECRET=...      # from the Meta app dashboard
   ```
3. Expose port 8000 publicly (`ngrok http 8000`) — Meta requires HTTPS.
4. Point the webhook at `https://<your-host>/webhooks/whatsapp` with your verify
   token, and subscribe to **messages**.
5. Set the agent's phone number id in the dashboard (Onboarding → Connect
   WhatsApp), or with SQL.

> **Follow-ups need approved templates.** The six templates in
> `backend/app/channels/templates.py` must be submitted in WhatsApp Manager under
> those exact names before any nudge will send — Meta rejects unapproved
> templates outside the 24-hour window. Replies inside a live conversation work
> immediately; only the nudges need this.

### Google Calendar

1. Create an OAuth client (type "Web application") in Google Cloud Console with
   the Calendar API enabled.
2. Register `http://localhost:8000/auth/google/callback` as a redirect URI.
3. Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `OAUTH_STATE_SECRET`
   (`openssl rand -hex 32`) in `.env`.
4. Each agent connects at `http://localhost:8000/auth/google/start?agent_id=<uuid>`.

Once connected, the agent's busy times are excluded when offering slots, and
every booking is written to their calendar with the lead's contact details,
budget, timeline and score reasons in the description.

---

## How it works

### The conversation engine

One inbound message in, one reply out, with tool calls in between. Six tools:

| Tool | What it does |
|---|---|
| `get_listing_details` | The only source of property facts. No result → the assistant says it will check. |
| `update_lead_profile` | Records budget, locations, size, timeline, loan status as they are learned |
| `score_lead` | Recomputes the 0–100 score and stores the reason for each factor |
| `check_availability` | Real open slots from working hours minus Google Calendar busy time |
| `book_appointment` | Only a slot `check_availability` returned, only after the lead agrees |
| `escalate_to_human` | Price talk, a request for a person, frustration, anything legal |

Prompts are versioned files in `backend/app/agent/prompts/` — never inlined in
code — so a regression can be rolled back by pinning the previous version.

### Lead scoring

Rule-based and explainable (ML is deliberately out of scope for v1). Weights sum
to 100:

| Factor | Max | Notes |
|---|---:|---|
| Budget match with inventory | 30 | 15 for a near miss within 15% |
| Purchase timeline | 25 | ≤3 months scores full |
| Financing | 20 | Pre-approved home loan |
| Responsiveness | 15 | How much they have engaged |
| Site-visit willingness | 10 | |

**70+ hot, 40–69 warm, below 40 cold.** The dashboard shows the reason for every
point, because "why is this lead hot?" is the question an agent actually asks.

### Follow-up cadence

Day 1, 3, 7, 14, then monthly — measured from the *lead's* last message, so a
reply resets the clock. Capped at six. Stops immediately on opt-out, booking or
human takeover. Anything due during the lead's local quiet hours is deferred to
the morning. The queue is the `follow_up_tasks` table (not Redis) so the schedule
survives restarts and is visible to the dashboard.

---

## API reference

Interactive docs at **http://localhost:8000/docs**.

**Public**

| Endpoint | |
|---|---|
| `GET /health`, `GET /health/ready` | liveness and readiness |
| `POST /auth/signup`, `POST /auth/login` | agent accounts |
| `GET,POST /webhooks/whatsapp` | Meta webhook (HMAC-authenticated) |
| `GET /auth/google/start`, `/callback` | Google Calendar OAuth |

**Authenticated** — `Authorization: Bearer <token>`

| Endpoint | |
|---|---|
| `GET /api/me`, `GET /api/stats` | current agent, pipeline counts |
| `GET /api/leads` | pipeline; filter by `status`, `temperature`, `search` |
| `GET /api/leads/{id}` | full detail with score reasons, appointments, follow-ups |
| `GET /api/leads/{id}/transcript` | the conversation |
| `POST /api/leads/{id}/takeover` · `/release` | manual takeover |
| `POST /api/leads/{id}/messages` | send as the human agent |
| `GET,PATCH /api/settings` | hours, tone, timezone, WhatsApp id |
| `GET /api/listings`, `POST /api/listings/import` | inventory and CSV import |
| `GET /api/onboarding` | setup checklist |

Two credential types, both bearer tokens: **session tokens** (`rls_…`, from
signing in, 14-day expiry, revocable per device) and **API tokens** (`rl_…`, from
`make token`, long-lived, for scripts and CI).

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/leads
```

### Importing listings

`POST /api/listings/import` takes a CSV. The parser is forgiving about shape and
strict about facts — portal column names (`Property Name`, `Cost`, `Bedrooms`)
and Indian price formats (`85 lakh`, `2.15 cr`, `85,00,000`) all work. Get a
starter file from `GET /api/listings/sample.csv`.

Import is **all-or-nothing**: if any row fails, nothing is written and every
problem is returned with its line number. A half-loaded catalogue would have the
assistant quoting from incomplete inventory with no way for the agent to tell.

---

## Project layout

```
backend/
  app/
    api/           FastAPI routers — health, webhooks, oauth, dashboard,
                   accounts, onboarding
    agent/         conversation engine, prompts/ (versioned), tools, scoring,
                   opt-out detection
    channels/      adapter interface, WhatsApp, in-memory, message templates
    models/        SQLAlchemy models — the domain
    services/      scheduling, Google Calendar, follow-ups, booking, ingestion,
                   passwords, sessions, CSV import
    workers/       the follow-up worker
    core/          config, database, logging (with PII masking)
    scripts/       seed, chat harness, demo, token issuer
  alembic/         migrations
  tests/           366 tests
frontend/
  app/             Next.js 14 App Router — pipeline, lead detail, signin,
                   onboarding
  lib/api.ts       typed client for the dashboard API
  e2e/             Playwright — stubbed specs plus a live-backend spec
docs/
  decisions.md     why things are the way they are (~30 entries)
  backlog.md       what is deliberately not done, and why
```

---

## Development

```bash
make test        # pytest
make lint        # ruff check + format check
make typecheck   # mypy --strict
make fmt         # auto-fix
make check       # all of the above
make check-all   # plus dashboard build + Playwright
```

**Conventions** (also in `CLAUDE.md`):

- Type hints everywhere; `ruff` and `mypy --strict` stay clean.
- Tests alongside features.
- Secrets only in `.env` (git-ignored). New keys go in `.env.example`.
- Prompts live in `backend/app/agent/prompts/` as versioned files.
- **No PII in logs** — use `mask_phone` / `mask_email` / `mask_name` from
  `app.core.logging`; a logging filter scrubs anything that slips through.
- Architectural decisions get a note in `docs/decisions.md`.

### Migrations

```bash
make revision m="add widget table"   # autogenerate
make migrate                          # apply
```

Autogenerated migrations that reference `app.models.types.UtcDateTime` need
`import app.models.types` added by hand — Alembic does not add it.

`test_migrations.py` diffs the migrated schema against the models on every test
run, so drift fails the build. Note it **drops the app's tables** as part of the
check — re-run `make migrate && make seed` afterwards if you were using that
database.

---

## Troubleshooting

**Port 5432 or 6379 already in use** — you have Postgres or Redis running
locally. Set `POSTGRES_PORT=5433` / `REDIS_PORT=6380` in `.env` and update
`DATABASE_URL` / `REDIS_URL` to match. Inside Docker the services talk over the
Docker network, so only host access is affected.

**Changed `.env` but the API still uses the old values** — `docker compose
restart` does **not** re-read `env_file`. Use
`docker compose up -d --force-recreate api`.

**`ModuleNotFoundError` in a container after adding a dependency** — the image is
stale. `docker compose build api worker && docker compose up -d api worker`.

**`No space left on device` from Postgres** — the Docker VM disk is full, not
your machine's. `docker builder prune -af` reclaims build cache safely (it
regenerates on the next build); `docker system df` shows what is using space.

**Dashboard shows "Could not load the pipeline"** — the browser is being blocked
by CORS. Add the dashboard's origin to `CORS_ALLOW_ORIGINS` in `.env` and
recreate the API container.

**`ANTHROPIC_API_KEY is not set`** — expected without a key. The webhook still
accepts and records messages; only the model turn is skipped. `make demo` works
regardless.

**Follow-ups are not sending** — check the templates are approved in WhatsApp
Manager, the worker is running (`docker compose ps`), and the lead has not opted
out, booked, or been taken over.

---

## What is not done yet

Being straight about it, since these matter before real leads:

1. **Webhook processing uses in-process background tasks.** A crash between the
   acknowledgement and the reply loses that turn, with no retry. Redis is already
   in the stack; moving this to a durable queue is the top priority.
2. **The WhatsApp templates have never been submitted** for Meta approval, so no
   follow-up will send until they are.
3. **No password reset** and **no rate limiting** on login or signup.
4. **The dashboard token is in `localStorage`**, readable by any script on that
   origin. An httpOnly cookie plus CSRF is the better end state.
5. **No realtime dashboard updates** — a new message needs a refresh.
6. **Google refresh tokens and transcripts are stored unencrypted.** Fine for a
   prototype; not for real agents' credentials under DPDP.

`docs/backlog.md` has the full list, grouped by the milestone that surfaced it.

---

## Compliance notes

- WhatsApp requires approved template messages for business-initiated
  conversations outside the 24-hour window — the follow-up worker uses them, and
  refuses to send free-form outside the window.
- Consent is recorded per lead and opt-out is honoured instantly, in code.
- India's DPDP Act and TRAI DND rules apply to outbound messaging.
- PII is masked in logs; model `__repr__`s never include names, phones or message
  content.
- RERA registration is stored per listing and included in calendar events.

## Licence

Not yet specified — add one before publishing.
