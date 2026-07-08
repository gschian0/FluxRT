#!/usr/bin/liquidsoap
# FluxRT flat audio mix — values injected by start_liquidsoap_audio_mix.sh

settings.init.allow_root.set(true)

music_vol = @MUSIC_VOL@
tts_vol = @TTS_VOL@
audio_bitrate = "@AUDIO_BITRATE@"
mix_out = "@MIX_OUTPUT_URL@"
music_in = "@MUSIC_INPUT_URL@"
tts_in = "@TTS_INPUT_URL@"

ffmpeg_out = "ffmpeg -hide_banner -nostdin -loglevel warning -i pipe:0 -c:a aac -b:a #{audio_bitrate} -ar 48000 -ac 2 -f mpegts '#{mix_out}'"

def safe_udp(url) =
  mksafe(input.ffmpeg(id="in_" ^ url, url))
end

music = amplify(music_vol, safe_udp(music_in))
tts = amplify(tts_vol, safe_udp(tts_in))
mixed = add(normalize=false, [music, tts])

output.external(
  %wav(stereo=true, samplesize=16),
  ffmpeg_out,
  mixed
)
