#!/usr/bin/liquidsoap
# FluxRT flat audio mix: MusicGen (UDP 5002) + Quote TTS (UDP 5004) → mixed AAC MPEG-TS on UDP 5006.
# Env: MUSIC_MIX_VOLUME, TTS_MIX_VOLUME, AUDIO_BITRATE, MIX_OUTPUT_URL, MUSIC_INPUT_URL, TTS_INPUT_URL

music_vol = float_of_string(default="0.85", getenv("MUSIC_MIX_VOLUME"))
tts_vol = float_of_string(default="1.55", getenv("TTS_MIX_VOLUME"))
audio_bitrate = getenv(default="128k", "AUDIO_BITRATE")
mix_out = getenv(default="udp://127.0.0.1:5006?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1", "MIX_OUTPUT_URL")
music_in = getenv(default="udp://127.0.0.1:5002?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1", "MUSIC_INPUT_URL")
tts_in = getenv(default="udp://127.0.0.1:5004?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1", "TTS_INPUT_URL")

settings.init.log.stdout.set(true)
settings.init.log.level.set(3)

def safe_udp(url) =
  mksafe(input.ffmpeg(id="in_" ^ url, url))
end

music = amplify(music_vol, safe_udp(music_in))
tts = amplify(tts_vol, safe_udp(tts_in))
mixed = add(normalize=false, [music, tts])

output.ffmpeg(
  %ffmpeg(%audio(codec="aac", b=audio_bitrate, ar=48000, ac=2), format="mpegts"),
  mixed,
  mix_out
)
