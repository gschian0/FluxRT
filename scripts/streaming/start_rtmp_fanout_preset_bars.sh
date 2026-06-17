#!/usr/bin/env bash
set -euo pipefail

# Bars preset: keeps startup color bars while waiting for live video,
# and uses the known-good audio/TTS mixing profile.

cd "$(dirname "$0")/../.."

ENABLE_YOUTUBE="${ENABLE_YOUTUBE:-0}" \
ENABLE_TWITCH="${ENABLE_TWITCH:-1}" \
ENABLE_FACEBOOK="${ENABLE_FACEBOOK:-0}" \
ENABLE_TTS_OVERLAY="${ENABLE_TTS_OVERLAY:-1}" \
AUDIO_SOURCE_MODE="${AUDIO_SOURCE_MODE:-url}" \
TTS_SOURCE_MODE="${TTS_SOURCE_MODE:-url}" \
MUSIC_MIX_VOLUME="${MUSIC_MIX_VOLUME:-0.65}" \
TTS_MIX_VOLUME="${TTS_MIX_VOLUME:-1.8}" \
STARTUP_BARS_SECONDS="${STARTUP_BARS_SECONDS:-6}" \
STARTUP_BARS_EXTEND_SECONDS="${STARTUP_BARS_EXTEND_SECONDS:-5}" \
MAX_BARS_EXTENSIONS="${MAX_BARS_EXTENSIONS:-2}" \
WAIT_FOR_VIDEO_READY="${WAIT_FOR_VIDEO_READY:-1}" \
AUDIO_INPUT_URL="${AUDIO_INPUT_URL:-udp://127.0.0.1:5002?pkt_size=1316}" \
TTS_INPUT_URL="${TTS_INPUT_URL:-udp://127.0.0.1:5004?pkt_size=1316}" \
scripts/streaming/start_rtmp_fanout.sh
