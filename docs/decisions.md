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

## 2026-08-11 — M5: the queue is Postgres, not Redis

Redis is in the stack for queues, so it was the obvious choice. But the follow-up
schedule has to survive restarts, be queryable by the dashboard ("when is this
lead next being contacted?"), and be cancellable from three different places.
`follow_up_tasks` already models all of that.

**Decision:** The table is the queue. Due work is claimed with
`SELECT ... FOR UPDATE SKIP LOCKED` on Postgres, so multiple worker replicas can
run without double-sending. SQLite (tests) skips the clause and runs serially.

**Consequence:** One source of truth instead of two that can disagree, and the
schedule is inspectable with plain SQL. The cost is polling — a nudge fires
within one poll interval (60s) of being due, which is irrelevant for a cadence
measured in days. Redis stays unused for now; rate limiting and the webhook queue
are still good fits for it.

## 2026-08-11 — Follow-ups are templates, never model-generated

Outside WhatsApp's 24-hour window Meta only delivers pre-approved templates,
matched by name and language. A model-written nudge could not be delivered.

**Decision:** `app/channels/templates.py` holds the approved copy for six
attempts across English, Hindi and Gujarati, keyed by `(attempt, language)` with
an English fallback. The worker never calls the model.

**Consequence:** The worker is cheap, deterministic and testable, and follow-ups
cost nothing in tokens. The text here must stay byte-identical to what was
submitted in WhatsApp Manager — changing the copy means re-submitting for
approval, and a mismatch is a rejected send, not a silent difference.

## 2026-08-11 — Eligibility is checked twice, and re-checked at send time

A task can sit in the queue for days. Almost everything that makes a nudge
inappropriate — the lead replied, booked, opted out, a human took over —
happens in that gap.

**Decision:** `check_eligibility` runs when a task is scheduled *and* immediately
before it is sent. The send-time check is the one that matters; the schedule-time
check just avoids queuing work that is already pointless.

**Consequence:** A nudge cannot be sent to a lead whose situation changed after
scheduling, even if the cancellation path failed to run. Every skip records its
reason on the task, so the dashboard can explain why a lead stopped being chased.

## 2026-08-11 — `FollowUpTask.baseline_at`

Staleness was originally decided by comparing the lead's `last_inbound_at` to the
task's `created_at`. That ties a business rule to a database insert timestamp
that nothing else in the system reasons about, and it cannot be reasoned about
with an injected clock — which is how the whole test suite works.

**Decision:** A task records the lead activity it was scheduled from. Staleness
is `last_inbound_at > baseline_at`: "the lead wrote after we planned this nudge".

**Consequence:** One added nullable column (migration `6373a5f23d58`). The rule
now reads the way it is described, and the dashboard can show what a nudge was
scheduled against. Pre-existing rows have a null baseline and are simply never
treated as stale.

## 2026-08-11 — Quiet hours defer, terminal conditions cancel

A nudge that comes due at 3am is not a nudge that should be abandoned.

**Decision:** Quiet hours push `scheduled_for` to the next allowed moment and
leave the task `scheduled`. Opt-out, booking, handoff and the cap cancel it. The
window is applied twice — when scheduling, and again at send time in case the
worker was down overnight.

**Consequence:** A worker outage across a night results in delayed nudges rather
than nudges arriving at 4am when it catches up.

## 2026-08-11 — A failed send does not consume an attempt

`follow_up_count` is what enforces the hard cap.

**Decision:** It is incremented only after the provider accepts the message. A
rejected send marks the task `failed` and leaves the count alone.

**Consequence:** A template-approval problem or an outage cannot silently burn
through a lead's allowance. The flip side is that a permanently broken template
would retry at the next cadence step rather than stopping — the task status makes
that visible, and the cap still bounds it.

## 2026-08-11 — M6: the dashboard API is authenticated from day one

These endpoints return lead phone numbers, budgets and full transcripts. CLAUDE.md
puts agent signup in M7, but shipping the dashboard unauthenticated in the
meantime would publish every lead's contact details.

**Decision:** Bearer tokens per agent, issued out of band
(`make token`) and stored as a SHA-256 hash. Tokens are high-entropy random
strings rather than user-chosen passwords, so there is no dictionary to attack
and a fast hash is the right choice — bcrypt would buy nothing here.

**Consequence:** M7 replaces this with real accounts. Until then there is no
self-service signup, no rotation UI and no expiry; re-running `make token` is
how a token is both rotated and revoked.

## 2026-08-11 — Scoping is in the WHERE clause, not a post-hoc check

Multi-tenant leakage is the worst failure this product could have.

**Decision:** Every dashboard query filters on `Lead.agent_id == agent.id` as
part of the query itself. There is no code path that loads a lead and then
decides whether the caller may see it. Another agent's lead returns 404, not
403 — a 403 would confirm the record exists.

**Consequence:** Tested directly: an agent cannot list, read, take over or
transcript another agent's lead, and `/api/stats` counts only their own.

## 2026-08-11 — CORS, found by testing against the real backend

The Playwright suite stubs the API, which is right for testing the dashboard's
own behaviour — but it hid the fact that the browser could not reach the backend
at all. The dashboard is a separate origin, and FastAPI had no CORS middleware,
so every request failed preflight.

**Decision:** `CORSMiddleware` with an explicit `cors_allow_origins` list, no
wildcard, `allow_credentials=False` (we send a bearer token, not cookies) and
only the methods and headers actually used.

**Consequence:** Deploying the dashboard to a new origin needs that origin added
to `CORS_ALLOW_ORIGINS`. The lesson generalises: `e2e/live.spec.ts` runs the same
flows against the real stack and is what caught this — worth keeping green.

## 2026-08-11 — A lead can be taken over before it has a conversation

Found the same way. An agent may want to claim a lead imported from a portal
*before* it ever writes in, so the assistant never answers it.

**Decision:** Takeover no longer requires a conversation. More importantly, the
engine now treats `lead.status == HANDED_OFF` as "a human owns this" in addition
to the conversation flag — otherwise a handed-off lead whose conversation was
closed and recreated would quietly get the assistant back.

**Consequence:** Handoff is durable across conversation boundaries. The dashboard
mirrors the same rule, since `conversation_status` is null for an unstarted lead.

## 2026-08-11 — The token lives in localStorage

The dashboard is a separate origin using bearer auth, so a cookie would need
CORS credentials plus CSRF protection.

**Decision:** `localStorage`, read by the API client on each request.

**Consequence:** Any script running on the dashboard origin can read the token.
Acceptable while the dashboard loads no third-party scripts, and explicitly worth
revisiting in M7 alongside real accounts — an httpOnly cookie plus CSRF tokens is
the better end state.

## 2026-08-11 — M7: scrypt from the stdlib, not bcrypt or argon2

Passwords need a slow, memory-hard hash — the opposite of the fast SHA-256 used
for API tokens, which are random and have no dictionary to attack.

**Decision:** `hashlib.scrypt`. It is in CPython's stdlib (OpenSSL-backed), it is
designed for exactly this, and it avoids adding bcrypt or argon2-cffi. Parameters
are stored alongside each hash (`scrypt$n$r$p$salt$hash`) so they can be raised
later without invalidating existing passwords; `needs_rehash()` reports when.

**Consequence:** n=2^15 (~100ms, 32 MB per hash here). 2^16 measured at 210ms and
64 MB, which is a real denial-of-service surface on an unauthenticated login
endpoint where concurrent attempts multiply the memory. That trade is written
into the module so the next person raising it knows what to re-measure.

## 2026-08-11 — Sessions replace the single API token for the dashboard

M6 shipped one long-lived token per agent because there were no accounts. With
signup, a browser login should expire and be revocable per device.

**Decision:** An `agent_sessions` row per login — 14-day expiry, individually
revocable, `last_used_at` touched at most every 15 minutes. The M6 API token
stays for scripts and the CLI, where a login flow makes no sense. Both are
presented as `Authorization: Bearer …` and distinguished by prefix (`rls_` vs
`rl_`).

**Consequence:** Two credential paths to reason about, which is why they share
one `current_agent` dependency. Changing a password revokes every session —
the expected behaviour after "someone may have my password".

## 2026-08-11 — Auth responses never confirm whether an account exists

Signup, login and lead lookup can all be used to probe for existence.

**Decision:** A duplicate signup returns "That email address cannot be used"
rather than "already registered". An unknown email and a wrong password return
the identical 401 body, and the unknown-email path still performs a real scrypt
hash against a dummy value so the timing matches.

**Consequence:** Slightly less helpful errors for legitimate users who have
forgotten they already signed up. Password reset (not yet built) is the right
place to solve that, not the error message.

## 2026-08-11 — CSV import is all-or-nothing

An agent uploading a 40-row export will not notice that rows 12 and 31 were
dropped. The assistant would then quote from an incomplete catalogue, and the
first sign of trouble would be a lead asking about a property it never mentions.

**Decision:** Nothing is written unless every row parses. Failures come back with
line numbers and a specific reason, and the UI lists them.

**Consequence:** One bad cell blocks the whole file. That is the right way round:
the fix is obvious and immediate, whereas a silent partial import is not.

The parser is deliberately forgiving about *shape* while strict about *facts*:
portal exports name columns "Property Name" / "Cost" / "Bedrooms", and Indian
agents write prices as "85 lakh", "2.15 cr" or "85,00,000". All are accepted;
"about a crore maybe" is not.

## 2026-08-11 — Prompt v2 rather than editing v1

`Agent.tone_instructions` has existed since M1 and was never used — it was in the
backlog from M2. M7 wires it in, which changes the system prompt.

**Decision:** A new `qualification_system_v2.md` with a `$tone_instructions`
slot. v1 stays on disk and loadable.

**Consequence:** This is what the versioning was for: if v2 regresses, pinning
back to v1 is a one-line change rather than a git archaeology exercise. Both
versions are covered by tests.

## 2026-08-11 — Optional onboarding steps do not gate completion

An agent with working hours, listings and WhatsApp can take leads today. Google
Calendar and a custom tone make it better, not functional.

**Decision:** The checklist shows six steps; only four are required for
`complete`. `onboarded_at` is stamped the first time those four are done.

**Consequence:** The "under 10 minutes" promise is achievable without an agent
having to complete a Google OAuth flow before their first lead.

## 2026-08-11 — Inbound turns go through Redis Streams, not background tasks (M8)

The webhook acknowledged Meta and then ran the model turn in a FastAPI
`BackgroundTasks` callback. That is in-process: a crash, an OOM kill or a
redeploy between the 200 and the reply lost that turn, with no retry and no
record. Meta treats the 200 as final, so the lead simply never heard back. The
backlog flagged this as the largest known gap twice, after M3 and again after M5.

**Decision:** The webhook now only records the message and enqueues it. A
separate `inbound_worker` runs the turn and acknowledges the stream entry once
the reply has actually been sent.

Streams rather than a list: `LPUSH`/`BRPOP` removes the entry before it is
processed, so a worker dying mid-turn loses it — the same bug moved one process
along. A stream's per-group pending list makes an unacknowledged entry
recoverable. Streams rather than another Postgres table (which is how
`follow_up_tasks` works): a follow-up is due on a date and a minute of polling
delay is invisible, but a lead waiting on a reply notices seconds, and
`XREADGROUP BLOCK` wakes the instant work arrives.

**Consequence:** Retries need no machinery of their own. A failed turn is simply
not acknowledged; `reclaim_stale` picks it up once it has been idle long enough,
so the idle threshold *is* the backoff. Entries that exhaust four attempts move
to a dead-letter stream rather than vanishing.

The cost is a third process. Without the inbound worker running, messages are
received and never answered — a failure mode that did not exist before, and the
reason it is in `docker-compose.yml` rather than being optional.

## 2026-08-11 — A Redis outage degrades the webhook rather than failing it

If Redis is unreachable when a delivery arrives, we can either fail the request
or handle it the old way.

**Decision:** Fall back to the in-process path, and log it as an error.

**Consequence:** That path can lose a turn on a crash — but the alternative is
losing it immediately and with certainty, because a non-200 makes Meta redeliver
a few times and then give up. A worse guarantee beats no guarantee. The same
reasoning makes the rate limiters fail open and keeps Redis out of the readiness
gate: a Redis outage should not pull every API instance out of the load balancer.

## 2026-08-11 — Rate limits are fixed windows, and they fail open

Nothing bounded inbound model calls per lead, follow-up sends per agent, or
login attempts. Redis had been in the stack since M1 with no code using it.

**Decision:** Fixed-window counters — one `INCR` on the hot path. Not a token
bucket (needs Lua or a read-modify-write race) and not a sliding log (a sorted
set and a range delete per call).

**Consequence:** Burstiness at the window boundary: a subject can spend a full
window's budget at the end of one window and again at the start of the next.
Acceptable for "stop runaway spend", and would not be if these were billing
quotas. Keys are SHA-256 of the subject because subjects are phone numbers and
email addresses, and Redis keys appear in `MONITOR`, slow logs and exporters.

## 2026-08-11 — An undelivered reply is a retry, not a completed turn

Found by running the stack with a deliberately bad WhatsApp token: `deliver`
marked the outbound row FAILED, logged, and returned normally. The worker
reported `completed=1` and acknowledged the message. The lead had been qualified
and would never be answered — the exact loss M8 was built to prevent, one step
further along, and invisible to 404 passing tests.

**Decision:** `deliver` raises `DeliveryRejectedError` after marking the row. The
worker maps that to a retry.

**Consequence:** The whole turn is retried, not just the send, so a retry costs
another model call. That is deliberate: committing the reply and retrying only
the delivery would leave a half-finished turn in the transcript, and re-running
from a rolled-back state means one attempt produces exactly one assistant
message. Bounded by the four-attempt budget.
