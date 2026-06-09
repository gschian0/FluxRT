# Tomorrow Starter Kit Plan: Self-Hosted Relay + AI Audio

## Objective
Be ready tomorrow to start implementation work for:
- Self-hosted multistream relay (YouTube + Twitch)
- TTS voice generation with personality presets
- Music generation with Magenta RT
- Stable operations that do not break current FluxRT baseline behavior

This is a planning-only document. No runtime behavior changes are required in this stage.

## Decision Summary
1. Relay: use SRS (Simple Realtime Server) in Docker as the primary self-hosted relay.
2. Legacy fallback: NGINX-RTMP only if SRS cannot be used.
3. Rust relay alternatives are not selected for tomorrow because they are less mature for RTMP fanout operations compared to SRS.

## Why SRS
- Better fit for modern live workflows than basic NGINX-RTMP fanout
- Straightforward RTMP publish + multi-destination forward pattern
- Good operational ergonomics for staged rollout and health checks

## Non-Goals For Tomorrow
- No full autonomous AI host persona logic
- No final visual scene choreography
- No production launch to public audiences

## Target Architecture
1. Video pipeline
- FluxRT stream app remains primary video source
- FFmpeg encoder publishes a single RTMP stream to local SRS ingest

2. Relay pipeline
- SRS receives one local RTMP ingest
- SRS forwards to YouTube and Twitch

3. Audio pipeline
- TTS worker generates speech audio from event text
- Music worker (Magenta RT) generates background music segments
- Mixer applies ducking and limiter, then outputs final stereo mix

4. Packaging
- Encoder/mux sends video + mixed audio to SRS ingest endpoint

## Phased Plan

### Phase 0: Baseline Freeze
Goal:
- Preserve current known-good stream behavior before adding new services

Tasks:
- Record current launch commands and health checks
- Record current expected ports and logs
- Capture baseline quality notes (latency, frame stability)

Exit criteria:
- Baseline can be started and validated repeatedly

### Phase 1: Relay Foundation (SRS only)
Goal:
- Run SRS and prove fanout architecture with synthetic source

Tasks:
- Define SRS container deployment approach
- Define stream key secret handling policy
- Define dual output targets (YouTube, Twitch)
- Define minimum health checks for relay status

Exit criteria:
- Relay design validated on paper and ready for first implementation

### Phase 2: Encoder and Publish Contract
Goal:
- Define one encoder profile that both platforms accept

Tasks:
- Use H.264 + AAC profile defaults (CBR, 2s keyframe)
- Start with 720p30 safe profile
- Define fallback bitrate profile for unstable conditions

Exit criteria:
- One agreed profile and one fallback profile documented

### Phase 3: TTS Personality Contract
Goal:
- Define a model-agnostic interface for TTS personalities

Tasks:
- Define personality schema fields:
  - name
  - speaking_rate
  - pitch_shift
  - energy
  - tone
  - pause_style
- Define 3 starter presets:
  - Neutral Host
  - Hype Caster
  - Story Narrator
- Define text queue policy and interruption behavior

Exit criteria:
- Personality config format and queue behavior finalized

### Phase 4: Magenta RT Music Contract
Goal:
- Define generation control surface and runtime behavior

Tasks:
- Define music schema fields:
  - mood
  - bpm
  - key
  - intensity
  - segment_seconds
  - crossfade_ms
- Define continuity policy for transitions
- Define fallback behavior when generation fails

Exit criteria:
- Music generation and fallback behaviors finalized

### Phase 5: Mixer Rules
Goal:
- Keep speech intelligible over generated music

Tasks:
- Define ducking policy (speech over music)
- Define gain and limiter strategy
- Define clipping/silence detection rules

Exit criteria:
- Mix policy documented with operator-adjustable defaults

### Phase 6: Reliability and Operations
Goal:
- Define restart and monitoring behavior

Tasks:
- Define watchdog restart policy
- Define required health signals:
  - relay alive
  - publish process alive
  - frame freshness
  - audio activity
- Define incident triage checklist

Exit criteria:
- Operations checklist ready for implementation day

## Tomorrow Workplan (Execution Checklist)
1. Morning: lock interfaces and config schema
- Finalize relay, TTS, music, and mixer contracts
- Finalize env var naming and secret placeholders

2. Midday: draft implementation skeleton (no risky integration)
- Prepare service boundaries and launch order
- Prepare test matrix for first smoke runs

3. Afternoon: controlled first implementation
- Deploy SRS
- Validate local ingest and dual fanout with synthetic media
- Keep FluxRT baseline untouched while validating relay

4. End of day: go/no-go gate
- Confirm readiness for AI fortune teller integration in the next session
- Record blockers and next exact commands

## Configuration Blueprint (Planning)

### Relay settings
- relay.enabled
- relay.provider (srs)
- relay.listen_rtmp_port
- relay.ingest_app
- relay.ingest_stream
- relay.forward.youtube_enabled
- relay.forward.twitch_enabled

### Encoder settings
- encoder.video_codec
- encoder.audio_codec
- encoder.fps
- encoder.width
- encoder.height
- encoder.video_bitrate_kbps
- encoder.audio_bitrate_kbps
- encoder.gop_seconds

### TTS settings
- tts.enabled
- tts.provider
- tts.model
- tts.personality_preset
- tts.max_latency_ms

### Music settings
- music.enabled
- music.provider (magenta_rt)
- music.model
- music.mood
- music.segment_seconds
- music.crossfade_ms

### Mixer settings
- mixer.sample_rate
- mixer.channels
- mixer.duck_db
- mixer.attack_ms
- mixer.release_ms
- mixer.limiter_ceiling_db

## Risks and Mitigations
1. GPU contention with video inference
- Mitigation: keep audio generation on CPU first pass, then tune later

2. Stream instability from adding too many services at once
- Mitigation: enable one subsystem at a time with rollback after each stage

3. Key leakage risk
- Mitigation: keep stream keys in environment or secret manager, never in files

4. Audio quality instability under live generation
- Mitigation: keep static fallback music bed and neutral fallback voice

## Definition of Ready (for tomorrow)
Ready means all of the following are true:
1. Relay choice finalized (SRS).
2. Platform targets and secret strategy documented.
3. Encoder defaults and fallback profile documented.
4. TTS personality schema documented.
5. Magenta RT control schema documented.
6. Reliability checklist documented.

## Definition of Done (for this planning stage)
Done means:
1. Team has one agreed architecture and staged rollout path.
2. Tomorrow implementation order is explicit.
3. Risk and rollback approach is written and reviewed.
