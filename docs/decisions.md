# Architecture decisions

Short, dated notes. Newest last. Format: context → decision → consequence.

## 2026-08-11 — Async SQLAlchemy 2.0 + asyncpg

FastAPI is async end to end and the conversation engine will spend most of its time
waiting on the Anthropic API and WhatsApp. Mixing a sync ORM in would force
threadpool hops on every request.

**Decision:** SQLAlchemy 2.0 async (`asyncpg` driver), `Mapped[...]`/`mapped_column`
typing style throughout, one session per request via a FastAPI dependency that
commits on clean exit and rolls back on exception.

**Consequence:** Alembic's `env.py` runs an async engine. Background workers (M5)
must create their own sessions rather than reusing the request-scoped dependency.

## 2026-08-11 — UUID primary keys

Lead and conversation ids show up in webhook payloads, dashboard URLs and support
tickets. Sequential integers leak volume and invite enumeration.

**Decision:** `sa.Uuid` (UUIDv4) primary keys on every table, generated client-side
so we know the id before flushing.

**Consequence:** Slightly larger indexes. Acceptable at this scale.

## 2026-08-11 — Enums stored as VARCHAR + CHECK, not native Postgres enums

Lead status, channels and property types will grow (more languages, more channels,
`handed_off` variants). `ALTER TYPE ... ADD VALUE` cannot run inside a transaction
in Postgres, which makes those migrations awkward.

**Decision:** `sa.Enum(..., native_enum=False)` via `app.models.types.enum_column`.
Python-side they remain `StrEnum`s, so type checking still catches typos.

**Consequence:** Adding a value is a CHECK-constraint change. It also keeps the
schema dialect-neutral, which is what lets the test suite run on SQLite.

## 2026-08-11 — Tests run on in-memory SQLite; migrations verified separately

Requiring a running Postgres to run `pytest` slows the inner loop and breaks a
fresh clone. Every column type used is portable (JSON, Uuid, Numeric, VARCHAR enums).

**Decision:** Model and API tests use `sqlite+aiosqlite:///:memory:`.
`tests/test_migrations.py` applies the migrations and diffs the resulting schema
against `Base.metadata` — against Postgres when one is reachable on
`TEST_POSTGRES_URL` (or the compose default), otherwise a temp SQLite file.

**Consequence:** Postgres-only features (JSONB operators, `ARRAY`, full-text search,
partial indexes) must not be used in models without revisiting this. If we need
them, the drift test still covers us — but only when run against Postgres, so CI
must start the compose Postgres service.

## 2026-08-11 — PII masking in the logging layer, not at call sites

The compliance rule ("phone numbers and names never appear in logs") fails the
moment one developer forgets. Relying on discipline alone is not a control.

**Decision:** `app.core.logging.PIIMaskingFilter` scrubs phone/email patterns from
every log record as a backstop, and `mask_phone`/`mask_email`/`mask_name` produce
readable masked values at call sites. Model `__repr__`s never include name, phone,
email or message content.

**Consequence:** A small regex cost per log record. The filter is a safety net —
call sites should still mask deliberately so the output stays useful.

## 2026-08-11 — Message idempotency key on `(channel, external_id)`

WhatsApp Cloud API retries webhook deliveries; duplicate processing would mean
double-replying to a lead.

**Decision:** Unique constraint on `(channel, external_id)` in `messages`.
Ingestion (M3) inserts and treats a unique violation as "already handled".

**Consequence:** Locally generated outbound messages have a NULL `external_id`,
which the constraint permits any number of (NULLs are distinct in SQL) — the
adapter backfills the provider id after a successful send.

## 2026-08-11 — M2: Claude Opus 5, low effort, no streaming

The conversation is short-turn and latency-sensitive (a WhatsApp reply should
land in seconds), but qualification still needs judgement.

**Decision:** `claude-opus-5` with `output_config.effort = "low"` and adaptive
thinking (the model's default). `max_tokens` 1024 — replies are two or three
sentences, so no streaming is needed. Both model and effort are settings, not
constants.

**Consequence:** Raising effort is the first lever if qualification quality
disappoints; it costs latency and tokens. The system prompt carries a
`cache_control` breakpoint, so the stable per-agent prefix is cached across a
conversation's turns.

## 2026-08-11 — Manual tool loop, not the SDK tool runner

Tool handlers need a per-turn context (DB session, the Lead and Agent rows, a
place to record escalation and booking side effects), and every test must run
without an API key.

**Decision:** A manual `while stop_reason == "tool_use"` loop in
`ConversationEngine`, against an `LLMClient` protocol. `AnthropicLLM` implements
it for real; `tests/fakes.py` scripts turns deterministically.

**Consequence:** We own the loop, including its iteration cap. Every engine test
— tool dispatch, escalation, refusals, provider outages — runs offline in
milliseconds. The cost is that SDK tool-runner features (automatic compaction,
per-turn hooks) would have to be reimplemented if we ever want them.

## 2026-08-11 — Compliance rules live in code, not the prompt

Opt-out handling and high-value escalation are obligations under DPDP/TRAI and
the brokerage's own commercial policy. A model instruction is a strong default,
not a control: it can be talked around, and it changes behaviour when the prompt
or model changes.

**Decision:** `is_opt_out()` runs before the model is called at all — an opt-out
message never reaches the API. A budget above the agent's threshold escalates
before the turn runs, whatever the model then decides. The prompt still describes
both, so the model's behaviour agrees with the enforcement.

**Consequence:** Opt-out detection is a keyword matcher and will miss creative
phrasings; the model can still call `escalate_to_human` for those. Single-word
triggers ("stop") only match standing alone — "stop by the site on Sunday" is not
an opt-out, and a false positive would silently kill a live lead.

## 2026-08-11 — `update_lead_profile` added to the tool set

CLAUDE.md names five tools. The engine also needs to persist what it learns as
the conversation goes, rather than only at scoring time — a lead who gives their
budget and then goes quiet should still have that budget on record.

**Decision:** A sixth tool, `update_lead_profile`, writes qualification fields.
`score_lead` stays a pure read-and-score over what has been stored.

**Consequence:** Two round trips where one might do, in exchange for state that
survives an abandoned conversation and a clean split between "what we know" and
"what it's worth".

## 2026-08-11 — Timestamps are generated client-side

`server_default=func.now()` is transaction-scoped on **both** Postgres and
SQLite: every row written in one transaction gets an identical `created_at`.
Message ordering in a transcript then falls back to the UUID primary key, which
is random — so a conversation could render out of order.

**Decision:** `TimestampMixin` sets a Python-side `default=utcnow` as well. The
`server_default` stays for rows inserted outside the ORM.

**Consequence:** Timestamps come from app servers rather than the database, so
material clock skew across hosts would reorder messages. Acceptable at this
scale; revisit if the transcript ever needs a strict total order (a per-
conversation sequence number is the fix).

## 2026-08-11 — `UtcDateTime` column type

Postgres returns timezone-aware datetimes; SQLite drops the offset and returns
naive ones. Comparing them raises `TypeError`, so code that passed against
SQLite could fail against Postgres and vice versa.

**Decision:** A `TypeDecorator` on every timestamp column that returns UTC-aware
datetimes on read and **rejects naive datetimes on write**.

**Consequence:** Writing a naive datetime is now a loud error at the boundary
rather than a silent wrong-by-hours bug. The DDL is unchanged
(`TIMESTAMP WITH TIME ZONE`), and the migration drift test confirms Alembic sees
no difference.

## 2026-08-11 — Only lead-visible text is persisted

A turn can involve several tool calls. Persisting those rounds would make the
`messages` table an API transcript rather than a conversation.

**Decision:** `messages` holds only what the lead sent and what we sent back.
Tool calls are re-derived per turn; durable state lives on the `Lead` row.

**Consequence:** The dashboard transcript (M6) reads like a chat. The trade-off
is that we cannot replay exactly which tool calls produced a given reply —
`Message.meta` is reserved for attaching that later if debugging needs it.

## 2026-08-11 — M3: acknowledge the webhook first, process in the background

Meta re-delivers any webhook it does not see acknowledged within a few seconds.
A model turn takes longer than that, so processing inline would guarantee
duplicate deliveries — and, without deduplication, duplicate replies to the lead.

**Decision:** The webhook does the fast, safe part synchronously (verify
signature, resolve the agent, record the message, commit) and returns 200. The
model turn and the reply run in a FastAPI background task with its own session.

**Consequence:** Background tasks are in-process — a crash or redeploy between
the acknowledgement and the reply drops that turn silently, with no retry. This
is the main known weakness of M3. Redis is already in the stack for M5's
follow-up worker; the same worker should take over webhook processing then, at
which point the failure becomes a retryable queued job.

## 2026-08-11 — `(channel, external_id)` is the deduplication point

Meta guarantees at-least-once delivery. Deduplicating after the model turn would
still burn a model call per duplicate; deduplicating on the reply would be too late.

**Decision:** The inbound Message row is inserted before the turn is scheduled,
and the existing unique constraint on `(channel, external_id)` makes that insert
the claim. A cheap existence check handles the common case; the constraint
handles two deliveries racing each other, with `IntegrityError` treated as "someone
else already has this one".

**Consequence:** Deduplication costs one insert and is correct under concurrency.
It relies on Meta's `wamid` being stable across retries, which it is.

## 2026-08-11 — An unroutable delivery is acknowledged, not failed

A delivery for a `phone_number_id` with no matching agent is a configuration
problem. Returning 5xx would make Meta redeliver the whole batch indefinitely,
including the messages in it that we handled fine.

**Decision:** Log a warning and return 200 with `accepted: 0`. Only genuine
authentication failures (bad signature) and malformed JSON return an error status.

**Consequence:** A misconfigured number drops messages silently apart from the
log line. Worth an alert on that log once there is monitoring.

## 2026-08-11 — The 24h window is measured from the lead's message

WhatsApp only allows free-form replies within 24h of the lead's last message;
outside it, an approved template is required.

**Decision:** `Lead.last_inbound_at` is set from the message's own timestamp, not
from when we processed it, and `deliver()` refuses to send a free-form reply
outside the window.

**Consequence:** A turn delayed past the window is marked failed rather than
being rejected by Meta. Sending a template instead is M5's job, once approved
templates exist. Using the processing time here — which is what the code did
first — would have let a delayed turn attempt a send Meta would reject.

## 2026-08-11 — Consent is recorded when the message is claimed

A lead messaging us is the opt-in. Recording it during the model turn made a
legal fact contingent on our model call succeeding.

**Decision:** `claim_inbound` sets `consent_status` to `opted_in` for a lead
whose status is `unknown`. An existing `opted_out` is never overwritten.

**Consequence:** The consent record is accurate even when the engine is down.

## 2026-08-11 — Media becomes a described placeholder

Leads send floor-plan photos and voice notes. The model cannot see them, and an
empty turn would make it reply as if nothing had arrived.

**Decision:** `parse_webhook` turns each non-text type into a short description
("[the lead sent an image]"), keeps any caption, and stores the media ids on the
Message. `WhatsAppChannel.download_media` can fetch the bytes when something
needs them.

**Consequence:** The assistant can acknowledge what arrived and ask for what it
needs instead of ignoring it. Actually interpreting images (vision on floor
plans, transcribing voice notes) is a later, deliberate step — see the backlog.

## 2026-08-11 — Tests inject Settings rather than setting env vars

Two webhook tests asserted behaviour when a secret is missing, and passed only
because the developer's `.env` had no value for it. Filling in `.env` locally
broke them — pydantic reads the dotenv file regardless of `monkeypatch.delenv`.

**Decision:** A `client_factory` fixture builds an app with `get_settings`
overridden by an explicit `Settings` instance. No API test depends on ambient
environment.

**Consequence:** Tests behave identically on a fresh clone and a fully configured
machine. Anything reading settings outside a request (background tasks) still
uses the process environment, and should be given its settings explicitly if it
ever needs testing this way.

## 2026-08-11 — M4: Google Calendar over REST, not the official client

`google-api-python-client` is synchronous and large, and everything M4 needs is
four endpoints (token, freeBusy, events.insert, events.delete).

**Decision:** Implement against the REST API with the httpx client already in the
stack. No new dependency, no threadpool hops in an async codebase, and the OAuth
flow stays explicit — which matters because per-agent refresh tokens are the
sensitive part of this feature.

**Consequence:** We own request shapes and error mapping, including that Google
reports errors two different ways (`{"error": "invalid_grant"}` from the token
endpoint, `{"error": {"message": ...}}` from the Calendar API). Both are handled
and tested. If we later need many more endpoints, revisit.

## 2026-08-11 — Book first, sync second, escalate on failure

If the calendar write fails, either the lead is told a booking exists that the
agent cannot see, or a lead who was ready to commit is turned away over a
transient Google error. Both are bad; they are not equally bad.

**Decision:** Write the appointment, then attempt the calendar event. On failure
keep the booking, leave `google_event_id` null, and escalate to the human agent
with the reason. The model is told to confirm the time to the lead and *not* to
mention the calendar problem.

**Consequence:** The lead's experience is unaffected by a Google outage, and a
human always learns when the calendar is out of step. The gap is that nothing
retries the sync automatically — the agent has to add it manually. A reconciler
job is in the backlog.

## 2026-08-11 — A calendar outage degrades rather than blocks

Free/busy is an input to slot generation, not a precondition for it.

**Decision:** A failed free/busy lookup logs and falls back to our own
appointments instead of raising, so the agent keeps taking bookings.

**Consequence:** During a Google outage we may offer a slot the agent has
privately blocked — a double-booking they can decline. That is strictly better
than being unable to book at all for the duration of the outage.

## 2026-08-11 — OAuth `state` is signed, not stored

The callback needs to know which agent is connecting. Taking that from an
unsigned query parameter would let anyone attach *their* Google calendar to
*someone else's* agent record by crafting a callback URL.

**Decision:** `state` is an HMAC-signed, timestamped payload carrying the agent
id, verified with a constant-time comparison and a 10-minute TTL. Stateless, so
no server-side session store is needed.

**Consequence:** `OAUTH_STATE_SECRET` must be set for the flow to run at all —
the endpoints return 503 rather than falling back to something unsafe. Rotating
it invalidates in-flight connect attempts, which is acceptable for a flow that
lasts seconds.

## 2026-08-11 — `access_type=offline` and `prompt=consent` are both required

Without both, Google returns an access token but no refresh token on a
re-authorisation, and the connection silently stops working an hour later.

**Decision:** Always request both, and treat a token exchange that returns no
refresh token as an error the agent must act on rather than storing a
half-working connection.

**Consequence:** Agents are re-prompted for consent on every reconnect. That is
the intended trade — a visible extra click beats a connection that dies
overnight for reasons nobody can diagnose.
