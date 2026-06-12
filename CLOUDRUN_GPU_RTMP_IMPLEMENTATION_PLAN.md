# CloudRun + GPU Worker + RTMP Fanout Implementation Plan

## 1. Goal
Deliver click-to-start live streaming from a website with these capabilities:
- Control plane on Cloud Run
- GPU worker runtime on Compute Engine (or GKE GPU later)
- RTMP output fanout to Twitch, YouTube, and Facebook
- 10-second moderation checks for nudity/sexual/gross/toxic content
- Future-ready payment-gated prompt and stream-change requests

This plan preserves existing FluxRT behavior on port 7861 and adds new components in stages.

## 2. Architecture Decision
Cloud Run should host the website and orchestration API.
The long-running GPU inference and RTMP publishing should run on a GPU VM.

Reason:
- Cloud Run lifecycle and request model are not ideal for persistent GPU streaming pipelines.
- GPU VM gives stable inference, predictable process supervision, and RTMP reliability.

## 3. Target System

### 3.1 Control Plane (Cloud Run)
Services:
1. Web frontend service
- Start/stop stream buttons
- Status panel (booting/ready/streaming/error)

2. Orchestrator API service
- Calls Compute Engine API to start/stop GPU worker VM
- Calls worker control endpoints (start stream, stop stream, update config)
- Stores state and audit events

Data stores:
- Firestore: job state, moderation events, request history
- Secret Manager: stream keys, API keys

### 3.2 Worker Plane (GPU VM)
Processes:
1. FluxRT stream app on 7861 (existing)
2. Encoder/publisher process (ffmpeg)
3. SRS relay container for multi-platform fanout
4. Moderation worker (10-second checks)
5. Supervisor/watchdog

## 4. RTMP Fanout Design
1. Worker publishes one local RTMP stream to SRS:
- rtmp://127.0.0.1:1935/live/fluxrt

2. SRS forwards to:
- Twitch ingest URL + key
- YouTube ingest URL + key
- Facebook Live ingest URL + key

3. Keys are loaded from Secret Manager into runtime env.

4. Retry policy:
- Exponential backoff on per-destination failure
- Keep other destinations alive if one fails

## 5. Moderation Design (Every 10 seconds)

### 5.1 Inputs
- Video frame samples from current output stream
- Audio sample window
- Optional OCR text from frame
- Optional speech-to-text transcript from audio

### 5.2 Checks
1. Nudity/sexual content detection
2. Graphic/gross content detection
3. Toxic/harassment language checks on OCR + transcript

### 5.3 Policy Actions
1. Score below threshold:
- continue stream

2. Medium threshold hit:
- reduce risky prompt actions
- alert operator dashboard

3. High threshold hit:
- switch to safe slate
- mute or replace audio bed
- optionally pause outbound RTMP for all destinations
- log incident with evidence hashes and timestamps

### 5.4 Compliance Notes
- Keep a moderation audit trail for all enforcement actions.
- Clearly disclose AI-generated content in channel metadata.

## 6. Payment-Gated Change Requests (Future)
Only planning at this stage.

1. Add request queue:
- user submits prompt or stream-change request
- system returns quote and payment address/link

2. Payment confirmation:
- accept only confirmed payment states
- map payment to pending request ID

3. Safety gate before apply:
- run prompt policy checks
- reject disallowed requests with refund/manual review policy

4. Apply rules:
- approved request is applied via worker control endpoint
- changes are logged with user request ID and moderation snapshot

## 7. API Contracts (Draft)

### 7.1 Orchestrator API
1. POST /stream/start
- starts VM if needed
- waits for worker readiness
- starts publishing

2. POST /stream/stop
- stops publishing
- optional worker stop

3. GET /stream/status
- returns VM state, worker health, destinations health

4. POST /stream/config
- updates allowed runtime config (non-destructive)

5. POST /request/change
- creates payment-gated change request (future)

### 7.2 Worker Control API
1. GET /health
2. POST /publish/start
3. POST /publish/stop
4. GET /publish/status
5. POST /runtime/update

## 8. Security Model
1. Cloud Run authenticated with IAM (no anonymous for control API)
2. Worker API behind firewall; only orchestrator can call it
3. Secret Manager for stream keys and provider credentials
4. Least-privilege service accounts
5. Signed audit logs for start/stop/config/moderation actions

## 9. Rollout Stages

### Stage A: Control Plane MVP
- Deploy web + orchestrator Cloud Run services
- Start/stop GPU VM and report state

Exit:
- One-click VM boot/shutdown works reliably

### Stage B: Worker Parity
- Container starts current 7861 functionality unchanged
- Health checks and watchdog added

Exit:
- Existing stream demo behavior reproduced on worker

### Stage C: RTMP Fanout
- Deploy SRS and ffmpeg publish path
- Validate Twitch/YouTube/Facebook outputs with test scene

Exit:
- 60-minute stable stream to all enabled destinations

### Stage D: Moderation Enforcement
- Run 10-second checks and alert-only mode first
- Then enable automatic enforcement actions

Exit:
- Policy triggers and safe fallback behavior validated

### Stage E: Payment Queue (Feature Flag)
- Add request/payment flow behind disabled flag
- Enable only after policy and abuse checks are complete

Exit:
- End-to-end dry run from payment to approved runtime change

## 10. Observability
Required metrics:
1. worker_up
2. publish_up
3. destination_up{platform}
4. av_desync_ms
5. moderation_violation_count
6. moderation_action_count
7. restart_count
8. gpu_mem_mb

Required logs:
- request_id, stream_id, action, component, severity, latency_ms

Dashboards:
- operational health
- moderation activity
- destination delivery health

## 11. Immediate Next Actions
1. Create Cloud Run orchestrator service scaffold
2. Define VM startup script and health endpoint contract
3. Create SRS relay config template with three destination toggles
4. Create moderation service interface and threshold config
5. Add runbook section for operator flows and incident response

## 12. Acceptance Criteria
1. One-click website flow starts stream end-to-end
2. Existing 7861 functionality remains intact
3. Multi-destination RTMP is stable for 60+ minutes
4. 10-second moderation checks run continuously
5. High-risk detection forces safe fallback within one cycle
6. All actions auditable and replayable from logs
