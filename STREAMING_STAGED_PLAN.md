# Streaming Staged Plan (No-Risk Path)

This plan keeps the current FluxRT app unchanged and running as-is.

Principles:
- Do not edit `scripts/run_gradio_demo.py`.
- Streaming runs as a separate operation only.
- Every stage has a rollback command.
- Record pass/fail before advancing.

## Stage 0: Baseline Snapshot (Known Good)

Goal:
- Confirm existing local FluxRT app works before any streaming tests.

Commands:

```bash
cd /home/gschi/FluxRT
curl -I http://127.0.0.1:7860
pgrep -af run_gradio_demo.py || true
```

Pass criteria:
- `HTTP/1.1 200 OK` for `127.0.0.1:7860`.

Rollback:
- None needed. This is baseline.

Record:
- Date/time:
- Baseline URL check:
- Notes:

## Stage 1: Tooling Check (No App Changes)

Goal:
- Ensure ffmpeg exists for separate relay testing.

Commands:

```bash
ffmpeg -version | head -n 2
```

Pass criteria:
- ffmpeg version prints.

Rollback:
- None.

If missing:
- Install on host/container used for relay.

## Stage 2: Start Separate NBC Relay

Goal:
- Start HLS relay process without touching FluxRT app.

Command:

```bash
cd /home/gschi/FluxRT
chmod +x scripts/streaming/start_nbc_relay.sh scripts/streaming/stop_nbc_relay.sh
scripts/streaming/start_nbc_relay.sh
```

Default input/output:
- Input: `https://xumo-xumoent-vc-105-z0vpm.fast.nbcuni.com/live/master.m3u8`
- Output: `udp://127.0.0.1:5000?pkt_size=1316`
- Log: `/tmp/fluxrt-nbc-relay.log`
- PID file: `/tmp/fluxrt-nbc-relay.pid`

Pass criteria:
- Script reports `Relay started`.
- PID file exists.

Rollback:

```bash
scripts/streaming/stop_nbc_relay.sh
```

Record:
- Relay started at:
- PID:
- Log path:

## Stage 3: Validate Relay Health

Goal:
- Check relay is running stably.

Commands:

```bash
tail -n 80 /tmp/fluxrt-nbc-relay.log
ps -fp "$(cat /tmp/fluxrt-nbc-relay.pid)"
```

Pass criteria:
- ffmpeg process alive.
- No fatal reconnect loop spam.

Rollback:

```bash
scripts/streaming/stop_nbc_relay.sh
```

## Stage 4: Keep FluxRT Unchanged, Observe Only

Goal:
- Keep app unchanged while relay runs in parallel.

Commands:

```bash
curl -I http://127.0.0.1:7860
pgrep -af run_gradio_demo.py
pgrep -af ffmpeg
```

Pass criteria:
- FluxRT still returns `200 OK`.
- App and relay can coexist.

Rollback:
- Stop relay only:

```bash
scripts/streaming/stop_nbc_relay.sh
```

## Stage 4.5: Run Separate Stream Demo App (Side-by-Side)

Goal:
- Keep the known-good app on 7860 untouched.
- Run stream ingest tests in a separate Gradio app on 7861.

Commands (inside your container):

```bash
cd /workspace
chmod +x scripts/start_gradio_stream_demo.sh
scripts/start_gradio_stream_demo.sh
```

Open:

- Baseline app: `http://127.0.0.1:7860`
- Stream demo app: `http://127.0.0.1:7861`

Notes:

- The stream demo script is `scripts/run_gradio_stream_demo.py`.
- It has its own log at `/tmp/fluxrt-gradio-stream.log`.

Rollback:

```bash
pkill -f scripts/run_gradio_stream_demo.py || true
```

## Stage 5: Optional Future Ingest Experiment (Separate Branch)

Goal:
- If you later add stream ingest in UI, do it in a branch and maintain rollback.

Steps:
1. Create branch:

```bash
git checkout -b feat/stream-ingest-safe
```

2. Apply minimal ingest change.
3. Test local mode first, then stream mode.

Rollback options:
- Soft rollback to previous commit:

```bash
git checkout main
git branch -D feat/stream-ingest-safe
```

- Runtime-only rollback (no git): stop modified app process and relaunch known-good command.

## Stage 6: End of Day Shutdown

Goal:
- Stop extra processes and preserve workspace state.

Commands:

```bash
cd /home/gschi/FluxRT
scripts/streaming/stop_nbc_relay.sh || true
pkill -f scripts/run_gradio_demo.py || true
```

Then stop container/VM.

## Quick Restore Next Session

1. Start VM/container.
2. Launch known-good app.
3. Optionally start relay script.

Known-good app launch:

```bash
cd /home/gschi/FluxRT
source .venv/bin/activate
python scripts/run_gradio_demo.py --int8
```

Optional relay start:

```bash
scripts/streaming/start_nbc_relay.sh
```

## Incident Notes Template

Use this section each attempt:

- Stage:
- Command run:
- Output summary:
- Pass/Fail:
- Rollback performed:
- Next action:
