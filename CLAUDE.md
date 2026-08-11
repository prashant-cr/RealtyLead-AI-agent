# CLAUDE.md — RealtyLead AI Agent

## What we're building

An AI-powered **lead qualification & follow-up agent** for real estate agents and small brokerages. When a property inquiry comes in (from a portal, website form, or WhatsApp), the agent:

1. Responds within seconds via WhatsApp/SMS/email
2. Runs a natural qualifying conversation (budget, preferred locations, property type, timeline, financing status)
3. Scores and tags the lead (hot / warm / cold)
4. Books qualified leads onto the human agent's calendar for a call or site visit
5. Nurtures unresponsive leads with polite, spaced follow-ups (day 1, 3, 7, 14 — then monthly)
6. Logs everything to a simple CRM/dashboard the human agent can review

**Primary market:** India first (WhatsApp-centric, multilingual — English, Hindi, Gujarati), but keep messaging channels pluggable so US/global channels (SMS via Twilio, email) can be added later.

## Target user

Individual real estate agents and 2–20 person brokerages. Non-technical users. They should be able to onboard in <10 minutes: connect WhatsApp, paste their listings or portal login, set working hours, done.

## Architecture (proposed — refine as we go)

```
Inbound lead (webhook / WhatsApp msg / portal email parser)
        │
        ▼
  Ingestion service ──► Lead store (Postgres)
        │
        ▼
  Conversation engine (Claude API)
   - system prompt per agent/brokerage (their listings, tone, languages)
   - tool use: check_availability, book_appointment, get_listing_details,
     score_lead, escalate_to_human
        │
        ▼
  Channel adapters (WhatsApp Business API first; Twilio SMS + email later)
        │
        ▼
  Dashboard (Next.js) — lead pipeline, conversation transcripts,
  lead scores, calendar, manual takeover button
```

## Tech stack

- **Backend:** Python 3.11+, FastAPI, Postgres (SQLAlchemy + Alembic), Redis for queues/rate limiting
- **LLM:** Anthropic API (Claude), tool use for booking/scoring, streaming where useful
- **Messaging:** WhatsApp Business Cloud API (Meta) as the first channel adapter
- **Calendar:** Google Calendar API (OAuth per agent)
- **Frontend:** Next.js 14 (App Router) + Tailwind, deployed separately
- **Infra:** Docker Compose for local dev; keep deploy target open (Railway/Fly/AWS)
- **Testing:** pytest for backend, Playwright for the dashboard's critical flows

## Project structure

```
/backend
  /app
    /api            # FastAPI routers (webhooks, dashboard API)
    /channels       # whatsapp.py, sms.py, email.py — one adapter interface
    /agent          # conversation engine, prompts, tools, lead scoring
    /models         # SQLAlchemy models
    /services       # calendar, crm, follow-up scheduler
    /workers        # background jobs (follow-ups, nightly digests)
  /tests
/frontend           # Next.js dashboard
/docs               # decisions, API contracts, prompt iterations
docker-compose.yml
```

## Core domain models (starting point)

- **Agent** — the human realtor: name, phone, languages, working hours, calendar creds, brokerage
- **Listing** — property: title, type (flat/villa/plot/commercial), location, price, BHK, status, media links
- **Lead** — contact info, source, status (new/engaged/qualified/booked/cold/handed_off), score, preferences (budget range, locations, BHK, timeline, financing)
- **Conversation** — messages with role, channel, timestamps; linked to Lead
- **Appointment** — lead + agent + listing + slot + status
- **FollowUpTask** — scheduled nudges with cadence state

## Conversation engine rules

- Always identify as an AI assistant working for [Agent/Brokerage name] — never pretend to be human.
- Detect and reply in the lead's language (English/Hindi/Gujarati to start).
- Ask ONE question at a time. Keep messages short and WhatsApp-natural.
- Qualify on: budget, location preference, property type & size, purchase timeline, loan pre-approval status, purpose (self-use vs investment).
- Escalate to the human agent immediately if the lead: asks for a human, discusses price negotiation, seems high-value (budget > configurable threshold), or expresses frustration.
- Never invent property details. Only state facts from the Listing records; say "let me check with [agent name]" otherwise.
- Respect quiet hours (default 9pm–9am lead's local time) for outbound messages.
- Hard cap on follow-ups; always honor opt-out ("stop", "not interested") instantly and mark the lead accordingly.

## Lead scoring (v1 — simple, explainable)

Rule-based first, ML later. Score 0–100 from: budget match with inventory, timeline (<3 months = hot), responsiveness, loan pre-approval, site-visit willingness. Thresholds: 70+ hot, 40–69 warm, <40 cold. Store the reasons, not just the number — the dashboard must show *why*.

## Build order (milestones)

1. **M1 — Skeleton:** FastAPI app, Postgres models + migrations, docker-compose, health check, basic tests running
2. **M2 — Conversation core:** Claude-powered conversation engine with tools, in-memory channel (CLI/test harness) so we can chat with it before any WhatsApp setup
3. **M3 — WhatsApp adapter:** Meta Cloud API webhook in/out, message dedup, media handling
4. **M4 — Booking:** Google Calendar integration, availability check, appointment creation + confirmations
5. **M5 — Follow-up worker:** scheduled nudges with cadence + opt-out handling
6. **M6 — Dashboard:** lead pipeline view, transcripts, scores, manual takeover
7. **M7 — Onboarding flow:** agent signup, listing import (CSV first), prompt/tone customization

Work milestone by milestone. Don't start M3 until M2's test harness works end-to-end.

## Conventions for Claude Code

- Write tests alongside features; run `pytest` before considering a task done
- Use type hints everywhere; run `ruff` and `mypy` — keep them clean
- Small, focused commits with clear messages
- Secrets only via `.env` (git-ignored); commit a `.env.example`
- Every external API call gets timeouts, retries with backoff, and error logging
- Store all prompts in `/backend/app/agent/prompts/` as versioned files — never inline long prompts in code
- When making an architectural decision, add a short note to `/docs/decisions.md`
- Ask before adding heavy dependencies

## Compliance & safety notes

- WhatsApp Business API requires template messages for business-initiated conversations after 24h — the follow-up worker must use approved templates
- Store consent/opt-in status per lead; India's DPDP Act and TRAI DND rules apply to outbound messaging
- PII (phone numbers, names) must never appear in logs — mask them
- RERA registration details of listings should be displayed where legally required

## Out of scope for v1

Portal scraping/auto-login, payments, ML-based scoring, voice calls, iOS/Android apps, multi-tenant billing. Note ideas in `/docs/backlog.md` and move on.
