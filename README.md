# RealtyLead AI Agent

AI lead qualification & follow-up agent for real estate agents and small brokerages.
An inbound property enquiry gets an instant reply, a natural qualifying conversation,
a score with reasons, and a booked appointment — logged where the human agent can see it.

See [CLAUDE.md](CLAUDE.md) for the product spec and [docs/decisions.md](docs/decisions.md)
for architecture decisions.

## Status

**M1 — Skeleton: done.** FastAPI app, domain models, migrations, docker-compose,
health checks, tests, lint/type gates.

**M2 — Conversation core: done.** Claude-powered engine with six tools, versioned
prompts, rule-based lead scoring, and a CLI harness you can talk to right now.

**M3 — WhatsApp adapter: done.** Meta Cloud API in and out, HMAC-verified
webhooks, at-least-once deduplication, delivery receipts, media handling.

**M4 — Booking: done.** Per-agent Google Calendar OAuth, free/busy-aware
availability, calendar events with a full lead briefing, graceful degradation.

**M5 — Follow-up worker: done.** Day 1/3/7/14-then-monthly cadence, approved
WhatsApp templates in three languages, quiet hours, hard cap, opt-out honoured.

**M6 — Dashboard: done.** Authenticated dashboard API plus a Next.js pipeline
board, transcripts, score reasons, manual takeover and agent-sent messages.

**M7 — Onboarding: done.** Agent signup with real password auth and sessions,
CSV listing import, working hours, and per-agent tone customisation.

All seven milestones from CLAUDE.md are built. See `docs/backlog.md` for what is
deliberately not done yet — the largest items are durable webhook processing and
getting the WhatsApp templates approved.

## Quickstart

```bash
make setup      # venv + deps + .env
make up         # Postgres + Redis via docker compose
make migrate    # apply migrations
make seed       # demo agent, 3 listings, 2 leads
make chat       # talk to the agent in your terminal  ← M2
make run        # http://localhost:8000/docs
make worker     # follow-up worker (or `make worker-once` for cron)  ← M5

make dashboard-setup   # once: npm install + Playwright browser
make token             # issue a dashboard token, then:
make dashboard         # http://localhost:3000  ← M6
```

`make chat` needs `ANTHROPIC_API_KEY` in `.env`. Inside it: `/profile` shows what
the agent has learned and why the lead scored what it did, `/reset` starts over,
`/quit` exits. Pass options with `make chat ARGS="--language hi --reset"`.

`make check` runs everything CI would: ruff, mypy, pytest. `make help` lists the rest.

Tests need no services — they run on in-memory SQLite. Start Postgres first if you
want `test_migrations.py` to verify the migrations against the real target; point it
at a non-default port with `TEST_POSTGRES_URL`.

Running the whole stack in Docker instead: `docker compose up --build`. The `api`
service applies migrations on startup, then serves on `:8000` with reload.

### Ports

If you already run Postgres or Redis locally, compose will fail to bind 5432/6379.
Set `POSTGRES_PORT` / `REDIS_PORT` in `.env` and update `DATABASE_URL` / `REDIS_URL`
to match — e.g. `POSTGRES_PORT=5433` with
`DATABASE_URL=postgresql+asyncpg://realtylead:realtylead@localhost:5433/realtylead`.
Inside compose the services talk over the Docker network, so only host access is affected.

## WhatsApp (M3)

Set `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_VERIFY_TOKEN` and `WHATSAPP_APP_SECRET` in
`.env`, then point a Meta app's webhook at `https://<host>/webhooks/whatsapp` and
map each agent to their number:

```sql
update agents set whatsapp_phone_number_id = '<PHONE_NUMBER_ID>' where email = '...';
```

An unmapped `phone_number_id` is refused rather than being routed to some other
agent's inventory. Locally, expose port 8000 with a tunnel (ngrok/cloudflared) —
Meta requires a public HTTPS URL.

Every delivery is authenticated with `X-Hub-Signature-256` over the raw body, so
an unsigned or replayed-with-different-content request is rejected with 401. The
endpoint acknowledges as soon as the message is recorded and runs the model turn
in the background; see `docs/decisions.md` for why, and for the durability
limitation that M5 fixes.

## Onboarding (M7)

```bash
make dashboard-setup     # once
make dashboard           # http://localhost:3000
```

Open http://localhost:3000/signin?mode=signup and create an account. The
checklist then walks through the four things needed to start taking leads:
working hours, listings, WhatsApp, and (optionally) tone and calendar.

**Listings** import from CSV. The parser accepts what agents actually have —
portal column names (`Property Name`, `Cost`, `Bedrooms`) and Indian price
formats (`85 lakh`, `2.15 cr`, `85,00,000`) — and rejects what it cannot read,
with line numbers. Import is all-or-nothing: a file with one bad row imports
nothing, because a half-loaded catalogue would have the assistant quoting from
incomplete inventory. Download the sample file from the onboarding page.

**Tone** feeds into the system prompt (`qualification_system_v2.md`). The
assistant still always identifies as an AI and follows the non-negotiable rules;
tone changes the voice, not the behaviour.

Passwords are hashed with `hashlib.scrypt`. Sign-in creates a session that
expires after 14 days and can be revoked; changing a password signs out every
device. `make token` still issues a long-lived API token for scripts and CI.

## Dashboard (M6)

```bash
make token               # API token for scripts, or just sign in
make dashboard           # http://localhost:3000
```

Sign in with your email and password, or paste an API token. The board shows every lead with its
score and temperature, filters for the ones that need a human, and a detail view
with the full WhatsApp transcript, the score broken down by reason, upcoming
appointments and scheduled nudges. **Take over** stops the assistant replying and
cancels pending follow-ups; **Hand back** returns it. The agent can also send
messages themselves, within WhatsApp's 24h window.

Authentication is a bearer token per agent (`make token` issues and rotates;
re-running it revokes the previous one). Every query is scoped to the token's
agent in its WHERE clause — one agent can never see another's leads. Real
accounts arrive in M7.

The dashboard is a separate origin, so the API's `CORS_ALLOW_ORIGINS` must list
wherever it is served from (defaults cover localhost:3000 and :3200).

`make e2e` runs the Playwright suite against a stubbed API; `e2e/live.spec.ts`
runs the same flows against a real backend when `LIVE_TOKEN` is set:

```bash
cd frontend && LIVE_TOKEN=rl_... LIVE_URL=http://localhost:3000 npx playwright test live
```

## Follow-ups (M5)

A lead who goes quiet is nudged on day 1, 3, 7 and 14, then monthly, measured
from *their* last message — replying resets the clock. Run the worker alongside
the API (`make worker`, or the `worker` service in docker-compose); it polls the
`follow_up_tasks` table every 60s.

Nudges stop immediately when the lead opts out, books, is handed to a human, or
hits the cap of six. Anything due during the lead's local quiet hours (21:00–09:00)
is deferred to the morning rather than dropped.

> **Before this can send anything:** the six templates in
> `app/channels/templates.py` must be submitted and approved in WhatsApp Manager
> under the same names. Meta rejects unapproved templates, and business-initiated
> messages outside the 24h window have no other route.

## Google Calendar (M4)

Create an OAuth client (type "Web application") in Google Cloud Console with the
Calendar API enabled, register `http://localhost:8000/auth/google/callback` as a
redirect URI, then set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and
`OAUTH_STATE_SECRET` (`openssl rand -hex 32`) in `.env`.

Each agent connects their own calendar by visiting:

```
http://localhost:8000/auth/google/start?agent_id=<uuid>
```

Once connected, their busy times are excluded when offering slots and every
booking is written to their calendar with the lead's contact details, budget,
timeline and score reasons in the description — so they can walk into the meeting
briefed. If the lead's email is known, Google sends them an invite too.

Connecting is optional: agents without a calendar still take bookings, stored in
the database only. If Google is unreachable, availability falls back to our own
appointments and bookings still succeed — a failed calendar write escalates to
the human agent rather than being dropped or shown to the lead.

## Layout

```
backend/
  app/
    api/         FastAPI routers (health today; webhooks + dashboard API later)
    agent/       conversation engine, prompts/, tools, scoring, opt-out
    channels/    adapter interface + in-memory channel; WhatsApp/SMS/email  (M3)
    models/      SQLAlchemy models — the domain
    services/    slot scheduling, quiet hours; Google Calendar          (M4)
    workers/     background jobs                                     (M5)
    core/        config, database, logging (incl. PII masking)
    scripts/     seed, chat harness, other one-offs
  alembic/       migrations
  tests/
frontend/        Next.js 14 dashboard — pipeline, transcripts, takeover
  app/           App Router pages
  lib/api.ts     typed client for the dashboard API
  e2e/           Playwright: stubbed specs + a live-backend spec
docs/            decisions, backlog, API contracts
```

## Conventions

- Type hints everywhere; `ruff` and `mypy --strict` must stay clean.
- Tests alongside features; `make test` before calling anything done.
- Secrets only in `.env` (git-ignored). Add new keys to `.env.example`.
- Prompts live as versioned files in `backend/app/agent/prompts/` — never inlined.
- Architectural decisions get a note in `docs/decisions.md`.
- **No PII in logs.** Use `mask_phone` / `mask_email` / `mask_name` from
  `app.core.logging`; a logging filter scrubs anything that slips through.
