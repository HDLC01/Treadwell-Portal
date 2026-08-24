# I am Treadwell Customer-Portal Agent

This repository is the standalone **customer-facing** half of Treadwell's proposal
experience. If a session begins here, confirm with: **"I am Treadwell Customer-Portal Agent"**.

## Purpose and boundary

After an estimator publishes a proposal from the separate proposal-tool application, this
portal sends a secure customer link. A customer authenticates by email OTP (or optionally
Google), views the proposal and PDF, asks questions, approves, and may record a deposit.
Staff monitor the pipeline and reply from the proposal-tool staff UI.

This is intentionally not merged with `../treadwell-proposal-tool`:

- The proposal tool is a staff-only `@wetreadwell.com` application.
- This portal serves external customers and needs a separate browser origin, session model,
  container, and attack surface.
- The staff portal screens (`portal.html` and `frontend/js/portal.js`) remain in the
  proposal-tool repository. This repo owns the customer site and portal API.

## Current deployment context — verified from source on 2026-07-22

- Live: `https://portal.wetreadwell.com`.
- Production: `/opt/treadwell-portal`, container `treadwell-portal`, loopback port
  `8898`, `docker-compose.prod.yml`; it connects as the least-privilege `portal_app`
  role to the shared Supabase Postgres session pooler.
- Staging: `https://staging.portal.wetreadwell.com`, container
  `treadwell-portal-staging`, port `8899`, staging compose/database, and visible OTP
  codes (`PORTAL_SHOW_OTP=true`).
- Production has been verified for health, database access, service-token enforcement,
  publication/pipeline, real Resend email delivery, customer rendering, and PDF retrieval.
  Google customer sign-in remains optional and off until a dedicated external OAuth Web
  client is configured.

## Data and service contract with the proposal tool

- The proposal tool owns `drafts` and `events`; the portal owns `portal_*` tables. Both
  use the same production Postgres database. The portal reads `drafts` directly, so its
  schema and the `data` JSONB shape are compatibility boundaries.
- Staff actions are server-to-server calls: proposal-tool `/api/portal/*` proxies to this
  app's `SERVICE_TOKEN`-protected `/api/admin/*` endpoints.
- For the branded PDF, this portal calls proposal-tool `/api/admin/proposal-pdf` through
  the internal Docker network.
- Never change the shared DB contract, service headers, or callback URLs without updating
  both projects and their focused tests.

## Authentication, email, and safety model

- Customer identity is email-scoped. `customer_auth.py` issues hashed, expiring, attempt-
  capped OTPs and HttpOnly sessions; `/p/<token>` is only a convenience deep-link, never
  the authorization gate.
- Optional Google sign-in must use a dedicated external OAuth client. Never reuse the
  proposal-tool's domain-restricted staff Supabase login.
- `ratelimit.py` protects sensitive public endpoints. Keep it in front of OTP, proposal,
  approval, and message flows.
- `email_sender.py` uses Resend for OTP, portal links, replies, and team notifications.
  Without `RESEND_API_KEY`, development logs instead of sending.
- The portal supports customer questions, approval, check/ACH deposit paths, contacts,
  notification-recipient overrides, and Resend inbound replies. Treat every customer
  action as auditable business state, not a mere UI update.

## Code map

| Location | Responsibility |
|---|---|
| `backend/main.py` | FastAPI entry point, customer APIs, service-gated staff APIs, static serving. |
| `backend/proposals.py` | Shared-draft lookup and portal proposal view/model work. |
| `backend/customer_auth.py` | OTP issuance/verification and customer session checks. |
| `backend/email_sender.py` | Resend mail composition/delivery. |
| `backend/inbound.py` | Resend inbound webhook handling. |
| `backend/automations.py` | Pipeline/deposit/notification business rules. |
| `backend/schema.sql`, `security_prod.sql` | Portal schema and production least-privilege grants. |
| `frontend/` | Static customer site: `index.html`, auth helpers, app JS, and styles. |
| `backend/tests/` | Focused pytest coverage for auth, ratelimiting, publication, messages, deposit flows, PDF cache, and notification rules. |

## Local development

The root compose files describe the local, staging, and production topologies. Copy
`.env.example` to a private `.env` and use local credentials only. Key settings are
`DATABASE_URL`, `PUBLIC_BASE_URL`, `SERVICE_TOKEN`, `PROPOSAL_TOOL_URL`, Resend sender/
recipient configuration, optional customer Google client ID, and optional Dropbox
credentials. Never commit a populated `.env`.

Run the API from `backend/` with the environment configured, then exercise the public
customer flow and the service-token staff endpoints separately. Run the focused pytest
suite before shipping:

```powershell
cd backend
python -m pytest tests/ -q
```

## Deployment rules

- Deploy with the repository's off-box scripts (`deploy/ship.sh` for staging,
  `deploy/ship-prod.sh` for production); the VPS is too small for ad-hoc rebuilds.
- Use staging first for code and migrations. Back up and verify before destructive schema
  work, especially because production data is shared with the proposal tool.
- Runtime production secrets exist only in `/opt/treadwell-portal/.env` with restrictive
  permissions. `.dockerignore` must continue to exclude `.env` from images.
- Do not push, deploy, rotate credentials, or enable customer Google sign-in without
  Hanz's explicit approval.
