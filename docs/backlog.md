# Backlog

Ideas deliberately out of scope for v1. Captured so we stop thinking about them.

## Out of scope per CLAUDE.md
- Portal scraping / auto-login (99acres, MagicBricks, Housing.com)
- Payments and multi-tenant billing
- ML-based lead scoring (v1 is rule-based and explainable)
- Voice calls
- iOS/Android apps

## Noticed while building M1
- **Brokerage as a first-class table.** `Agent.brokerage_name` is a string today. A
  2–20 person brokerage will eventually want shared listings, round-robin lead
  assignment and a team view — that needs a `Brokerage` table and `agent.brokerage_id`.
- **Lead deduplication across agents in one brokerage.** The unique key is
  `(agent_id, phone)`, so the same buyer enquiring on two listings creates two leads.
  Correct for independent agents, wrong once brokerages arrive.
- **Message content encryption at rest.** Transcripts are PII under the DPDP Act.
  Column-level encryption or a managed encrypted volume before we hold real data.
- **Soft deletes / retention policy.** DPDP gives data principals erasure rights;
  we need a documented retention window and a purge job.
- **Outbox pattern for outbound messages.** Right now a send failure after a DB
  commit could lose a message. A transactional outbox + worker would make delivery
  at-least-once.
- **Rate limiting per agent.** Redis is in the stack for this; nothing uses it yet.
- **Timezone per lead.** `Lead.timezone` exists but nothing populates it; quiet
  hours currently fall back to the agent's timezone.

## Noticed while building M2
- **Opt-out coverage is keyword-based.** Misses paraphrases ("please don't send
  me anything else"). The model can still escalate those, but a periodic review of
  transcripts for missed opt-outs would be worth building before scale.
- **No per-agent prompt customisation yet.** `Agent.tone_instructions` exists and
  is unused; M7's onboarding flow should feed it into the system prompt.
- **Language is set once per lead** and never re-detected. A lead who opens in
  English and switches to Gujarati keeps the English instruction — the model is
  told to follow their lead, but `Lead.language` goes stale and the opt-out
  confirmation would come back in the wrong language.
- **Scoring ignores location fit.** A lead whose preferred locality has no
  inventory scores the same as one whose does, as long as the budget matches.
- **Tool-call replay is not recorded.** `Message.meta` is empty; attaching the
  tool calls behind each reply would make dashboard debugging much easier.
- **No cost tracking.** Token spend per conversation is not recorded anywhere,
  so there is no way to see what a qualified lead costs.

## Noticed while building M3
- **Background tasks are not durable.** A crash between the webhook ack and the
  reply loses that turn with no retry. M5's Redis worker should take over webhook
  processing, not just follow-ups. This is the largest known gap in M3.
- **Media is described, never read.** Floor plans, property photos and voice
  notes arrive as placeholders. Vision on images and transcription for voice
  notes are both plausible and would want a deliberate cost/latency decision.
- **No inbound rate limiting.** A lead (or a bad actor) can drive one model call
  per message. Redis is in the stack; per-lead throttling belongs there.
- **Read receipts are never sent.** `WhatsAppChannel.mark_read` exists and is
  tested but nothing calls it — worth wiring in so leads see their message landed.
- **Replies are not split.** A long model reply goes out as one WhatsApp message;
  chunking at sentence boundaries would read more naturally.
- **No alerting on dropped deliveries.** An unmapped `phone_number_id` only
  produces a log line, which nothing watches.

## Noticed while building M4
- **No calendar-sync reconciler.** When a calendar write fails the booking is
  kept and a human is alerted, but nothing retries. A periodic job that finds
  appointments with a null `google_event_id` and re-syncs them would close this.
- **Refresh tokens are stored in plain text.** `Agent.google_refresh_token` grants
  ongoing calendar access. Column-level encryption, or a secrets manager, before
  this holds real agents' credentials.
- **No reschedule or cancel from the conversation.** "Can we move it to Friday?"
  currently has to escalate. The service layer already has `cancel_calendar_event`;
  a `reschedule_appointment` tool would use it.
- **Access tokens are cached per process.** Each worker refreshes independently.
  Harmless now; Redis-backed caching would cut refresh calls at scale.
- **Only the primary calendar is used.** Agents who keep viewings on a separate
  calendar cannot choose it — `Agent.google_calendar_id` is set but never exposed.
- **No reminder before the appointment.** Google's popup reminders fire for the
  agent; the lead gets nothing. A WhatsApp reminder the day before is an obvious
  no-show reducer and fits naturally into M5's worker.

## Noticed while building M5
- **Webhook processing still uses in-process background tasks.** M5 was expected
  to fix this, but the follow-up queue turned out not to need Redis, so nothing
  durable was built for the webhook path. It remains the largest reliability gap:
  a crash between the webhook ack and the reply loses that turn.
- **Templates must be approved in WhatsApp Manager before any of this sends.**
  The copy in `app/channels/templates.py` is written and tested but has never
  been submitted. Until it is approved, every follow-up will be rejected by Meta.
  There is no check that the local copy still matches the approved version.
- **No appointment reminders.** The worker is the natural home for a "your visit
  is tomorrow at 11" message, which is the cheapest no-show reduction available.
- **Failed nudges are not retried.** A task that fails stays failed; the next
  cadence step still fires. A bounded retry would be better than waiting days.
- **No per-agent nudge rate limit.** A brokerage importing 500 stale leads would
  send 500 templates in one pass. Redis is in the stack for exactly this.
- **Follow-up effectiveness is not measured.** Nothing records which attempt
  number actually produced a reply, so the cadence cannot be tuned with evidence.

## Noticed while building M6
- **No token expiry or rotation UI.** `make token` is the only way to issue or
  revoke. M7's accounts should bring sessions with expiry.
- **The dashboard token sits in localStorage**, readable by any script on the
  origin. Fine today; an httpOnly cookie + CSRF is the better end state.
- **No realtime updates.** The pipeline is fetched on load; a new WhatsApp message
  does not appear until the agent refreshes. Polling or SSE would fix it.
- **No pagination controls in the UI.** The API paginates, the dashboard always
  requests the first 50.
- **No listings or appointment management screens.** The dashboard reads the
  pipeline; editing inventory is still SQL. M7 covers listing import.
- **Search hits the database with ILIKE on every keystroke.** Fine at demo scale,
  wants debouncing and an index before real volume.
- **The lead detail page is not accessible-audited** — no keyboard-trap testing,
  no screen-reader pass, and colour is doing some work that text should.

## Noticed while building M7
- **No password reset.** An agent who forgets their password has no route back in
  without a DB edit. Needs an email sender, which the product does not have yet.
- **No email verification.** Signup accepts any well-formed address.
- **Email validation is a pragmatic regex**, not RFC-complete — deliberate, to
  avoid pulling in email-validator and dnspython. Revisit when we send email.
- **No rate limiting on login or signup.** Password guessing is bounded only by
  scrypt's cost. Redis is in the stack for exactly this.
- **The session token is still in localStorage** (from M6). Now that there is a
  real login, an httpOnly cookie plus CSRF is worth doing properly.
- **No "your sessions" screen.** `AgentSession.user_agent` is recorded but never
  shown; agents cannot see or revoke individual devices.
- **Listings can only be imported, not edited.** No add/edit form, no media
  upload; changing one price means re-uploading the CSV.
- **No listing de-duplication.** Importing the same file twice doubles the
  inventory unless `replace` is used.
- **`needs_rehash` is never called.** Passwords are not upgraded on login when
  parameters are raised — a small loop to add in the login path.
