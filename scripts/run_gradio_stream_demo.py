import argparse
from collections import deque
import os
import random
import re
import signal
import subprocess
import threading
import time

import cv2
import numpy as np
import gradio as gr

from fluxrt import StreamProcessor
from fluxrt.utils import crop_maximal_rectangle

# Must match fanout FPS (start_rtmp_fanout.sh) to avoid massive frame drops on Twitch.
BROADCAST_FPS = float(os.environ.get("BROADCAST_FPS", "8"))

default_prompt = "8k ultra high resolution claymation character video frame, expressive handmade alien hosts, visible fingerprints in clay, miniature broadcast studio, saturated practical lights, crisp macro lens detail, cinematic depth of field"
default_stream_url = "https://streamer1.connectto.com/AABC_WEB_1201/index.m3u8"
default_music_radio_url = "http://stream.zeno.fm/0a4yq1u0f0hvv"
default_music_station_name = "Reggae King Radio"
default_musicgen_output_dir_name = "musicgen_output_bass_chords_pads"
default_music_prompt = (
    "cool groove, electronic chill downtempo, bassline and chords lead the track, "
    "fat reggae dub sub bassline, cool jazz chord progression, lush synth pads, "
    "airy ethereal melodies throughout, warm chord stabs, light understated drums, "
    "steady instrumental club lounge mix"
)
default_sfx_prompts = """subtle analog tape whoosh, short broadcast transition, clean and quiet
soft futuristic interface chirps, tiny electric sparkles, short and tasteful
distant synthetic thunder swell, low cinematic rumble, restrained
gentle glass shimmer and airy reverse cymbal, short transition sound"""
default_music_station_m3u = """#EXTM3U
#EXTINF:-1,Reggae King Radio
http://stream.zeno.fm/0a4yq1u0f0hvv
#EXTINF:-1,Roots Legacy Radio
http://rootslegacy.ddns.net:8000/stream"""

APP_CSS = """
.gradio-container { max-width: 1680px !important; margin: 0 auto !important; }
.main { background: #f7f7f4; }
.block { border-radius: 8px !important; }
.tabs { border-radius: 8px !important; overflow: visible !important; }
.tab-nav,
.gradio-container div[role="tablist"] {
    display: flex !important;
    flex-wrap: wrap !important;
    gap: 8px !important;
    align-items: center !important;
    overflow: visible !important;
    padding: 8px !important;
    margin-bottom: 10px !important;
    background: #ecece6 !important;
    border: 1px solid #c8c8be !important;
    border-radius: 8px !important;
}
.tab-nav button,
.gradio-container button[role="tab"] {
    min-height: 44px !important;
    padding: 10px 16px !important;
    font-size: 17px !important;
    font-weight: 800 !important;
    line-height: 1.2 !important;
    color: #111827 !important;
    background: #ffffff !important;
    border: 2px solid #a8ada5 !important;
    border-radius: 8px !important;
    opacity: 1 !important;
    text-transform: none !important;
    letter-spacing: 0 !important;
}
.tab-nav button.selected,
.gradio-container button[role="tab"][aria-selected="true"] {
    color: #ffffff !important;
    background: #1f2937 !important;
    border-color: #1f2937 !important;
}
.compact-row { gap: 10px !important; }
textarea, input { font-size: 15px !important; }
.status-strip > div { min-width: 0 !important; }
"""

stream_processor = None
input_tensor = None
output_tensor = None
resolution = None
use_int8 = False

processor_lock = threading.Lock()
current_video_id = 0
current_video_id_lock = threading.Lock()

current_input_frame = None
current_processed_frame = None
frame_lock = threading.Lock()

stream_status = "idle"
stream_status_lock = threading.Lock()
process_frame_counter = 0
stream_config_path = "configs/stream_demo_config.json"
filter_enabled = True
filter_enabled_lock = threading.Lock()
overlay_enabled = True
overlay_enabled_lock = threading.Lock()
stream_channel_map = {}
stream_channel_map_lock = threading.Lock()
music_station_map = {}
music_station_map_lock = threading.Lock()
repo_catalog_paths = {}

music_station_map[default_music_station_name] = default_music_radio_url

voice_choices = [
    "Magpie-Multilingual.EN-US.Aria",
    "Magpie-Multilingual.EN-US.Sarah",
    "Magpie-Multilingual.EN-US.Ryan",
]

udp_writer = None
udp_writer_lock = threading.Lock()
udp_writer_dims = (0, 0)
broadcast_default_width = int(os.getenv("BROADCAST_WIDTH", "288"))
broadcast_default_height = int(os.getenv("BROADCAST_HEIGHT", "160"))

broadcast_sender_thread = None
broadcast_sender_lock = threading.Lock()
broadcast_send_queue = deque(maxlen=8)
broadcast_send_queue_lock = threading.Lock()

quote_tts_proc = None
quote_tts_lock = threading.Lock()
sfx_proc = None
sfx_lock = threading.Lock()
prompt_rotator_proc = None
prompt_rotator_lock = threading.Lock()

stream_relay_proc = None
stream_relay_lock = threading.Lock()
stream_relay_input_url = ""
stream_relay_output_url = "udp://127.0.0.1:5010?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1"

processed_valid_streak = 0
processed_valid_streak_lock = threading.Lock()
broadcast_ready = False
broadcast_ready_lock = threading.Lock()
processed_frame_buffer = deque()
processed_frame_buffer_lock = threading.Lock()
required_processed_streak = 20
lead_buffer_seconds = 0.8
last_good_broadcast_frame = None
last_good_broadcast_frame_lock = threading.Lock()


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _is_process_running(pattern: str) -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _is_music_stream_running() -> bool:
    return _is_process_running("run_musicgen_radio_plus_musicGEN.py")


def _is_quote_voice_running() -> bool:
    if _quote_tts_is_running():
        return True
    return _is_process_running("run_quote_tts_from_json.py") or _is_process_running("run_edge_tts_quotes.py")


def _is_sfx_stream_running() -> bool:
    with sfx_lock:
        if sfx_proc is not None and sfx_proc.poll() is None:
            return True
    return _is_process_running("run_audiogen_sfx_stream.py")


def _prompt_rotator_is_running() -> bool:
    with prompt_rotator_lock:
        if prompt_rotator_proc is not None and prompt_rotator_proc.poll() is None:
            return True
    return _is_process_running("scripts/streaming/rotate_flux_prompt.py")


def _is_audio_mix_bus_running() -> bool:
    pid_file = "/tmp/fluxrt-audio-mix.pid"
    try:
        if os.path.isfile(pid_file):
            raw = open(pid_file, "r", encoding="utf-8", errors="ignore").read().strip()
            if raw.isdigit() and _pid_is_running(int(raw)):
                return True
    except Exception:
        pass
    return _is_process_running("/tmp/fluxrt-audio-mix-loop.sh")


def _is_stream_monitor_running() -> bool:
    pid_file = "/tmp/fluxrt-monitor-http.pid"
    try:
        if os.path.isfile(pid_file):
            raw = open(pid_file, "r", encoding="utf-8", errors="ignore").read().strip()
            if raw.isdigit() and _pid_is_running(int(raw)):
                return True
    except Exception:
        pass
    return _is_process_running("http.server 8090")


def stream_monitor_url() -> str:
    return "http://127.0.0.1:8090/"


def _status_light_html(label: str, running: bool) -> str:
    color = "#16a34a" if running else "#dc2626"
    state = "RUNNING" if running else "STOPPED"
    return (
        "<div style='display:flex;align-items:center;gap:8px;padding:6px 10px;"
        "border:1px solid #e5e7eb;border-radius:10px;background:#ffffff;'>"
        f"<span style='width:12px;height:12px;border-radius:50%;background:{color};"
        "display:inline-block;'></span>"
        f"<span style='font-size:13px;'><strong>{label}</strong>: {state}</span>"
        "</div>"
    )


def poll_audio_service_lights():
    music_running = _is_music_stream_running()
    quote_running = _is_quote_voice_running()
    sfx_running = _is_sfx_stream_running()
    bus_running = _is_audio_mix_bus_running()
    return (
        _status_light_html("Music", music_running),
        _status_light_html("Quote Voice", quote_running),
        _status_light_html("SFX", sfx_running),
        _status_light_html("Audio Bus", bus_running),
    )


def refresh_stream_monitor_status():
    state = "running" if _is_stream_monitor_running() else "stopped"
    playlist = "http://127.0.0.1:8090/stream.m3u8"
    return f"monitor: {state}\npage: {stream_monitor_url()}\nplaylist: {playlist}"


def start_stream_monitor_http_ui():
    cmd = ["bash", "-lc", "cd /workspace/FluxRT && scripts/streaming/start_stream_monitor_http.sh"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    msg = (result.stdout or result.stderr or "monitor start attempted").strip()
    if result.returncode != 0:
        set_status(f"monitor start failed: {msg}")
        return f"monitor start failed: {msg}"
    set_status("monitor http started")
    return msg


def stop_stream_monitor_http_ui():
    cmd = ["bash", "-lc", "cd /workspace/FluxRT && scripts/streaming/stop_stream_monitor_http.sh"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    msg = (result.stdout or result.stderr or "monitor stop attempted").strip()
    set_status("monitor http stopped")
    return msg


def start_audio_mix_bus_ui(
    music_mix_volume: float,
    tts_mix_volume: float,
    audio_bitrate: str = "128k",
    enable_sfx_input: bool = False,
    sfx_mix_volume: float = 0.25,
):
    env = os.environ.copy()
    env["MUSIC_MIX_VOLUME"] = str(float(music_mix_volume))
    env["TTS_MIX_VOLUME"] = str(float(tts_mix_volume))
    env["SFX_MIX_VOLUME"] = str(float(sfx_mix_volume))
    env["ENABLE_SFX_INPUT"] = "1" if enable_sfx_input else "0"
    if enable_sfx_input:
        env["FORCE_RESTART"] = "1"
    env["AUDIO_BITRATE"] = (audio_bitrate or "128k").strip()
    cmd = ["bash", "-lc", "cd /workspace/FluxRT && scripts/streaming/start_audio_mix_bus.sh"]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "audio bus start failed").strip()
        set_status(f"audio bus start failed: {msg}")
        return f"audio bus start failed: {msg}"
    set_status("audio mix bus started (udp 5006)")
    return (result.stdout or "audio mix bus started").strip()


def stop_audio_mix_bus_ui():
    cmd = ["bash", "-lc", "cd /workspace/FluxRT && scripts/streaming/stop_audio_mix_bus.sh"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    msg = (result.stdout or result.stderr or "audio bus stopped").strip()
    set_status("audio mix bus stopped")
    return msg


def _reset_broadcast_gate(reason: str | None = None):
    global processed_valid_streak, broadcast_ready
    with processed_valid_streak_lock:
        processed_valid_streak = 0
    with broadcast_ready_lock:
        broadcast_ready = False
    with processed_frame_buffer_lock:
        processed_frame_buffer.clear()
    with broadcast_send_queue_lock:
        broadcast_send_queue.clear()
    if reason:
        set_status(f"broadcast gated: {reason}")


def _broadcast_sender_loop():
    last_frame = None
    last_fps = 8
    next_send_ts = 0.0
    while True:
        payload = None
        with broadcast_send_queue_lock:
            if broadcast_send_queue:
                payload = broadcast_send_queue.popleft()

        now = time.monotonic()
        if payload is None:
            if last_frame is not None and now >= next_send_ts:
                _write_to_udp(last_frame, fps=int(max(1, last_fps)))
                next_send_ts = now + (1.0 / float(max(1, last_fps)))
                continue
            time.sleep(0.002)
            continue

        frame_to_send, fps = payload
        last_frame = frame_to_send
        last_fps = int(max(1, fps))
        _write_to_udp(frame_to_send, fps=last_fps)
        next_send_ts = now + (1.0 / float(max(1, last_fps)))


def _ensure_broadcast_sender():
    global broadcast_sender_thread
    with broadcast_sender_lock:
        if broadcast_sender_thread is not None and broadcast_sender_thread.is_alive():
            return
        broadcast_sender_thread = threading.Thread(
            target=_broadcast_sender_loop,
            daemon=True,
        )
        broadcast_sender_thread.start()


def _enqueue_broadcast_frame(frame: np.ndarray, fps: float):
    frame = _normalize_broadcast_frame(frame)
    if frame is None:
        return
    _ensure_broadcast_sender()
    frame_copy = frame.copy()
    with broadcast_send_queue_lock:
        broadcast_send_queue.append((frame_copy, float(fps)))


def _broadcast_target_dims() -> tuple[int, int]:
    with udp_writer_lock:
        if udp_writer_dims[0] > 0 and udp_writer_dims[1] > 0:
            return int(udp_writer_dims[0]), int(udp_writer_dims[1])

    with last_good_broadcast_frame_lock:
        if isinstance(last_good_broadcast_frame, np.ndarray) and last_good_broadcast_frame.size > 0:
            h, w = last_good_broadcast_frame.shape[:2]
            if w > 0 and h > 0:
                return int(w), int(h)

    if isinstance(resolution, dict):
        try:
            w = int(resolution.get("width", 0))
            h = int(resolution.get("height", 0))
            if w > 0 and h > 0:
                return w, h
        except Exception:
            pass

    return broadcast_default_width, broadcast_default_height


def _normalize_broadcast_frame(frame: np.ndarray | None) -> np.ndarray | None:
    if frame is None or not isinstance(frame, np.ndarray) or frame.size == 0:
        return None

    target_w, target_h = _broadcast_target_dims()
    h, w = frame.shape[:2]
    if w == target_w and h == target_h:
        return frame

    interpolation = cv2.INTER_AREA if (w > target_w or h > target_h) else cv2.INTER_LINEAR
    try:
        return cv2.resize(frame, (target_w, target_h), interpolation=interpolation)
    except Exception:
        return frame


def _is_processed_frame_valid(frame: np.ndarray | None) -> bool:
    return isinstance(frame, np.ndarray) and frame.size > 0 and not _is_zero_frame(frame)


def _target_buffer_frames(fps: float) -> int:
    fps_val = max(1.0, float(fps or BROADCAST_FPS))
    return max(8, int(round(fps_val * lead_buffer_seconds)))


def _set_broadcast_ready(ready: bool):
    global broadcast_ready
    with broadcast_ready_lock:
        broadcast_ready = bool(ready)


def _is_broadcast_ready() -> bool:
    with broadcast_ready_lock:
        return broadcast_ready


def _can_start_fanout_now() -> bool:
    if _is_broadcast_ready():
        return True
    if not is_filter_enabled():
        return False
    if not _workers_alive():
        with frame_lock:
            input_frame = current_input_frame
        return _is_processed_frame_valid(to_bgr(input_frame) if input_frame is not None else None)
    with frame_lock:
        frame = current_processed_frame
    return _is_processed_frame_valid(to_bgr(frame) if frame is not None else None)


def _push_processed_for_broadcast(processed_frame: np.ndarray | None, fps: float):
    global processed_valid_streak, last_good_broadcast_frame

    if not is_filter_enabled():
        _reset_broadcast_gate("filter is off")
        return

    if not _workers_alive():
        _reset_broadcast_gate("workers not healthy")
        with frame_lock:
            fallback_input = current_input_frame
        fallback_frame = to_bgr(fallback_input) if fallback_input is not None else None
        if fallback_frame is not None:
            _enqueue_broadcast_frame(fallback_frame, fps=float(fps))
        return

    if not _is_processed_frame_valid(processed_frame):
        with processed_valid_streak_lock:
            processed_valid_streak = 0
        _set_broadcast_ready(False)
        # Keep the stream alive through short upstream stalls by replaying
        # the most recent valid real frame instead of dropping the publisher.
        with last_good_broadcast_frame_lock:
            fallback_frame = (
                None
                if last_good_broadcast_frame is None
                else last_good_broadcast_frame.copy()
            )
        if fallback_frame is None:
            with frame_lock:
                current_input = current_input_frame
            if current_input is not None:
                fallback_frame = to_bgr(current_input)
        if fallback_frame is not None:
            _enqueue_broadcast_frame(fallback_frame, fps=float(fps))
        return

    with processed_valid_streak_lock:
        processed_valid_streak += 1
        streak = processed_valid_streak

    target = _target_buffer_frames(fps)
    with processed_frame_buffer_lock:
        processed_frame_buffer.append(processed_frame.copy())
        while len(processed_frame_buffer) > target * 4:
            processed_frame_buffer.popleft()
        buffered = len(processed_frame_buffer)

    with last_good_broadcast_frame_lock:
        last_good_broadcast_frame = processed_frame.copy()

    if streak < required_processed_streak:
        _set_broadcast_ready(False)
        _enqueue_broadcast_frame(processed_frame, fps=float(fps))
        return

    if buffered < target:
        _set_broadcast_ready(False)
        _enqueue_broadcast_frame(processed_frame, fps=float(fps))
        return

    _set_broadcast_ready(True)
    with processed_frame_buffer_lock:
        if not processed_frame_buffer:
            _set_broadcast_ready(False)
            return
        frame_to_send = processed_frame_buffer.popleft()
    with last_good_broadcast_frame_lock:
        last_good_broadcast_frame = frame_to_send.copy()
    _enqueue_broadcast_frame(frame_to_send, fps=float(fps))


def _get_udp_writer(width, height, fps=None):
    global udp_writer, udp_writer_dims
    if fps is None:
        fps = BROADCAST_FPS
    # Keep keyframes frequent so downstream decoders recover quickly from UDP loss.
    gop = max(8, int(float(fps) * 2))
    with udp_writer_lock:
        if udp_writer is not None and udp_writer_dims != (width, height, float(fps)):
            try:
                udp_writer.terminate()
                udp_writer.wait(timeout=2)
            except Exception:
                pass
            udp_writer = None
            # Kill any orphaned ffmpeg writers left over from previous sessions
            import subprocess as _sp
            _sp.run(
                ['pkill', '-f', 'ffmpeg.*-f rawvideo.*udp://127.0.0.1:5000'],
                capture_output=True,
            )

        if udp_writer is None:
            cmd = [
                'ffmpeg',
                '-hide_banner', '-loglevel', 'error',
                '-y',
                '-f', 'rawvideo',
                '-vcodec', 'rawvideo',
                '-pix_fmt', 'bgr24',
                '-s', f'{width}x{height}',
                '-r', str(fps),
                '-i', '-',
                '-c:v', 'libx264',
                '-preset', 'ultrafast',
                '-tune', 'zerolatency',
                '-g', str(gop),
                '-keyint_min', str(gop),
                '-sc_threshold', '0',
                '-x264-params', f'repeat-headers=1:keyint={gop}:min-keyint={gop}:scenecut=0',
                '-pix_fmt', 'yuv420p',
                '-mpegts_flags', '+resend_headers',
                '-muxdelay', '0',
                '-muxpreload', '0',
                '-flush_packets', '1',
                '-f', 'mpegts',
                'udp://127.0.0.1:5000?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1'
            ]
            udp_writer = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            udp_writer_dims = (width, height, float(fps))
        return udp_writer


def _write_to_udp(frame, fps=None):
    global udp_writer
    if frame is None:
        return
    if fps is None:
        fps = BROADCAST_FPS
    h, w = frame.shape[:2]
    writer = _get_udp_writer(w, h, fps)
    if writer and writer.stdin:
        try:
            writer.stdin.write(frame.tobytes())
        except Exception:
            with udp_writer_lock:
                udp_writer = None


def _is_zero_frame(frame: np.ndarray | None) -> bool:
    if frame is None:
        return True
    if not isinstance(frame, np.ndarray):
        return False
    if frame.size == 0:
        return True
    return int(frame.max()) == 0


def get_processor():
    global stream_processor, input_tensor, output_tensor, resolution

    if stream_processor is None:
        stream_processor = StreamProcessor(stream_config_path)
        if use_int8:
            stream_processor.enable_quantization()
        stream_processor.start()
        stream_processor.set_prompt(default_prompt)

        input_tensor = stream_processor.get_input_tensor()
        output_tensor = stream_processor.get_output_tensor()
        resolution = stream_processor.get_resolution()

    return stream_processor, input_tensor, output_tensor, resolution


def _workers_alive() -> bool:
    sp = stream_processor
    if sp is None:
        return False
    try:
        mi_proc = sp.model_inference_subprocess.process
        out_proc = sp.output_scheduler_subprocess.process
        return bool(
            mi_proc is not None
            and out_proc is not None
            and mi_proc.is_alive()
            and out_proc.is_alive()
        )
    except Exception:
        return False


def _cleanup_orphan_local_workers() -> None:
    """Best-effort cleanup for local spawned workers after recovery failures."""
    parent_pid = os.getpid()
    try:
        ps_out = subprocess.check_output(
            ["ps", "-eo", "pid=,ppid=,cmd="], text=True
        )
    except Exception:
        return

    victims: list[int] = []
    for row in ps_out.splitlines():
        parts = row.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except Exception:
            continue
        cmd = parts[2]
        if ppid != parent_pid:
            continue
        if "multiprocessing.spawn import spawn_main" not in cmd:
            continue
        victims.append(pid)

    for pid in victims:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

    if victims:
        time.sleep(0.3)
        for pid in victims:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass


def _cleanup_global_orphan_workers() -> None:
    """Reap stale worker subprocesses orphaned to init (PPID 1)."""
    try:
        ps_out = subprocess.check_output(
            ["ps", "-eo", "pid=,ppid=,cmd="], text=True
        )
    except Exception:
        return

    victims: list[int] = []
    for row in ps_out.splitlines():
        parts = row.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except Exception:
            continue
        cmd = parts[2]
        if ppid != 1:
            continue
        if "/workspace/FluxRT/.venv/bin/python3" not in cmd:
            continue
        if "multiprocessing.spawn import spawn_main" not in cmd:
            continue
        victims.append(pid)

    for pid in victims:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

    # Also reap stale FFmpeg stream-relay processes that can persist across
    # app restarts and starve webcam processing.
    try:
        subprocess.run(
            [
                "pkill",
                "-f",
                "ffmpeg.*-f mpegts udp://127.0.0.1:5010",
            ],
            check=False,
            capture_output=True,
        )
    except Exception:
        pass

    if victims:
        time.sleep(0.3)
        for pid in victims:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass


def reset_processor(reason: str):
    global stream_processor, input_tensor, output_tensor, resolution
    set_status(f"recovering: {reason}")
    if stream_processor is not None:
        try:
            stream_processor.stop()
        except Exception:
            pass

    # If a previous worker died unexpectedly, stop() can leave extra local
    # children behind; reap them to avoid sustained CPU slowdown.
    _cleanup_orphan_local_workers()
    _cleanup_global_orphan_workers()

    stream_processor = None
    input_tensor = None
    output_tensor = None
    resolution = None


def to_bgr(frame):
    if frame is None:
        return None
    if not isinstance(frame, np.ndarray):
        return frame
    if frame.ndim == 2:
        return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    if frame.ndim != 3:
        return frame

    channels = frame.shape[2]
    if channels == 4:
        return cv2.cvtColor(frame, cv2.COLOR_RGBA2BGR)
    if channels == 3:
        return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    return frame

SHOW_NAMES = [
    "Quantum Flux TV",
    "Synthwave Sunday",
    "Cybernetic News Network",
    "Midnight AI Broadcast",
    "Neon Glow Live",
    "Neural Network Morning",
    "AI Visionaries",
    "The Matrix Feed",
    "Deep Dream Cinema",
    "Latent Space Lounge",
    "Generative Grooves",
    "The Diffusion Dispatch",
    "Synthetic Soul Radio",
    "Algorithmic Awakening",
    "Pixel Prophecy Hour",
    "The Embedding Empire",
    "Neural Noise Network",
    "Transformer Theater",
    "Gradient Garden Live",
    "Backprop Broadcast",
    "Stochastic Sessions",
    "The Attention Agenda",
    "Prompt Engineering Today",
    "Hallucination Hour",
    "Weight Space Weekly",
    "The Token Tribunal",
    "Fine-Tune Friday",
    "Inference Island",
    "The Loss Landscape",
    "Checkpoint Chronicles",
    "Epoch Evening News",
    "Batch Size Bonanza",
    "The Regularization Report",
    "Dropout Diaries",
    "Activation Atlas Live",
    "The Manifold Mix",
    "Vector Vortex TV",
    "Embedding Echoes",
    "The Singularity Show",
    "AGI Alert Network",
    "Robo Renaissance Radio",
    "The Turing Test Tribune",
    "Cybernetic Sunrise",
    "Holographic Hits",
    "The Phantom Frequency",
    "Glitch Gospel Hour",
    "Vaporwave Vault",
    "Retro Render Room",
    "The Polychrome Pulse",
    "Chromatic Chaos Channel",
]

# Philosopher quotes loaded from data/quotes for the ticker
_TICKER_QUOTES: list[str] = []
_TICKER_QUOTES_LOADED = False

def _load_ticker_quotes() -> list[str]:
    global _TICKER_QUOTES, _TICKER_QUOTES_LOADED
    if _TICKER_QUOTES_LOADED:
        return _TICKER_QUOTES
    _TICKER_QUOTES_LOADED = True
    try:
        import json
        import os
        # Prefer Gemini quotes, fall back to diffusiongemma for backward compat
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        gemini_path = os.path.join(repo_root, "data", "quotes", "gemini_quotes.json")
        diffusion_path = os.path.join(repo_root, "data", "quotes", "diffusiongemma_quotes.json")
        quotes_path = gemini_path if os.path.exists(gemini_path) else diffusion_path
        if os.path.exists(quotes_path):
            with open(quotes_path, "r") as f:
                data = json.load(f)
            _TICKER_QUOTES = [f'"{item["quote"]}" — {item["philosopher"]}' for item in data if "quote" in item and "philosopher" in item]
            print(f"[overlay] Loaded {len(_TICKER_QUOTES)} ticker quotes from {quotes_path}")
    except Exception as exc:
        print(f"[overlay] Could not load ticker quotes: {exc}")
    return _TICKER_QUOTES

_current_show_str = SHOW_NAMES[0]
_show_last_changed = time.time()
_current_ticker_idx = 0
_ticker_last_changed = time.time()
_ticker_scroll_offset = 0.0


def _fit_text_to_width(text: str, max_width: int, font_scale: float, thickness: int) -> str:
    font = cv2.FONT_HERSHEY_SIMPLEX
    if cv2.getTextSize(text, font, font_scale, thickness)[0][0] <= max_width:
        return text

    trimmed = text
    while len(trimmed) > 4:
        candidate = trimmed[:-1].rstrip() + "..."
        if cv2.getTextSize(candidate, font, font_scale, thickness)[0][0] <= max_width:
            return candidate
        trimmed = trimmed[:-1]
    return "..."


def add_tv_overlay(frame_bgr: np.ndarray) -> np.ndarray:
    global _current_show_str, _show_last_changed, _current_ticker_idx, _ticker_last_changed, _ticker_scroll_offset
    now = time.time()

    # Change show name every 15 seconds
    if now - _show_last_changed > 15.0:
        _current_show_str = random.choice(SHOW_NAMES)
        _show_last_changed = now

    # Rotate ticker quote every 12 seconds
    quotes = _load_ticker_quotes()
    if quotes and now - _ticker_last_changed > 12.0:
        _current_ticker_idx = (_current_ticker_idx + 1) % len(quotes)
        _ticker_last_changed = now
        _ticker_scroll_offset = 0.0

    out = frame_bgr.copy()
    h, w = out.shape[:2]
    scale = max(0.42, min(1.0, min(w / 640.0, h / 360.0)))
    margin = max(6, int(16 * scale))
    bar_h = max(32, int(60 * scale))
    bar_y1 = max(6, h - margin - bar_h)
    bar_y2 = h - margin
    label_w = max(42, int(84 * scale))
    label_h = max(20, int(38 * scale))
    label_x1 = margin + max(4, int(10 * scale))
    label_y1 = bar_y1 + max(5, int(12 * scale))
    label_y2 = min(bar_y2 - 4, label_y1 + label_h)
    live_font = max(0.35, 0.75 * scale)
    show_font = max(0.34, 0.88 * scale)
    clock_font = max(0.34, 0.72 * scale)
    ticker_font = max(0.30, 0.55 * scale)
    text_thickness = max(1, int(round(2 * scale)))

    overlay = out.copy()
    cv2.rectangle(overlay, (margin, bar_y1), (w - margin, bar_y2), (0, 0, 0), -1)
    cv2.rectangle(overlay, (label_x1, label_y1), (label_x1 + label_w, label_y2), (0, 0, 220), -1)
    cv2.addWeighted(overlay, 0.6, out, 0.4, 0, out)

    live_x = label_x1 + max(5, int(10 * scale))
    live_y = label_y2 - max(5, int(10 * scale))
    show_x = label_x1 + label_w + max(8, int(16 * scale))
    show_y = live_y
    show_text = _fit_text_to_width(
        _current_show_str,
        max(24, w - margin - show_x - 4),
        show_font,
        text_thickness,
    )
    cv2.putText(out, "LIVE", (live_x, live_y), cv2.FONT_HERSHEY_SIMPLEX, live_font, (255, 255, 255), text_thickness)
    cv2.putText(out, show_text, (show_x, show_y), cv2.FONT_HERSHEY_SIMPLEX, show_font, (255, 255, 255), text_thickness)

    # Scrolling ticker with philosopher quotes (thin bar above the show name bar)
    ticker_text = ""
    if quotes:
        ticker_text = quotes[_current_ticker_idx] if _current_ticker_idx < len(quotes) else ""
    if ticker_text:
        ticker_h = max(18, int(28 * scale))
        ticker_y1 = max(2, bar_y1 - ticker_h - 2)
        ticker_y2 = bar_y1 - 2
        ticker_x1 = margin
        ticker_x2 = w - margin
        cv2.rectangle(out, (ticker_x1, ticker_y1), (ticker_x2, ticker_y2), (10, 10, 30), -1)
        # Scroll the text horizontally — start off-screen right, scroll all the way to off-screen left
        text_w = cv2.getTextSize(ticker_text, cv2.FONT_HERSHEY_SIMPLEX, ticker_font, 1)[0][0]
        # Total travel distance: from fully off-screen right to fully off-screen left
        scroll_range = max(1, text_w + (ticker_x2 - ticker_x1))
        _ticker_scroll_offset = (_ticker_scroll_offset + max(1.0, 1.5 * scale)) % scroll_range
        # Text starts at right edge of bar + text width (off-screen), scrolls left
        start_x = ticker_x2 + text_w - int(_ticker_scroll_offset)
        # Reset when text has fully scrolled off the left edge
        if start_x + text_w < ticker_x1:
            _ticker_scroll_offset = 0.0
            start_x = ticker_x2 + text_w
        # Clip drawing to ticker area using ROI
        ticker_y_text = ticker_y2 - max(4, int(8 * scale))
        clip_x1 = max(0, ticker_x1)
        clip_x2 = min(w, ticker_x2)
        clip_y1 = max(0, ticker_y1)
        clip_y2 = min(h, ticker_y2)
        if clip_x2 > clip_x1 and clip_y2 > clip_y1:
            # Draw text on a temp strip, then copy only the clipped region
            temp = np.zeros((clip_y2 - clip_y1, w, 3), dtype=out.dtype)
            temp_x = start_x
            temp_y = ticker_y_text - clip_y1
            cv2.putText(temp, ticker_text, (temp_x, temp_y), cv2.FONT_HERSHEY_SIMPLEX, ticker_font, (180, 220, 255), 1)
            out[clip_y1:clip_y2, clip_x1:clip_x2] = temp[0:clip_y2 - clip_y1, clip_x1:clip_x2]

    current_time_str = time.strftime("%H:%M:%S")
    tw, th = cv2.getTextSize(current_time_str, cv2.FONT_HERSHEY_SIMPLEX, clock_font, text_thickness)[0]
    x2 = w - margin
    x1 = max(margin, x2 - tw - max(12, int(24 * scale)))
    y1 = margin
    y2 = y1 + th + max(10, int(18 * scale))
    cv2.rectangle(out, (x1, y1), (x2, y2), (0, 0, 0), -1)
    cv2.rectangle(out, (x1, y1), (x2, y2), (70, 70, 70), 1)
    cv2.putText(out, current_time_str, (x1 + max(6, int(12 * scale)), y2 - max(5, int(8 * scale))), cv2.FONT_HERSHEY_SIMPLEX, clock_font, (235, 235, 235), text_thickness)

    return out


def polish_processed_frame(frame_bgr: np.ndarray) -> np.ndarray:
    if frame_bgr is None:
        return frame_bgr
    try:
        frame = np.asarray(frame_bgr)
        if frame.dtype != np.uint8:
            frame = np.clip(frame, 0, 255).astype(np.uint8)

        lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
        l_channel, a_channel, b_channel = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=1.35, tileGridSize=(4, 4))
        l_channel = clahe.apply(l_channel)
        contrast = cv2.cvtColor(cv2.merge((l_channel, a_channel, b_channel)), cv2.COLOR_LAB2BGR)

        blur = cv2.GaussianBlur(contrast, (0, 0), 0.8)
        sharpened = cv2.addWeighted(contrast, 1.22, blur, -0.22, 0)
        hsv = cv2.cvtColor(sharpened, cv2.COLOR_BGR2HSV)
        h_channel, s_channel, v_channel = cv2.split(hsv)
        s_channel = np.clip(s_channel.astype(np.float32) * 1.06, 0, 255).astype(np.uint8)
        v_channel = np.clip(v_channel.astype(np.float32) * 1.02, 0, 255).astype(np.uint8)
        return cv2.cvtColor(cv2.merge((h_channel, s_channel, v_channel)), cv2.COLOR_HSV2BGR)
    except Exception as exc:
        print(f"Visual polish error: {exc}")
        return frame_bgr


def apply_overlay(frame_bgr: np.ndarray) -> np.ndarray:
    if frame_bgr is None:
        return frame_bgr
    try:
        return add_tv_overlay(frame_bgr)
    except Exception as exc:
        print(f"Overlay error: {exc}")
        return frame_bgr


def set_filter_enabled(enabled: bool):
    global filter_enabled
    with filter_enabled_lock:
        filter_enabled = bool(enabled)
    if not filter_enabled:
        _reset_broadcast_gate("filter is off")
    else:
        _reset_broadcast_gate("warming processed output")
    set_status(f"filter: {'on' if filter_enabled else 'off'}")


def is_filter_enabled() -> bool:
    with filter_enabled_lock:
        return filter_enabled


def set_overlay_enabled(enabled: bool):
    global overlay_enabled
    with overlay_enabled_lock:
        overlay_enabled = bool(enabled)
    set_status(f"overlay: {'on' if overlay_enabled else 'off'}")


def is_overlay_enabled() -> bool:
    with overlay_enabled_lock:
        return overlay_enabled


def parse_m3u_channels(m3u_text: str) -> list[tuple[str, str]]:
    channels: list[tuple[str, str]] = []
    if not m3u_text:
        return channels

    pending_name: str | None = None
    for raw_line in m3u_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            name = line.split(",", 1)[-1].strip() if "," in line else ""
            pending_name = re.sub(r"\s+", " ", name) or "Untitled"
            continue
        if line.startswith("#"):
            continue
        if line.startswith("http://") or line.startswith("https://"):
            channel_name = pending_name or f"Channel {len(channels) + 1}"
            channels.append((channel_name, line))
            pending_name = None

    # Fallback: if pasted text is not strict M3U, extract bare URLs anyway.
    if not channels:
        url_matches = re.findall(r"https?://\S+", m3u_text)
        for idx, url in enumerate(url_matches, start=1):
            clean_url = url.strip().rstrip(",;)")
            if clean_url:
                channels.append((f"Channel {idx}", clean_url))
    return channels


def _catalog_dir() -> str:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)
    return os.path.join(repo_root, "iptv-streams")


def discover_repo_catalogs() -> dict[str, str]:
    base = _catalog_dir()
    if not os.path.isdir(base):
        return {}

    out: dict[str, str] = {}
    for name in sorted(os.listdir(base)):
        if not name.endswith(".m3u"):
            continue
        key = name[:-4]
        out[key] = os.path.join(base, name)
    return out


def _load_repo_channels(catalog_key: str) -> list[tuple[str, str]]:
    path = repo_catalog_paths.get(catalog_key)
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return parse_m3u_channels(f.read())
    except Exception:
        return []


def load_repo_catalog_choice(catalog_key: str):
    channels = _load_repo_channels(catalog_key)
    with stream_channel_map_lock:
        stream_channel_map.clear()
        for idx, (name, url) in enumerate(channels, start=1):
            label = f"{name} [{idx}]"
            stream_channel_map[label] = url

    if not channels:
        set_status(f"catalog {catalog_key}: no channels parsed")
        return gr.update(choices=[], value=None), gr.update()

    first_name = next(iter(stream_channel_map.keys()))
    first_url = stream_channel_map[first_name]
    set_status(f"catalog {catalog_key}: loaded {len(channels)} channels")
    return gr.update(choices=list(stream_channel_map.keys()), value=first_name), gr.update(value=first_url)


def load_m3u_catalog(m3u_text: str):
    channels = parse_m3u_channels(m3u_text)
    with stream_channel_map_lock:
        stream_channel_map.clear()
        for name, url in channels:
            stream_channel_map[name] = url

    if not channels:
        set_status("m3u catalog: no channels parsed")
        return gr.update(choices=[], value=None), gr.update()

    first_name, first_url = channels[0]
    set_status(f"m3u catalog loaded: {len(channels)} channels")
    return (
        gr.update(choices=[name for name, _ in channels], value=first_name),
        gr.update(value=first_url),
    )


def apply_channel_choice(channel_name: str):
    with stream_channel_map_lock:
        url = stream_channel_map.get(channel_name, "")
    if not url:
        set_status("channel select: no URL found")
        return gr.update()
    set_status(f"channel selected: {channel_name}")
    return gr.update(value=url)


def load_music_station_catalog(m3u_text: str):
    stations = parse_m3u_channels(m3u_text)
    with music_station_map_lock:
        music_station_map.clear()
        for name, url in stations:
            music_station_map[name] = url

    if not stations:
        set_status("music station catalog: no stations parsed")
        return gr.update(choices=[], value=None), gr.update()

    first_name, first_url = stations[0]
    set_status(f"music station catalog loaded: {len(stations)} stations")
    return gr.update(choices=[name for name, _ in stations], value=first_name), gr.update(value=first_url)


def apply_music_station_choice(station_name: str):
    with music_station_map_lock:
        url = music_station_map.get(station_name, "")
    if not url:
        return gr.update()
    set_status(f"music station selected: {station_name}")
    return gr.update(value=url)


def _repo_root() -> str:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(script_dir)


def _radio_stations_dir() -> str:
    return os.path.join(_repo_root(), "radio-stations")


def load_music_stations_from_repo_files() -> list[str]:
    base = _radio_stations_dir()
    parsed: list[tuple[str, str]] = []
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            if not name.endswith(".m3u"):
                continue
            path = os.path.join(base, name)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    entries = parse_m3u_channels(f.read())
            except Exception:
                continue
            for station_name, station_url in entries:
                label = f"{os.path.splitext(name)[0]} | {station_name}"
                parsed.append((label, station_url))

    with music_station_map_lock:
        music_station_map.clear()
        for label, url in parsed:
            music_station_map[label] = url
        if not music_station_map:
            music_station_map[default_music_station_name] = default_music_radio_url
        return list(music_station_map.keys())


def start_musicgen_stream(
    radio_url: str,
    base_prompt: str,
    sample_seconds: int,
    gen_seconds: int,
    top_k: int,
    top_p: float,
    temperature: float,
    guidance_scale: float,
    stream_delay_seconds: float,
    crossfade_seconds: float,
):
    if not radio_url or not radio_url.strip():
        return "musicgen: radio URL required"

    env = os.environ.copy()
    env["RADIO_URL"] = radio_url.strip()
    env["MUSICGEN_BASE_PROMPT"] = (base_prompt or default_music_prompt).strip()
    env["MUSICGEN_SAMPLE_SECONDS"] = str(int(sample_seconds))
    env["MUSICGEN_GEN_SECONDS"] = str(int(gen_seconds))
    env["MUSICGEN_TOP_K"] = str(int(top_k))
    env["MUSICGEN_TOP_P"] = str(float(top_p))
    env["MUSICGEN_TEMPERATURE"] = str(float(temperature))
    env["MUSICGEN_GUIDANCE_SCALE"] = str(float(guidance_scale))
    env["MUSICGEN_DRUNK_WALK"] = "0"
    env["MUSICGEN_DRUNK_WALK_STRENGTH"] = "0.0"
    env["MUSICGEN_MODEL"] = "facebook/musicgen-small"
    env["MUSICGEN_OUTPUT_DIR"] = os.path.join(_repo_root(), default_musicgen_output_dir_name)
    env["MUSICGEN_CONDITIONING_MODE"] = "text"
    env["MUSICGEN_CONDITIONING_SECONDS"] = "8"
    env["MUSICGEN_PARALLEL_CLIPS"] = "3"
    env["MUSICGEN_SEED"] = "424242"
    env["MUSICGEN_STREAM_DELAY_SECONDS"] = str(float(stream_delay_seconds))
    env["MUSICGEN_CROSSFADE_SECONDS"] = str(float(crossfade_seconds))
    env["MUSICGEN_PAUSE_SECONDS"] = "0"
    env["MUSICGEN_BOOTSTRAP_CLIPS"] = "64"
    env["MUSICGEN_AUDIO_UDP_URL"] = "udp://127.0.0.1:5002?pkt_size=1316"

    cmd = ["bash", "-lc", "cd /workspace/FluxRT && scripts/start_musicgen_radio_plus_musicGEN.sh"]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "unknown error").strip()
        set_status(f"musicgen start failed: {msg}")
        return f"musicgen start failed: {msg}"

    set_status(f"broadcast waiting on backing track: buffering {float(stream_delay_seconds):.0f}s")
    return (
        (result.stdout or "musicgen stream started").strip()
        + f"\nBroadcast waiting on backing track buffer ({float(stream_delay_seconds):.0f}s)."
    )


def stop_musicgen_stream():
    cmd = ["bash", "-lc", "cd /workspace/FluxRT && scripts/stop_musicgen_radio_plus_musicGEN.sh"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    # Also kill any orphaned writers in case the worker was started outside this UI.
    subprocess.run(
        [
            "bash",
            "-lc",
            "pkill -f 'run_musicgen_radio_plus_musicGEN.py' || true; "
            "pkill -f 'ffmpeg.*udp://127.0.0.1:5002' || true",
        ],
        capture_output=True,
        text=True,
    )
    msg = (result.stdout or result.stderr or "musicgen stopped").strip()
    set_status("musicgen stream stopped")
    return msg


def start_audiogen_sfx_stream(
    sfx_prompts: str,
    sfx_duration: float,
    sfx_interval: float,
    sfx_volume: float,
    sfx_seed: int,
    sfx_cpu_mode: bool,
):
    env = os.environ.copy()
    env["AUDIOGEN_SFX_PROMPTS"] = (sfx_prompts or default_sfx_prompts).strip()
    env["AUDIOGEN_SFX_DURATION"] = str(float(sfx_duration))
    env["AUDIOGEN_SFX_INTERVAL"] = str(float(sfx_interval))
    env["AUDIOGEN_SFX_VOLUME"] = str(float(sfx_volume))
    env["AUDIOGEN_SFX_SEED"] = str(int(sfx_seed))
    env["AUDIOGEN_SFX_CPU"] = "1" if sfx_cpu_mode else "0"
    env["AUDIOGEN_SFX_AUDIO_UDP_URL"] = "udp://127.0.0.1:5008?pkt_size=1316"
    cmd = ["bash", "-lc", "cd /workspace/FluxRT && scripts/start_audiogen_sfx_stream.sh"]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    msg = (result.stdout or result.stderr or "audiogen sfx start attempted").strip()
    if result.returncode != 0:
        set_status(f"audiogen sfx start failed: {msg}")
        return f"audiogen sfx start failed: {msg}"
    set_status("audiogen sfx stream started (udp 5008)")
    return msg


def stop_audiogen_sfx_stream():
    cmd = ["bash", "-lc", "cd /workspace/FluxRT && scripts/stop_audiogen_sfx_stream.sh"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    msg = (result.stdout or result.stderr or "audiogen sfx stopped").strip()
    set_status("audiogen sfx stopped")
    return msg


def refresh_audio_mix_bus_status():
    return "audio bus: running (udp://127.0.0.1:5006)" if _is_audio_mix_bus_running() else "audio bus: stopped"


def start_fanout_with_music(
    enable_youtube: bool,
    enable_twitch: bool,
    enable_facebook: bool,
    enable_quote_voice: bool,
    use_audio_bus: bool,
    music_mix_volume: float,
    tts_mix_volume: float,
    enable_sfx_input: bool,
    sfx_mix_volume: float,
    fanout_audio_bitrate: str,
    enable_local_monitor: bool,
):
    if not (enable_youtube or enable_twitch or enable_facebook):
        set_status("fanout start skipped: no platform enabled")
        return "fanout start skipped: enable at least one platform"

    if not _can_start_fanout_now():
        msg = (
            "fanout start blocked: waiting for AI-filtered processed frames "
            "(no passthrough allowed)"
        )
        set_status(msg)
        return msg

    env_path = os.path.join(_repo_root(), "scripts", "streaming", "rtmp_targets.env")
    env_targets = {
        "youtube": "",
        "twitch": "",
        "facebook": "",
    }
    try:
        if os.path.isfile(env_path):
            with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    key = k.strip()
                    value = v.strip().strip('"').strip("'")
                    if key == "YOUTUBE_RTMP_URL":
                        env_targets["youtube"] = value
                    elif key == "TWITCH_RTMP_URL":
                        env_targets["twitch"] = value
                    elif key == "FACEBOOK_RTMP_URL":
                        env_targets["facebook"] = value
    except Exception:
        pass

    missing = []
    if enable_youtube and not env_targets["youtube"]:
        missing.append("YouTube")
    if enable_twitch and not env_targets["twitch"]:
        missing.append("Twitch")
    if enable_facebook and not env_targets["facebook"]:
        missing.append("Facebook")

    if missing:
        msg = f"fanout start blocked: missing RTMP URL for {', '.join(missing)} in scripts/streaming/rtmp_targets.env"
        set_status(msg)
        return msg

    enable_youtube_str = "1" if enable_youtube else "0"
    enable_twitch_str = "1" if enable_twitch else "0"
    enable_facebook_str = "1" if enable_facebook else "0"
    selected_audio_bitrate = (fanout_audio_bitrate or "96k").strip()
    enable_monitor_str = "1" if enable_local_monitor else "0"

    if enable_local_monitor:
        monitor_msg = start_stream_monitor_http_ui()
        if "failed" in monitor_msg.lower():
            return f"fanout start blocked: local monitor requested but failed to start ({monitor_msg})"

    if use_audio_bus:
        bus_msg = start_audio_mix_bus_ui(
            music_mix_volume=music_mix_volume,
            tts_mix_volume=tts_mix_volume,
            audio_bitrate=selected_audio_bitrate,
            enable_sfx_input=enable_sfx_input,
            sfx_mix_volume=sfx_mix_volume,
        )
        if "failed" in bus_msg.lower():
            return f"fanout start blocked: audio bus requested but failed to start ({bus_msg})"

    enable_quote_voice_str = "0" if use_audio_bus else ("1" if enable_quote_voice else "0")
    audio_input_url = "udp://127.0.0.1:5006?pkt_size=1316" if use_audio_bus else "udp://127.0.0.1:5002?pkt_size=1316"

    launch_cmd = (
        "cd /workspace/FluxRT && "
        "scripts/streaming/stop_mediamtx_fanout.sh || true; "
        "scripts/streaming/stop_rtmp_fanout.sh || true; "
        f"ENABLE_YOUTUBE={enable_youtube_str} ENABLE_TWITCH={enable_twitch_str} ENABLE_FACEBOOK={enable_facebook_str} ENABLE_LOCAL_MONITOR={enable_monitor_str} ENABLE_TTS_OVERLAY={enable_quote_voice_str} AUDIO_SOURCE_MODE=url TTS_SOURCE_MODE=url "
        "INPUT_URL='udp://127.0.0.1:5000?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1' "
        f"AUDIO_INPUT_URL='{audio_input_url}' "
        "TTS_INPUT_URL='udp://127.0.0.1:5004?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1' "
        f"MUSIC_MIX_VOLUME={float(music_mix_volume)} TTS_MIX_VOLUME={float(tts_mix_volume)} "
        "FPS=8 OUTPUT_WIDTH=288 OUTPUT_HEIGHT=160 VIDEO_BITRATE=550k VIDEO_MAXRATE=550k VIDEO_BUFSIZE=1100k "
        f"AUDIO_BITRATE={selected_audio_bitrate} "
        "scripts/streaming/start_rtmp_fanout.sh"
    )
    try:
        result = subprocess.run(["bash", "-lc", launch_cmd], capture_output=True, text=True)
    except Exception as exc:
        msg = str(exc)
        set_status(f"fanout launch failed: {msg}")
        return f"fanout launch failed: {msg}"

    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "fanout launch failed").strip()
        set_status(f"fanout launch failed: {msg}")
        return f"fanout launch failed: {msg}"

    launch_msg = (result.stdout or "fanout launch started").strip()
    set_status("fanout started (direct RTMP loop)")
    if enable_local_monitor:
        launch_msg += f"\nLocal monitor: {stream_monitor_url()}"
    if use_audio_bus:
        if enable_sfx_input:
            return launch_msg + " (direct fanout + audio bus on udp 5006 + sfx udp 5008)"
        return launch_msg + " (direct fanout + audio bus on udp 5006)"
    return launch_msg + (" (quote voice mix enabled)" if enable_quote_voice else "")


def start_fanout_with_music_ui(
    enable_youtube: bool,
    enable_twitch: bool,
    enable_facebook: bool,
    enable_quote_voice: bool,
    use_audio_bus: bool,
    music_mix_volume: float,
    tts_mix_volume: float,
    enable_sfx_input: bool,
    sfx_mix_volume: float,
    fanout_audio_bitrate: str,
    enable_local_monitor: bool,
):
    msg = start_fanout_with_music(
        enable_youtube,
        enable_twitch,
        enable_facebook,
        enable_quote_voice,
        use_audio_bus,
        music_mix_volume,
        tts_mix_volume,
        enable_sfx_input,
        sfx_mix_volume,
        fanout_audio_bitrate,
        enable_local_monitor,
    )
    stamp = time.strftime("%H:%M:%S")
    ui_msg = f"[{stamp}] {msg}"
    return ui_msg, ui_msg


def _quote_tts_is_running() -> bool:
    with quote_tts_lock:
        return quote_tts_proc is not None and quote_tts_proc.poll() is None


def stop_quote_voice_stream():
    global quote_tts_proc
    with quote_tts_lock:
        proc = quote_tts_proc
        quote_tts_proc = None

    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # Also stop orphaned quote/TTS workers not tracked in this process.
    subprocess.run(
        [
            "bash",
            "-lc",
            "pkill -f 'run_quote_tts_from_json.py' || true; "
            "pkill -f 'run_edge_tts_quotes.py' || true; "
            "pkill -f 'ffmpeg.*udp://127.0.0.1:5004' || true",
        ],
        capture_output=True,
        text=True,
    )
    set_status("quote voice stopped")
    return "quote voice stopped"


def stop_all_stream_workers():
    # Main stream stop should stop audio workers too, so background generation does not linger.
    stop_video_source()
    stop_musicgen_stream()
    stop_audiogen_sfx_stream()
    stop_quote_voice_stream()
    stop_auto_prompt_rotator()


def generate_quote_data_inline(count: int, output_path: str):
    count = max(1, int(count))
    output_path = (output_path or "data/quotes/gemini_quotes.json").strip()
    cmd = [
        "bash",
        "-lc",
        (
            "cd /workspace/FluxRT && "
            f"python3 scripts/generate_quote_data_gemini.py --count {count} --output '{output_path}'"
        ),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "quote generation failed").strip()
        set_status(f"quote gen failed: {msg}")
        return f"quote gen failed: {msg}"
    set_status(f"quote data generated: {output_path}")
    return (result.stdout or f"quote data generated: {output_path}").strip()


def start_quote_voice_stream(
    quote_json_path: str,
    voice_name: str,
    interval_seconds: float,
    include_author: bool,
    shuffle_quotes: bool,
    loop_quotes: bool,
    reverb_enabled: bool,
    echo_enabled: bool,
    live_gemini: bool = False,
):
    global quote_tts_proc

    quote_json_path = (quote_json_path or "data/quotes/gemini_quotes.json").strip()
    if not quote_json_path:
        return "quote voice: quote path required"

    if _quote_tts_is_running():
        return "quote voice: already running"

    # Use edge TTS (free, no API key) with optional live Gemini auto-refresh
    cmd_parts = [
        "cd /workspace/FluxRT &&",
        "python3 -u scripts/run_edge_tts_quotes.py",
        f"--quotes '{quote_json_path}'",
        f"--interval {max(0.0, float(interval_seconds))}",
        "--random-voices",
        "--udp-url 'udp://127.0.0.1:5004?pkt_size=1316'",
    ]
    if not include_author:
        cmd_parts.append("--no-author")
    if shuffle_quotes:
        cmd_parts.append("--shuffle")
    if loop_quotes:
        cmd_parts.append("--loop")
    cmd_parts.append("--repeat-on-empty")
    cmd_parts.append("--cache-dir voices/quote_cache_edge")
    cmd_parts.append("--cache-size 4")
    if reverb_enabled:
        cmd_parts.append("--reverb")
        cmd_parts.append("--reverb-mix 0.35")
        cmd_parts.append("--reverb-decay 0.50")
        cmd_parts.append("--reverb-delay-ms 60")
    if echo_enabled:
        cmd_parts.append("--echo")
        cmd_parts.append("--echo-mix 0.45")
        cmd_parts.append("--echo-decay 0.65")
        cmd_parts.append("--echo-delay-ms 150")
    if live_gemini:
        cmd_parts.append("--live-gemini")
        cmd_parts.append("--gemini-refresh-threshold 5")
        cmd_parts.append("--gemini-refresh-count 10")
        cmd_parts.append("--gemini-timeout 30")

    full_cmd = " ".join(cmd_parts)
    try:
        with quote_tts_lock:
            quote_tts_proc = subprocess.Popen(["bash", "-lc", full_cmd])
    except Exception as exc:
        msg = str(exc)
        set_status(f"quote voice failed: {msg}")
        return f"quote voice failed: {msg}"

    mode = "live Gemini" if live_gemini else "static"
    set_status(f"quote voice started ({mode}, udp mix on 5004)")
    return f"quote voice started ({mode}, udp mix on 5004)"


def _musicgen_output_dirs() -> list[str]:
    repo = _repo_root()
    preferred = os.path.join(repo, default_musicgen_output_dir_name)
    dirs = []
    if os.path.isdir(preferred):
        dirs.append(preferred)
    try:
        for name in os.listdir(repo):
            path = os.path.join(repo, name)
            if name.startswith("musicgen_output_") and os.path.isdir(path) and path not in dirs:
                dirs.append(path)
    except Exception:
        pass
    return sorted(dirs, key=lambda p: os.path.getmtime(p), reverse=True)


def _latest_musicgen_clip() -> str | None:
    candidates = []
    for output_dir in _musicgen_output_dirs():
        candidates.extend(
            os.path.join(output_dir, name)
            for name in os.listdir(output_dir)
            if name.endswith(".wav") and name.startswith("musicgen_clip_")
        )
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def _read_marker(marker_name: str) -> str | None:
    for output_dir in _musicgen_output_dirs():
        marker_path = os.path.join(output_dir, marker_name)
        if not os.path.isfile(marker_path):
            continue
        try:
            path = open(marker_path, "r", encoding="utf-8").read().strip()
        except Exception:
            continue
        if path and os.path.isfile(path):
            return path
    return None


def _clip_index_from_path(path: str | None) -> int | None:
    if not path:
        return None
    name = os.path.basename(path)
    m = re.search(r"musicgen_clip_(\d+)\.wav$", name)
    if not m:
        return None
    return int(m.group(1))


def poll_musicgen_preview():
    now_clip = _read_marker("now_playing_path.txt")
    latest_clip = _latest_musicgen_clip()

    all_clips = []
    for output_dir in _musicgen_output_dirs():
        for name in os.listdir(output_dir):
            if name.startswith("musicgen_clip_") and name.endswith(".wav"):
                all_clips.append(os.path.join(output_dir, name))

    all_clips.sort(key=lambda p: _clip_index_from_path(p) or -1)

    now_value = now_clip or latest_clip
    now_idx = _clip_index_from_path(now_value)

    edit_value = None
    if now_idx is not None:
        for clip in all_clips:
            clip_idx = _clip_index_from_path(clip)
            if clip_idx is not None and clip_idx > now_idx:
                edit_value = clip
                break
    if edit_value is None:
        marker_edit = _read_marker("editing_path.txt")
        marker_idx = _clip_index_from_path(marker_edit)
        if marker_edit and marker_idx is not None and (now_idx is None or marker_idx > now_idx):
            edit_value = marker_edit

    if now_value is None and edit_value is None:
        return (
            gr.update(value=None),
            gr.update(value=None),
            "now feed: no clip",
            "edit feed: no clip",
        )

    now_text = f"now feed: {os.path.basename(now_value)}" if now_value else "now feed: waiting"
    edit_text = f"edit feed: {os.path.basename(edit_value)}" if edit_value else "edit feed: waiting for next clip"
    return gr.update(value=now_value), gr.update(value=edit_value), now_text, edit_text


def render_frame(frame_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray, bool]:
    if frame_bgr is None:
        return frame_bgr, frame_bgr, False

    overlay_active = is_overlay_enabled()
    display_input = apply_overlay(frame_bgr) if overlay_active else frame_bgr
    if is_filter_enabled():
        try:
            _, processed = process_frame(frame_bgr)
            processed = polish_processed_frame(processed)
            display_processed = apply_overlay(processed) if overlay_active else processed
            return display_input, display_processed, True
        except Exception as exc:
            set_status(f"filter degraded: {exc}")
            return display_input, display_input, False
    return display_input, display_input, False

def to_rgb(frame):
    if frame is None:
        return None
    return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)


def _placeholder_rgb(message: str, width: int = 640, height: int = 360) -> np.ndarray:
    """Return a glitching SMPTE color-bar placeholder frame."""
    import time
    rng = np.random.default_rng(int(time.time() * 1000) % 2**31)
    # Classic SMPTE color bars (BGR order for OpenCV)
    bar_colors = np.array([
        [192, 192, 192],   # white
        [192, 192, 0],     # yellow
        [0, 192, 192],     # cyan
        [0, 192, 0],       # green
        [192, 0, 192],     # magenta
        [192, 0, 0],       # red
        [0, 0, 192],       # blue
    ], dtype=np.uint8)
    bar_width = width // len(bar_colors)
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    for i, color in enumerate(bar_colors):
        x0 = i * bar_width
        x1 = width if i == len(bar_colors) - 1 else (i + 1) * bar_width
        canvas[:, x0:x1] = color

    # Glitch: random horizontal slices shifted and tinted
    num_glitches = rng.integers(3, 8)
    for _ in range(num_glitches):
        y0 = rng.integers(0, max(1, height - 8))
        y1 = min(height, y0 + rng.integers(4, 24))
        shift = rng.integers(-40, 41)
        slice_ = canvas[y0:y1, :].copy()
        if shift > 0:
            canvas[y0:y1, shift:] = slice_[:, :-shift]
            canvas[y0:y1, :shift] = rng.integers(0, 256, (y1 - y0, shift, 3), dtype=np.uint8)
        elif shift < 0:
            canvas[y0:y1, :shift] = slice_[:, -shift:]
            canvas[y0:y1, shift:] = rng.integers(0, 256, (y1 - y0, -shift, 3), dtype=np.uint8)
        # RGB channel offset tint
        tint = rng.integers(-30, 31, size=3)
        canvas[y0:y1] = np.clip(canvas[y0:y1].astype(np.int16) + tint, 0, 255).astype(np.uint8)

    # Scanlines
    canvas[::4, :] = (canvas[::4, :] * 0.7).astype(np.uint8)

    msg = re.sub(r"\s+", " ", (message or "waiting for frames").strip())
    if len(msg) > 64:
        msg = msg[:61] + "..."
    cv2.putText(
        canvas,
        msg,
        (24, height - 24),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (255, 255, 255),
        2,
    )
    return cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)


def process_frame(frame):
    global process_frame_counter
    if stream_processor is not None and not _workers_alive():
        reset_processor("worker down")

    _, local_input_tensor, local_output_tensor, local_resolution = get_processor()
    frame = crop_maximal_rectangle(
        frame, local_resolution["height"], local_resolution["width"]
    )

    with processor_lock:
        local_input_tensor.copy_from(frame)
        processed = local_output_tensor.to_numpy()

    process_frame_counter += 1
    if process_frame_counter % 90 == 0:
        p_min = int(processed.min())
        p_max = int(processed.max())
        p_mean = float(processed.mean())
        if p_max == 0:
            set_status("processed output is all zeros (inference likely not running)")
        print(
            f"[stream-demo] processed stats: min={p_min} max={p_max} mean={p_mean:.2f}"
        )

    return frame, processed


def set_prompt(prompt: str):
    sp, _, _, _ = get_processor()
    sp.set_prompt(prompt)


def apply_custom_image_prompt(prompt: str):
    prompt = (prompt or "").strip()
    if not prompt:
        msg = "image prompt: enter a custom prompt"
        set_status(msg)
        return msg

    stop_auto_prompt_rotator()
    set_prompt(prompt)
    msg = "image prompt: custom prompt applied"
    set_status(msg)
    return msg


def set_image_prompt_mode(mode: str):
    if mode == "Auto Prompt":
        msg = start_auto_prompt_rotator()
        return msg, gr.update(interactive=False), gr.update(interactive=False)

    stop_auto_prompt_rotator()
    msg = "image prompt: custom prompt mode"
    set_status(msg)
    return msg, gr.update(interactive=True), gr.update(interactive=True)


def set_auto_image_prompt_enabled(enabled: bool):
    if enabled:
        msg = start_auto_prompt_rotator()
        return msg, gr.update(interactive=False), gr.update(interactive=False)

    stop_auto_prompt_rotator()
    msg = "image prompt: manual text prompt mode"
    set_status(msg)
    return msg, gr.update(interactive=True), gr.update(interactive=True)


def start_auto_prompt_rotator():
    global prompt_rotator_proc
    if _prompt_rotator_is_running():
        set_status("auto prompt: already on")
        return "auto prompt: already on"

    log_path = "/tmp/fluxrt-prompt-rotator.log"
    cmd = [
        "bash",
        "-lc",
        "cd /workspace/FluxRT && uv run python -u scripts/streaming/rotate_flux_prompt.py --interval 75 --jitter 15",
    ]
    try:
        log_file = open(log_path, "a", encoding="utf-8")
        with prompt_rotator_lock:
            prompt_rotator_proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT)
    except Exception as exc:
        msg = str(exc)
        set_status(f"auto prompt failed: {msg}")
        return f"auto prompt failed: {msg}"

    set_status("auto prompt: on")
    return "auto prompt: on"


def stop_auto_prompt_rotator():
    global prompt_rotator_proc
    with prompt_rotator_lock:
        proc = prompt_rotator_proc
        prompt_rotator_proc = None
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()

    subprocess.run(
        ["bash", "-lc", "pkill -f 'scripts/streaming/rotate_flux_prompt.py' || true"],
        capture_output=True,
        text=True,
    )
    set_status("auto prompt: off")
    return "auto prompt: off"


def set_reference_image_ui(image):
    sp, _, _, _ = get_processor()
    try:
        if image is not None:
            image = np.asarray(image)
            if image.ndim == 2:
                image = np.stack([image] * 3, axis=-1)
            if image.shape[-1] == 4:
                image = image[:, :, :3]
            image = image.astype(np.uint8, copy=False)
        sp.set_reference_image(image)
        set_status("reference image updated")
    except Exception as exc:
        set_status(f"reference image unavailable: {exc}")


def set_status(value: str):
    global stream_status
    with stream_status_lock:
        stream_status = value


def get_status() -> str:
    with stream_status_lock:
        return stream_status


def get_worker_health() -> str:
    sp = stream_processor
    if sp is None:
        return "workers: not initialized"

    try:
        mi_proc = sp.model_inference_subprocess.process
        out_proc = sp.output_scheduler_subprocess.process
        mi_alive = bool(mi_proc is not None and mi_proc.is_alive())
        out_alive = bool(out_proc is not None and out_proc.is_alive())
        return f"workers: inference={mi_alive} scheduler={out_alive}"
    except Exception:
        return "workers: unknown"


def _local_video_loop(video_path: str, video_id: int):
    global current_input_frame, current_processed_frame
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        set_status("local open failed")
        return

    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    frame_time = 1.0 / fps
    _reset_broadcast_gate("source switched")
    set_status("local live")

    try:
        while True:
            with current_video_id_lock:
                if current_video_id != video_id:
                    break

            ok, frame = cap.read()
            if not ok:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue

            start = time.time()
            try:
                input_frame, processed, filter_active = render_frame(frame)
            except Exception as exc:
                set_status(f"local processing error: {exc}")
                time.sleep(0.1)
                continue

            with frame_lock:
                current_input_frame = to_rgb(input_frame)
                current_processed_frame = to_rgb(processed)

            candidate = processed if filter_active else None
            _push_processed_for_broadcast(candidate, fps=float(fps))

            time.sleep(max(0, frame_time - (time.time() - start)))
    finally:
        cap.release()


def _open_stream_capture(stream_url: str):
    # Prefer FFmpeg backend first for stream URLs.
    cap = cv2.VideoCapture(stream_url, cv2.CAP_FFMPEG)
    if cap.isOpened():
        return cap

    cap.release()
    return cv2.VideoCapture(stream_url)


def _start_stream_relay_if_needed(stream_url: str) -> str:
    global stream_relay_proc, stream_relay_input_url

    # Local files and direct UDP sources do not need relay.
    if stream_url.startswith("udp://") or os.path.isfile(stream_url):
        return stream_url

    with stream_relay_lock:
        if (
            stream_relay_proc is not None
            and stream_relay_proc.poll() is None
            and stream_relay_input_url == stream_url
        ):
            return stream_relay_output_url

        if stream_relay_proc is not None:
            try:
                stream_relay_proc.terminate()
                stream_relay_proc.wait(timeout=2)
            except Exception:
                try:
                    stream_relay_proc.kill()
                except Exception:
                    pass
            stream_relay_proc = None

        try:
            # Keep this relay resilient to transient HTTPS/TLS source drops.
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-fflags",
                "+genpts+discardcorrupt+igndts",
                "-use_wallclock_as_timestamps",
                "1",
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_at_eof",
                "1",
                "-reconnect_on_network_error",
                "1",
                "-reconnect_delay_max",
                "2",
                "-rw_timeout",
                "15000000",
                "-i",
                stream_url,
                "-an",
                "-c:v",
                "copy",
                "-muxdelay",
                "0",
                "-muxpreload",
                "0",
                "-max_interleave_delta",
                "0",
                "-flush_packets",
                "1",
                "-f",
                "mpegts",
                stream_relay_output_url,
            ]
            stream_relay_proc = subprocess.Popen(cmd)
            # If relay exits immediately (e.g. 404 upstream), avoid locking
            # capture onto a dead UDP relay endpoint.
            time.sleep(0.15)
            if stream_relay_proc.poll() is not None:
                stream_relay_proc = None
                stream_relay_input_url = ""
                set_status("stream relay failed, using direct source")
                return stream_url
            stream_relay_input_url = stream_url
            set_status("stream relay live")
            return stream_relay_output_url
        except Exception as exc:
            set_status(f"stream relay unavailable: {exc}")
            return stream_url


def _stop_stream_relay():
    global stream_relay_proc, stream_relay_input_url
    with stream_relay_lock:
        proc = stream_relay_proc
        stream_relay_proc = None
        stream_relay_input_url = ""

    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _stream_loop(stream_url: str, video_id: int):
    global current_input_frame, current_processed_frame
    capture_url = _start_stream_relay_if_needed(stream_url)
    cap = _open_stream_capture(capture_url)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    frame_time = 1.0 / BROADCAST_FPS
    consecutive_failures = 0
    last_reopen_at = 0.0
    switched_to_fallback = False
    _reset_broadcast_gate("source switched")

    try:
        while True:
            with current_video_id_lock:
                if current_video_id != video_id:
                    break

            if not cap.isOpened():
                set_status("stream reconnecting")
                consecutive_failures += 1
                keepalive_rgb = _placeholder_rgb("Stream source unavailable")
                _enqueue_broadcast_frame(to_bgr(keepalive_rgb), fps=BROADCAST_FPS)
                if (
                    consecutive_failures >= 120
                    and not switched_to_fallback
                    and stream_url != default_stream_url
                ):
                    switched_to_fallback = True
                    stream_url = default_stream_url
                    _stop_stream_relay()
                    set_status("stream source unavailable, switched to default")
                    consecutive_failures = 0
                    last_reopen_at = 0.0
                if consecutive_failures >= 20 and (time.time() - last_reopen_at) >= 2.0:
                    last_reopen_at = time.time()
                    cap.release()
                    capture_url = _start_stream_relay_if_needed(stream_url)
                    cap = _open_stream_capture(capture_url)
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    consecutive_failures = 0
                time.sleep(0.05)
                continue

            ok, frame = cap.read()
            if not ok:
                set_status("stream reconnecting")
                consecutive_failures += 1
                # Keep broadcast output alive while upstream reconnects so
                # fanout does not drop the RTMP session.
                keepalive_rgb = _placeholder_rgb("Stream reconnecting")
                _enqueue_broadcast_frame(to_bgr(keepalive_rgb), fps=BROADCAST_FPS)
                if (
                    consecutive_failures >= 120
                    and not switched_to_fallback
                    and stream_url != default_stream_url
                ):
                    switched_to_fallback = True
                    stream_url = default_stream_url
                    _stop_stream_relay()
                    set_status("stream source unavailable, switched to default")
                    consecutive_failures = 0
                    last_reopen_at = 0.0
                if consecutive_failures >= 20 and (time.time() - last_reopen_at) >= 2.0:
                    last_reopen_at = time.time()
                    cap.release()
                    capture_url = _start_stream_relay_if_needed(stream_url)
                    cap = _open_stream_capture(capture_url)
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    consecutive_failures = 0
                time.sleep(0.05)
                continue

            set_status("stream live")
            consecutive_failures = 0
            start = time.time()
            try:
                input_frame, processed, filter_active = render_frame(frame)
            except Exception as exc:
                set_status(f"stream processing error: {exc}")
                time.sleep(0.1)
                continue

            with frame_lock:
                current_input_frame = to_rgb(input_frame)
                current_processed_frame = to_rgb(processed)

            candidate = processed if filter_active else None
            _push_processed_for_broadcast(candidate, fps=BROADCAST_FPS)

            time.sleep(max(0, frame_time - (time.time() - start)))
    finally:
        cap.release()


def start_local_video(video_path: str | None):
    global current_video_id, current_input_frame, current_processed_frame

    with current_video_id_lock:
        current_video_id += 1
        my_id = current_video_id

    with frame_lock:
        current_input_frame = None
        current_processed_frame = None

    if not video_path:
        set_status("idle")
        return

    t = threading.Thread(target=_local_video_loop, args=(video_path, my_id), daemon=True)
    t.start()


def start_stream_video(stream_url: str | None):
    global current_video_id

    if stream_url is None:
        set_status("stream url missing")
        return

    stream_url = stream_url.strip()
    if not stream_url:
        set_status("stream url missing")
        return

    with current_video_id_lock:
        current_video_id += 1
        my_id = current_video_id

    set_status("stream connecting")
    t = threading.Thread(target=_stream_loop, args=(stream_url, my_id), daemon=True)
    t.start()


def stop_video_source():
    global current_video_id
    with current_video_id_lock:
        current_video_id += 1
    _stop_stream_relay()
    _reset_broadcast_gate("source stopped")
    set_status("idle")


def poll_video():
    with frame_lock:
        status = f"{get_status()} | {get_worker_health()}"
        input_frame = current_input_frame
        processed_frame = current_processed_frame

    if input_frame is None:
        input_frame = _placeholder_rgb(f"Input: {status}")
    if processed_frame is None:
        if current_input_frame is not None:
            processed_frame = input_frame
        else:
            processed_frame = _placeholder_rgb(f"Processed: {status}")
    elif isinstance(processed_frame, np.ndarray) and processed_frame.size > 0 and int(processed_frame.max()) == 0:
        if current_input_frame is not None:
            processed_frame = input_frame
        else:
            processed_frame = _placeholder_rgb(f"Processed warming up: {status}")

    return input_frame, processed_frame, status


def switch_mode(mode: str, request: gr.Request | None):
    if mode == "webcam":
        stop_video_source()

    webcam_visible = mode == "webcam"
    local_visible = mode == "local"
    stream_visible = mode == "stream"
    file_visible = mode == "local"

    return (
        gr.update(visible=webcam_visible),
        gr.update(visible=(local_visible or stream_visible)),
        gr.update(visible=webcam_visible),
        gr.update(visible=(local_visible or stream_visible)),
        gr.update(visible=file_visible),
        gr.update(visible=stream_visible),
        gr.update(active=(local_visible or stream_visible)),
    )


def process_webcam(frame):
    if frame is None:
        return None
    try:
        _, processed, filter_active = render_frame(to_bgr(frame))
        candidate = processed if filter_active else None
        _push_processed_for_broadcast(candidate, fps=BROADCAST_FPS)
        return to_rgb(processed)
    except Exception as exc:
        set_status(f"webcam processing error: {exc}")
        return frame


def main():
    global use_int8, stream_config_path
    parser = argparse.ArgumentParser(description="Run FluxRT Stream Gradio demo.")
    parser.add_argument("--int8", action="store_true", help="Enable int8 quantization")
    parser.add_argument(
        "--config-path",
        type=str,
        default="configs/stream_demo_config.json",
        help="Stream processor config path",
    )
    parser.add_argument(
        "--server-port", type=int, default=7862, help="Port for stream demo app"
    )
    parser.add_argument(
        "--server-name", type=str, default="0.0.0.0", help="Bind address"
    )
    parser.add_argument(
        "--local-video",
        type=str,
        default="",
        help="Optional local video path to start immediately instead of stream URL",
    )
    args, _ = parser.parse_known_args()
    use_int8 = args.int8
    stream_config_path = args.config_path

    # Clean stale workers left by prior crashes/restarts before initializing.
    _cleanup_global_orphan_workers()

    # Preload processor before UI launch so processed frames appear immediately
    # when a source starts (matches baseline run_gradio_demo behavior).
    try:
        set_status("initializing processor")
        get_processor()
        set_status("idle")
    except Exception as exc:
        set_status(f"processor init failed: {exc}")

    global repo_catalog_paths
    if get_status() == "initializing processor":
        set_status("idle")
    repo_catalog_paths = discover_repo_catalogs()
    catalog_choices = list(repo_catalog_paths.keys())
    default_catalog = "us_30a" if "us_30a" in repo_catalog_paths else (catalog_choices[0] if catalog_choices else None)
    initial_music_station_choices = load_music_stations_from_repo_files()
    default_music_choice = initial_music_station_choices[0] if initial_music_station_choices else default_music_station_name
    startup_local_video = (args.local_video or "").strip()
    startup_with_local = bool(startup_local_video and os.path.isfile(startup_local_video))

    with gr.Blocks() as demo:
        gr.Markdown("# Live Console")
        mode = gr.Radio(
            choices=["webcam", "local", "stream"],
            value="local" if startup_with_local else "stream",
            label="Mode",
        )

        with gr.Column(visible=False) as webcam_output_col:
            webcam_output = gr.Image(streaming=True, label="Processed stream")

        with gr.Column(visible=True) as source_output_col:
            source_output = gr.Image(label="Processed stream", value=_placeholder_rgb("Processed: initializing"))
            source_input = gr.Image(label="Input stream", value=_placeholder_rgb("Input: initializing"))
            source_status = gr.Textbox(label="Source status", value="initializing")

        source_timer = gr.Timer(value=0.04, active=True)

        with gr.Row(equal_height=False):
            with gr.Column(scale=3, min_width=420):
                with gr.Tabs():
                    with gr.Tab("Source"):
                        with gr.Column(visible=False) as webcam_input_col:
                            webcam_input = gr.Image(
                                sources=["webcam"],
                                streaming=True,
                                type="numpy",
                                label="Webcam",
                            )

                        with gr.Column(visible=True) as source_input_col:
                            with gr.Column(visible=startup_with_local) as local_controls:
                                video_file = gr.File(
                                    label="Choose local video",
                                    file_count="single",
                                    file_types=["video"],
                                    type="filepath",
                                )

                            with gr.Column(visible=(not startup_with_local)) as stream_controls:
                                with gr.Row():
                                    catalog_choice = gr.Dropdown(
                                        label="Catalog Group",
                                        choices=catalog_choices,
                                        value=default_catalog,
                                        allow_custom_value=False,
                                        filterable=True,
                                        interactive=True,
                                    )
                                    load_catalog_btn = gr.Button("Reload Catalog")
                                channel_choice = gr.Dropdown(
                                    label="Channel Choice",
                                    choices=[],
                                    allow_custom_value=True,
                                    filterable=True,
                                    interactive=True,
                                )
                                stream_url = gr.Textbox(
                                    label="Stream URL",
                                    value=default_stream_url,
                                    lines=1,
                                )
                                with gr.Row():
                                    apply_channel_btn = gr.Button("Use Selected")
                                    stream_start_btn = gr.Button("Start Stream", variant="primary")
                                    stream_stop_btn = gr.Button("Stop")

                                with gr.Accordion("Custom M3U", open=False):
                                    m3u_catalog = gr.Textbox(
                                        label="M3U Catalog",
                                        lines=10,
                                        value="",
                                        placeholder="#EXTM3U\n#EXTINF:-1,Channel Name\nhttps://example.com/live.m3u8",
                                    )

                    with gr.Tab("Visual"):
                        prompt = gr.Textbox(
                            value=default_prompt,
                            label="Manual Text Prompt",
                            lines=5,
                            interactive=True,
                        )
                        with gr.Row():
                            prompt_apply_btn = gr.Button("Enter Prompt", variant="primary")
                            auto_prompt_toggle = gr.Checkbox(value=False, label="Auto Prompt")
                            filter_toggle = gr.Checkbox(value=True, label="AI Filter")
                        with gr.Row():
                            overlay_on_btn = gr.Button("Overlay On")
                            overlay_off_btn = gr.Button("Overlay Off")
                        image_prompt_status = gr.Textbox(
                            label="Image Prompt Status",
                            value="image prompt: manual text prompt mode",
                            interactive=False,
                        )
                        ref_image_input = gr.Image(
                            label="Reference Image",
                            type="numpy",
                            sources=["upload"],
                            image_mode="RGB",
                        )
                        ref_image_apply_btn = gr.Button("Apply Image Prompt")

            with gr.Column(scale=4, min_width=520):
                with gr.Tabs():
                    with gr.Tab("Music"):
                        with gr.Row():
                            music_station_choice = gr.Dropdown(
                                label="Music Station",
                                choices=initial_music_station_choices,
                                value=default_music_choice,
                                allow_custom_value=False,
                                filterable=True,
                                interactive=True,
                                scale=3,
                            )
                            load_music_catalog_btn = gr.Button("Load Stations")
                            use_music_station_btn = gr.Button("Use Station")
                        musicgen_radio_url = gr.Textbox(
                            label="Radio URL",
                            value=music_station_map.get(default_music_choice, default_music_radio_url),
                            lines=1,
                        )
                        musicgen_base_prompt = gr.Textbox(
                            label="Music Prompt",
                            value=default_music_prompt,
                            lines=2,
                        )
                        with gr.Accordion("Music Station M3U", open=False):
                            music_station_m3u = gr.Textbox(
                                label="M3U Catalog",
                                lines=7,
                                value=default_music_station_m3u,
                                placeholder="#EXTM3U\n#EXTINF:-1,Station Name\nhttps://example.com/radio",
                            )
                        with gr.Row():
                            musicgen_sample_seconds = gr.Slider(
                                label="Sample Seconds",
                                minimum=6,
                                maximum=30,
                                value=8,
                                step=1,
                            )
                            musicgen_gen_seconds = gr.Slider(
                                label="Gen Seconds",
                                minimum=6,
                                maximum=30,
                                value=8,
                                step=1,
                            )
                            musicgen_top_k = gr.Slider(
                                label="Top-k",
                                minimum=0,
                                maximum=1000,
                                value=110,
                                step=1,
                            )
                        with gr.Row():
                            musicgen_top_p = gr.Slider(
                                label="Top-p",
                                minimum=0.05,
                                maximum=1.0,
                                value=0.82,
                                step=0.01,
                            )
                            musicgen_temperature = gr.Slider(
                                label="Temperature",
                                minimum=0.1,
                                maximum=2.0,
                                value=0.78,
                                step=0.05,
                            )
                            musicgen_guidance_scale = gr.Slider(
                                label="Guidance Scale",
                                minimum=1.0,
                                maximum=8.0,
                                value=3.4,
                                step=0.1,
                            )
                        with gr.Row():
                            musicgen_stream_delay = gr.Slider(
                                label="Stream Delay Seconds",
                                minimum=2,
                                maximum=240,
                                value=45,
                                step=1,
                            )
                            musicgen_crossfade_seconds = gr.Slider(
                                label="Crossfade Seconds",
                                minimum=0,
                                maximum=4,
                                value=1.2,
                                step=0.1,
                            )
                        with gr.Row():
                            musicgen_start_btn = gr.Button("Start Music", variant="primary")
                            musicgen_stop_btn = gr.Button("Stop Music")
                        musicgen_status = gr.Textbox(label="MusicGen Status", value="idle", lines=3)
                        with gr.Row():
                            musicgen_audio_now = gr.Audio(
                                label="Now Playing Feed",
                                type="filepath",
                                interactive=False,
                            )
                            musicgen_audio_edit = gr.Audio(
                                label="Editing / Next Feed",
                                type="filepath",
                                interactive=False,
                            )
                        with gr.Row():
                            musicgen_now_status = gr.Textbox(label="Now Feed Status", value="now feed: idle", lines=1)
                            musicgen_edit_status = gr.Textbox(label="Edit Feed Status", value="edit feed: idle", lines=1)
                        musicgen_audio_preview = gr.Audio(
                            label="Latest Clip",
                            type="filepath",
                            interactive=False,
                            visible=False,
                        )

                    with gr.Tab("Quotes"):
                        with gr.Row():
                            quote_json_path = gr.Textbox(
                                label="Quote JSON Path",
                                value="data/quotes/gemini_quotes.json",
                                lines=1,
                            )
                            quote_voice = gr.Dropdown(
                                label="Voice",
                                choices=voice_choices,
                                value=voice_choices[0],
                                allow_custom_value=True,
                                interactive=True,
                            )
                        with gr.Row():
                            quote_interval = gr.Slider(
                                label="Quote Interval Seconds",
                                minimum=0,
                                maximum=120,
                                value=15,
                                step=1,
                            )
                            quote_gen_count = gr.Slider(
                                label="Generate Quote Count",
                                minimum=1,
                                maximum=200,
                                value=30,
                                step=1,
                            )
                        with gr.Row():
                            quote_include_author = gr.Checkbox(value=False, label="Speak Author")
                            quote_shuffle = gr.Checkbox(value=True, label="Shuffle")
                            quote_loop = gr.Checkbox(value=True, label="Loop")
                            quote_reverb = gr.Checkbox(value=True, label="Reverb")
                            quote_echo = gr.Checkbox(value=True, label="Last-Word Echo")
                            quote_live_gemini = gr.Checkbox(value=True, label="Live Gemini", info="Auto-generate fresh quotes via Gemini 2.5 Flash")
                        with gr.Row():
                            quote_generate_btn = gr.Button("Generate Quote Data")
                            quote_start_btn = gr.Button("Start Quote Voice", variant="primary")
                            quote_stop_btn = gr.Button("Stop Quote Voice")
                        quote_status = gr.Textbox(label="Quote Voice Status", value="idle", lines=3)

                    with gr.Tab("SFX"):
                        sfx_prompts = gr.Textbox(
                            label="AudioGen Prompts",
                            value=default_sfx_prompts,
                            lines=5,
                        )
                        with gr.Row():
                            sfx_duration = gr.Slider(
                                label="SFX Seconds",
                                minimum=1,
                                maximum=8,
                                value=3,
                                step=0.5,
                            )
                            sfx_interval = gr.Slider(
                                label="Interval Seconds",
                                minimum=5,
                                maximum=180,
                                value=35,
                                step=1,
                            )
                            sfx_volume = gr.Slider(
                                label="SFX Pre-Bus Volume",
                                minimum=0.0,
                                maximum=1.0,
                                value=0.35,
                                step=0.05,
                            )
                            sfx_seed = gr.Number(
                                label="Seed",
                                value=4242,
                                precision=0,
                            )
                            sfx_cpu_mode = gr.Checkbox(value=False, label="CPU Mode")
                        with gr.Row():
                            sfx_start_btn = gr.Button("Start AudioGen SFX", variant="primary")
                            sfx_stop_btn = gr.Button("Stop AudioGen SFX")
                        sfx_status = gr.Textbox(label="AudioGen SFX Status", value="idle", lines=3)

                    with gr.Tab("Broadcast"):
                        with gr.Row():
                            fanout_enable_twitch = gr.Checkbox(value=True, label="Twitch")
                            fanout_enable_youtube = gr.Checkbox(value=False, label="YouTube")
                            fanout_enable_facebook = gr.Checkbox(value=False, label="Facebook")
                            fanout_enable_quote_voice = gr.Checkbox(value=True, label="Quote Voice")
                            fanout_enable_sfx = gr.Checkbox(value=False, label="SFX")
                            fanout_use_audio_bus = gr.Checkbox(value=True, label="Audio Bus")
                            fanout_enable_monitor = gr.Checkbox(value=True, label="Local Monitor")
                        with gr.Row():
                            audio_bus_music_volume = gr.Slider(
                                label="Bus Music Volume",
                                minimum=0.0,
                                maximum=2.0,
                                value=0.85,
                                step=0.05,
                            )
                            audio_bus_tts_volume = gr.Slider(
                                label="Bus Quote Volume",
                                minimum=0.0,
                                maximum=4.0,
                                value=1.55,
                                step=0.1,
                            )
                            audio_bus_sfx_volume = gr.Slider(
                                label="Bus SFX Volume",
                                minimum=0.0,
                                maximum=2.0,
                                value=0.25,
                                step=0.05,
                            )
                            audio_bus_bitrate = gr.Dropdown(
                                label="Audio Bitrate",
                                choices=["96k", "128k", "160k", "192k"],
                                value="128k",
                                allow_custom_value=False,
                                interactive=True,
                            )
                        with gr.Row():
                            fanout_music_btn = gr.Button("Start Fanout", variant="primary")
                            audio_bus_start_btn = gr.Button("Start Audio Bus")
                            audio_bus_stop_btn = gr.Button("Stop Audio Bus")
                        with gr.Row():
                            monitor_start_btn = gr.Button("Start Monitor")
                            monitor_stop_btn = gr.Button("Stop Monitor")
                        with gr.Row():
                            music_status_light = gr.HTML(_status_light_html("Music", False))
                            quote_status_light = gr.HTML(_status_light_html("Quote Voice", False))
                            sfx_status_light = gr.HTML(_status_light_html("SFX", False))
                            audio_bus_status_light = gr.HTML(_status_light_html("Audio Bus", False))
                        fanout_status = gr.Textbox(label="Fanout Status", value="idle", lines=3)
                        audio_bus_status = gr.Textbox(label="Audio Bus Status", value=refresh_audio_mix_bus_status(), lines=2)
                        monitor_status = gr.Textbox(label="Monitor Status", value=refresh_stream_monitor_status(), lines=3)

        musicgen_timer = gr.Timer(value=2.0, active=True)

        mode.change(
            switch_mode,
            inputs=mode,
            outputs=[
                webcam_output_col,
                source_output_col,
                webcam_input_col,
                source_input_col,
                local_controls,
                stream_controls,
                source_timer,
            ],
        )

        webcam_input.stream(
            process_webcam,
            inputs=webcam_input,
            outputs=[webcam_output],
            stream_every=0.04,
            concurrency_limit=1,
            queue=False,
        )

        video_file.change(start_local_video, inputs=video_file, outputs=None)

        if default_catalog is not None:
            initial_channels_update, initial_url_update = load_repo_catalog_choice(default_catalog)
            channel_choice.value = initial_channels_update["value"]
            # Keep the explicit default URL unless the textbox is empty.
            if "value" in initial_url_update and not (stream_url.value or "").strip():
                stream_url.value = initial_url_update["value"]

        catalog_choice.change(
            load_repo_catalog_choice,
            inputs=[catalog_choice],
            outputs=[channel_choice, stream_url],
        )

        load_catalog_btn.click(
            load_repo_catalog_choice,
            inputs=[catalog_choice],
            outputs=[channel_choice, stream_url],
        )

        m3u_catalog.change(
            load_m3u_catalog,
            inputs=[m3u_catalog],
            outputs=[channel_choice, stream_url],
        )

        apply_channel_btn.click(
            apply_channel_choice,
            inputs=[channel_choice],
            outputs=[stream_url],
        )

        channel_choice.change(
            apply_channel_choice,
            inputs=[channel_choice],
            outputs=[stream_url],
        )

        stream_start_btn.click(
            start_stream_video,
            inputs=[stream_url],
            outputs=None,
        )

        stream_stop_btn.click(stop_all_stream_workers, outputs=None)

        source_timer.tick(
            poll_video,
            outputs=[source_input, source_output, source_status],
        )

        auto_prompt_toggle.change(
            set_auto_image_prompt_enabled,
            inputs=[auto_prompt_toggle],
            outputs=[image_prompt_status, prompt, prompt_apply_btn],
        )

        prompt_apply_btn.click(
            apply_custom_image_prompt,
            inputs=[prompt],
            outputs=[image_prompt_status],
        )

        # Prompt auto-change updates can occasionally arrive as empty queue
        # payloads; use explicit apply action above for stability.
        filter_toggle.change(set_filter_enabled, inputs=filter_toggle, outputs=None)
        overlay_on_btn.click(lambda: set_overlay_enabled(True), outputs=None)
        overlay_off_btn.click(lambda: set_overlay_enabled(False), outputs=None)

        musicgen_start_btn.click(
            start_musicgen_stream,
            inputs=[
                musicgen_radio_url,
                musicgen_base_prompt,
                musicgen_sample_seconds,
                musicgen_gen_seconds,
                musicgen_top_k,
                musicgen_top_p,
                musicgen_temperature,
                musicgen_guidance_scale,
                musicgen_stream_delay,
                musicgen_crossfade_seconds,
            ],
            outputs=[musicgen_status],
        )

        musicgen_stop_btn.click(
            stop_musicgen_stream,
            outputs=[musicgen_status],
        )

        sfx_start_btn.click(
            start_audiogen_sfx_stream,
            inputs=[sfx_prompts, sfx_duration, sfx_interval, sfx_volume, sfx_seed, sfx_cpu_mode],
            outputs=[sfx_status],
        )

        sfx_stop_btn.click(
            stop_audiogen_sfx_stream,
            outputs=[sfx_status],
        )

        fanout_music_btn.click(
            start_fanout_with_music_ui,
            inputs=[
                fanout_enable_youtube,
                fanout_enable_twitch,
                fanout_enable_facebook,
                fanout_enable_quote_voice,
                fanout_use_audio_bus,
                audio_bus_music_volume,
                audio_bus_tts_volume,
                fanout_enable_sfx,
                audio_bus_sfx_volume,
                audio_bus_bitrate,
                fanout_enable_monitor,
            ],
            outputs=[fanout_status, musicgen_status],
        )

        audio_bus_start_btn.click(
            start_audio_mix_bus_ui,
            inputs=[audio_bus_music_volume, audio_bus_tts_volume, audio_bus_bitrate, fanout_enable_sfx, audio_bus_sfx_volume],
            outputs=[audio_bus_status],
        )

        audio_bus_stop_btn.click(
            stop_audio_mix_bus_ui,
            outputs=[audio_bus_status],
        )

        monitor_start_btn.click(
            start_stream_monitor_http_ui,
            outputs=[monitor_status],
        )

        monitor_stop_btn.click(
            stop_stream_monitor_http_ui,
            outputs=[monitor_status],
        )

        quote_generate_btn.click(
            generate_quote_data_inline,
            inputs=[quote_gen_count, quote_json_path],
            outputs=[quote_status],
        )

        quote_start_btn.click(
            start_quote_voice_stream,
            inputs=[
                quote_json_path,
                quote_voice,
                quote_interval,
                quote_include_author,
                quote_shuffle,
                quote_loop,
                quote_reverb,
                quote_echo,
                quote_live_gemini,
            ],
            outputs=[quote_status],
        )

        quote_stop_btn.click(
            stop_quote_voice_stream,
            outputs=[quote_status],
        )

        load_music_catalog_btn.click(
            load_music_station_catalog,
            inputs=[music_station_m3u],
            outputs=[music_station_choice, musicgen_radio_url],
        )

        music_station_m3u.change(
            load_music_station_catalog,
            inputs=[music_station_m3u],
            outputs=[music_station_choice, musicgen_radio_url],
        )

        use_music_station_btn.click(
            apply_music_station_choice,
            inputs=[music_station_choice],
            outputs=[musicgen_radio_url],
        )

        music_station_choice.change(
            apply_music_station_choice,
            inputs=[music_station_choice],
            outputs=[musicgen_radio_url],
        )

        musicgen_timer.tick(
            poll_musicgen_preview,
            outputs=[musicgen_audio_now, musicgen_audio_edit, musicgen_now_status, musicgen_edit_status],
        )

        musicgen_timer.tick(
            poll_audio_service_lights,
            outputs=[music_status_light, quote_status_light, sfx_status_light, audio_bus_status_light],
        )

        musicgen_timer.tick(
            refresh_audio_mix_bus_status,
            outputs=[audio_bus_status],
        )

        musicgen_timer.tick(
            refresh_stream_monitor_status,
            outputs=[monitor_status],
        )

        # Reference image updates are applied manually when needed to avoid
        # empty-input queue events crashing the handler.
        ref_image_apply_btn.click(
            set_reference_image_ui,
            inputs=[ref_image_input],
            outputs=None,
        )

    # Only auto-start an explicit local file source.
    # Avoid auto-starting remote stream input so webcam mode stays responsive.
    if startup_with_local:
        start_local_video(startup_local_video)
        set_status("local live")
    else:
        set_status("idle")

    # Auto-start IPTV stream on RunPod so video pipeline begins without manual UI clicks.
    if os.getenv("AUTO_START_STREAM", "0") == "1":
        auto_url = os.getenv("STREAM_URL", default_stream_url).strip()
        auto_delay = float(os.getenv("AUTO_START_DELAY", "90"))

        def _auto_start_stream():
            time.sleep(auto_delay)
            start_stream_video(auto_url)
            set_status(f"auto stream started: {auto_url[:80]}")

        threading.Thread(target=_auto_start_stream, daemon=True).start()

    # Queue can stall live webcam callbacks over some tunnels; keep it
    # optional and off by default for responsive webcam processing.
    enable_queue = os.getenv("GRADIO_ENABLE_QUEUE", "0") == "1"
    app = demo
    if enable_queue:
        app = demo.queue(default_concurrency_limit=8)

    # Enable Gradio share tunnel if requested (creates a public URL)
    share_tunnel = os.getenv("GRADIO_SHARE", "0") == "1"

    app.launch(
        server_name=args.server_name,
        server_port=args.server_port,
        share=share_tunnel,
        css=APP_CSS,
    )


if __name__ == "__main__":
    main()
