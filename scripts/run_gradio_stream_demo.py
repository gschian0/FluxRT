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

default_prompt = "claymation"
default_stream_url = "https://streamer1.connectto.com/AABC_WEB_1201/index.m3u8"
default_music_radio_url = "http://london-dedicated.myautodj.com:8862/stream"
default_music_station_name = "AutoDJ London"

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
    return _is_process_running("run_quote_tts_from_json.py")


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
    bus_running = _is_audio_mix_bus_running()
    return (
        _status_light_html("Music", music_running),
        _status_light_html("Quote Voice", quote_running),
        _status_light_html("Audio Bus", bus_running),
    )


def start_audio_mix_bus_ui(
    music_mix_volume: float,
    tts_mix_volume: float,
    audio_bitrate: str = "128k",
):
    env = os.environ.copy()
    env["MUSIC_MIX_VOLUME"] = str(float(music_mix_volume))
    env["TTS_MIX_VOLUME"] = str(float(tts_mix_volume))
    env["AUDIO_BITRATE"] = (audio_bitrate or "128k").strip()
    cmd = ["bash", "-lc", "cd /home/gschi/FluxRT && scripts/streaming/start_audio_mix_bus.sh"]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "audio bus start failed").strip()
        set_status(f"audio bus start failed: {msg}")
        return f"audio bus start failed: {msg}"
    set_status("audio mix bus started (udp 5006)")
    return (result.stdout or "audio mix bus started").strip()


def stop_audio_mix_bus_ui():
    cmd = ["bash", "-lc", "cd /home/gschi/FluxRT && scripts/streaming/stop_audio_mix_bus.sh"]
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
    while True:
        payload = None
        with broadcast_send_queue_lock:
            if broadcast_send_queue:
                payload = broadcast_send_queue.popleft()
        if payload is None:
            time.sleep(0.002)
            continue

        frame_to_send, fps = payload
        _write_to_udp(frame_to_send, fps=int(max(1, fps)))


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
    fps_val = max(1.0, float(fps or 25.0))
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


def _get_udp_writer(width, height, fps=25):
    global udp_writer, udp_writer_dims
    with udp_writer_lock:
        if udp_writer is not None and udp_writer_dims != (width, height):
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
                '-g', '50',
                '-keyint_min', '50',
                '-sc_threshold', '0',
                '-x264-params', 'repeat-headers=1:keyint=50:min-keyint=50:scenecut=0',
                '-pix_fmt', 'yuv420p',
                '-mpegts_flags', '+resend_headers',
                '-muxdelay', '0',
                '-muxpreload', '0',
                '-flush_packets', '1',
                '-f', 'mpegts',
                'udp://127.0.0.1:5000?pkt_size=1316'
            ]
            udp_writer = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            udp_writer_dims = (width, height)
        return udp_writer


def _write_to_udp(frame, fps=25):
    if frame is None:
        return
    h, w = frame.shape[:2]
    writer = _get_udp_writer(w, h, fps)
    if writer and writer.stdin:
        try:
            writer.stdin.write(frame.tobytes())
        except Exception:
            pass


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
        if "/home/gschi/FluxRT/.venv/bin/python3" not in cmd:
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
]

_current_show_str = SHOW_NAMES[0]
_show_last_changed = time.time()

def add_tv_overlay(frame_bgr: np.ndarray) -> np.ndarray:
    global _current_show_str, _show_last_changed
    now = time.time()
    
    if now - _show_last_changed > 15.0:  # Change every 15 seconds
        _current_show_str = random.choice(SHOW_NAMES)
        _show_last_changed = now
        
    out = frame_bgr.copy()
    h, w = out.shape[:2]

    # Lower-left title package.
    overlay = out.copy()
    cv2.rectangle(overlay, (16, h - 78), (w - 16, h - 18), (0, 0, 0), -1)
    cv2.rectangle(overlay, (26, h - 66), (110, h - 28), (0, 0, 220), -1)
    cv2.addWeighted(overlay, 0.6, out, 0.4, 0, out)

    cv2.putText(out, "LIVE", (36, h - 40), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (255, 255, 255), 2)
    cv2.putText(out, _current_show_str, (126, h - 38), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)

    # Upper-right numeric clock.
    current_time_str = time.strftime("%H:%M:%S")
    tw, th = cv2.getTextSize(current_time_str, cv2.FONT_HERSHEY_SIMPLEX, 0.72, 2)[0]
    x2 = w - 18
    x1 = max(12, x2 - tw - 24)
    y1 = 16
    y2 = y1 + th + 18
    cv2.rectangle(out, (x1, y1), (x2, y2), (0, 0, 0), -1)
    cv2.rectangle(out, (x1, y1), (x2, y2), (70, 70, 70), 1)
    cv2.putText(out, current_time_str, (x1 + 12, y2 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (235, 235, 235), 2)
    
    return out


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
    env["MUSICGEN_BASE_PROMPT"] = (base_prompt or "experimental electronic sound art").strip()
    env["MUSICGEN_SAMPLE_SECONDS"] = str(int(sample_seconds))
    env["MUSICGEN_GEN_SECONDS"] = str(int(gen_seconds))
    env["MUSICGEN_TOP_K"] = str(int(top_k))
    env["MUSICGEN_TOP_P"] = str(float(top_p))
    env["MUSICGEN_TEMPERATURE"] = str(float(temperature))
    env["MUSICGEN_GUIDANCE_SCALE"] = str(float(guidance_scale))
    env["MUSICGEN_PARALLEL_CLIPS"] = "2"
    env["MUSICGEN_SEED"] = "-1"
    env["MUSICGEN_STREAM_DELAY_SECONDS"] = str(float(stream_delay_seconds))
    env["MUSICGEN_CROSSFADE_SECONDS"] = str(float(crossfade_seconds))
    env["MUSICGEN_PAUSE_SECONDS"] = "0"
    env["MUSICGEN_BOOTSTRAP_CLIPS"] = "24"
    env["MUSICGEN_AUDIO_UDP_URL"] = "udp://127.0.0.1:5002?pkt_size=1316"

    cmd = ["bash", "-lc", "cd /home/gschi/FluxRT && scripts/start_musicgen_radio_plus_musicGEN.sh"]
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
    cmd = ["bash", "-lc", "cd /home/gschi/FluxRT && scripts/stop_musicgen_radio_plus_musicGEN.sh"]
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
    fanout_audio_bitrate: str,
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

    if use_audio_bus:
        bus_msg = start_audio_mix_bus_ui(
            music_mix_volume=music_mix_volume,
            tts_mix_volume=tts_mix_volume,
            audio_bitrate=selected_audio_bitrate,
        )
        if "failed" in bus_msg.lower():
            return f"fanout start blocked: audio bus requested but failed to start ({bus_msg})"

    enable_quote_voice_str = "0" if use_audio_bus else ("1" if enable_quote_voice else "0")
    audio_input_url = "udp://127.0.0.1:5006?pkt_size=1316" if use_audio_bus else "udp://127.0.0.1:5002?pkt_size=1316"

    launch_cmd = (
        "cd /home/gschi/FluxRT && "
        "scripts/streaming/stop_mediamtx_fanout.sh || true; "
        "scripts/streaming/stop_rtmp_fanout.sh || true; "
        f"ENABLE_YOUTUBE={enable_youtube_str} ENABLE_TWITCH={enable_twitch_str} ENABLE_FACEBOOK={enable_facebook_str} ENABLE_TTS_OVERLAY={enable_quote_voice_str} AUDIO_SOURCE_MODE=url TTS_SOURCE_MODE=url "
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
    if use_audio_bus:
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
    fanout_audio_bitrate: str,
):
    msg = start_fanout_with_music(
        enable_youtube,
        enable_twitch,
        enable_facebook,
        enable_quote_voice,
        use_audio_bus,
        music_mix_volume,
        tts_mix_volume,
        fanout_audio_bitrate,
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
    stop_quote_voice_stream()


def generate_quote_data_inline(count: int, output_path: str):
    count = max(1, int(count))
    output_path = (output_path or "data/quotes/diffusiongemma_quotes.json").strip()
    cmd = [
        "bash",
        "-lc",
        (
            "cd /home/gschi/FluxRT && "
            f"uv run scripts/generate_quote_data_diffusiongemma.py --count {count} --output '{output_path}'"
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
):
    global quote_tts_proc

    quote_json_path = (quote_json_path or "data/quotes/diffusiongemma_quotes.json").strip()
    if not quote_json_path:
        return "quote voice: quote path required"

    if _quote_tts_is_running():
        return "quote voice: already running"

    cmd_parts = [
        "cd /home/gschi/FluxRT &&",
        "uv run scripts/run_quote_tts_from_json.py",
        f"--quotes '{quote_json_path}'",
        f"--voice '{voice_name}'",
        f"--interval {max(0.0, float(interval_seconds))}",
        "--udp-url 'udp://127.0.0.1:5004?pkt_size=1316'",
    ]
    if not include_author:
        cmd_parts.append("--no-author")
    if shuffle_quotes:
        cmd_parts.append("--shuffle")
    if loop_quotes:
        cmd_parts.append("--loop")
    # Always auto-refresh so process never exits and kills UDP 5004.
    # refresh-threshold=10 starts generating while 10 quotes remain (buys time).
    # repeat-on-empty is the failsafe if generation is slow or fails.
    cmd_parts.append("--auto-refresh")
    cmd_parts.append("--refresh-threshold 10")
    cmd_parts.append("--refresh-count 50")
    cmd_parts.append("--repeat-on-empty")
    # Keep quote voice FX consistently on to avoid dry/cut-in sounding speech.
    cmd_parts.append("--reverb")
    cmd_parts.append("--last-word-echo")

    full_cmd = " ".join(cmd_parts)
    try:
        with quote_tts_lock:
            quote_tts_proc = subprocess.Popen(["bash", "-lc", full_cmd])
    except Exception as exc:
        msg = str(exc)
        set_status(f"quote voice failed: {msg}")
        return f"quote voice failed: {msg}"

    set_status("quote voice started (udp mix on 5004)")
    return "quote voice started (udp mix on 5004)"


def _latest_musicgen_clip() -> str | None:
    output_dir = os.path.join(_repo_root(), "musicgen_output_plus_musicGEN")
    if not os.path.isdir(output_dir):
        return None

    candidates = [
        os.path.join(output_dir, name)
        for name in os.listdir(output_dir)
        if name.endswith(".wav") and name.startswith("musicgen_clip_")
    ]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def _read_marker(marker_name: str) -> str | None:
    marker_path = os.path.join(_repo_root(), "musicgen_output_plus_musicGEN", marker_name)
    if not os.path.isfile(marker_path):
        return None
    try:
        path = open(marker_path, "r", encoding="utf-8").read().strip()
    except Exception:
        return None
    if not path:
        return None
    return path if os.path.isfile(path) else None


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

    output_dir = os.path.join(_repo_root(), "musicgen_output_plus_musicGEN")
    all_clips = []
    if os.path.isdir(output_dir):
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

    frame_with_overlay = apply_overlay(frame_bgr) if is_overlay_enabled() else frame_bgr
    if is_filter_enabled():
        try:
            _, processed = process_frame(frame_with_overlay)
            return frame_with_overlay, processed, True
        except Exception as exc:
            set_status(f"filter degraded: {exc}")
            return frame_with_overlay, frame_with_overlay, False
    return frame_with_overlay, frame_with_overlay, False

def to_rgb(frame):
    if frame is None:
        return None
    return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)


def _placeholder_rgb(message: str, width: int = 640, height: int = 360) -> np.ndarray:
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    cv2.rectangle(canvas, (0, 0), (width, height), (12, 12, 12), -1)
    cv2.putText(
        canvas,
        "FluxRT Loading",
        (24, 56),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (210, 210, 210),
        2,
    )

    msg = re.sub(r"\s+", " ", (message or "waiting for frames").strip())
    if len(msg) > 64:
        msg = msg[:61] + "..."
    cv2.putText(
        canvas,
        msg,
        (24, 104),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (160, 220, 255),
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
    frame_time = 1.0 / 25
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
                _enqueue_broadcast_frame(to_bgr(keepalive_rgb), fps=8.0)
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
                _enqueue_broadcast_frame(to_bgr(keepalive_rgb), fps=8.0)
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
            _push_processed_for_broadcast(candidate, fps=25.0)

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
        _push_processed_for_broadcast(candidate, fps=25.0)
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
        "--server-port", type=int, default=7861, help="Port for stream demo app"
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

        with gr.Row():
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
                    with gr.Row():
                        channel_choice = gr.Dropdown(
                            label="Channel Choice",
                            choices=[],
                            allow_custom_value=True,
                            filterable=True,
                            interactive=True,
                        )
                    stream_url = gr.Textbox(
                        label="Stream URL (custom field)",
                        value=default_stream_url,
                        lines=1,
                    )
                    with gr.Row():
                        apply_channel_btn = gr.Button("Use Selected Channel")
                        stream_start_btn = gr.Button("Start Stream")
                        stream_stop_btn = gr.Button("Stop")

                    with gr.Accordion("Paste Custom M3U (optional)", open=False):
                        m3u_catalog = gr.Textbox(
                            label="Custom M3U Catalog",
                            lines=10,
                            value="",
                            placeholder="#EXTM3U\n#EXTINF:-1,Channel Name\nhttps://example.com/live.m3u8",
                        )

            prompt = gr.Textbox(
                value=default_prompt,
                label="Prompt",
                lines=3,
                interactive=True,
            )
            prompt_apply_btn = gr.Button("Enter New Prompt")
            filter_toggle = gr.Checkbox(value=True, label="Enable AI Filter")
            with gr.Row():
                overlay_on_btn = gr.Button("Overlay On")
                overlay_off_btn = gr.Button("Overlay Off")

            with gr.Row():
                musicgen_radio_url = gr.Textbox(
                    label="MusicGen Radio URL",
                    value=music_station_map.get(default_music_choice, default_music_radio_url),
                    lines=1,
                    scale=3,
                )
                musicgen_base_prompt = gr.Textbox(
                    label="Music Prompt",
                    value="experimental electronic sound art for an internet installation",
                    lines=1,
                    scale=3,
                )
            with gr.Row():
                music_station_choice = gr.Dropdown(
                    label="Music Station",
                    choices=initial_music_station_choices,
                    value=default_music_choice,
                    allow_custom_value=False,
                    filterable=True,
                    interactive=True,
                )
                load_music_catalog_btn = gr.Button("Load Music Stations")
                use_music_station_btn = gr.Button("Use Selected Station")
            with gr.Accordion("Paste Music Station M3U", open=False):
                music_station_m3u = gr.Textbox(
                    label="Music M3U Catalog",
                    lines=8,
                    value="#EXTM3U\n#EXTINF:-1,AutoDJ London\nhttp://london-dedicated.myautodj.com:8862/stream",
                    placeholder="#EXTM3U\n#EXTINF:-1,Station Name\nhttps://example.com/radio",
                )
            with gr.Row():
                musicgen_sample_seconds = gr.Slider(
                    label="Sample Seconds",
                    minimum=6,
                    maximum=30,
                    value=12,
                    step=1,
                )
                musicgen_gen_seconds = gr.Slider(
                    label="Gen Seconds",
                    minimum=6,
                    maximum=30,
                    value=12,
                    step=1,
                )
                musicgen_top_k = gr.Slider(
                    label="Top-k",
                    minimum=0,
                    maximum=1000,
                    value=250,
                    step=1,
                )
            with gr.Row():
                musicgen_top_p = gr.Slider(
                    label="Top-p",
                    minimum=0.05,
                    maximum=1.0,
                    value=0.95,
                    step=0.01,
                )
                musicgen_temperature = gr.Slider(
                    label="Temperature",
                    minimum=0.1,
                    maximum=2.0,
                    value=1.0,
                    step=0.05,
                )
                musicgen_guidance_scale = gr.Slider(
                    label="Guidance Scale",
                    minimum=1.0,
                    maximum=8.0,
                    value=3.0,
                    step=0.1,
                )
            with gr.Row():
                musicgen_stream_delay = gr.Slider(
                    label="Stream Delay Seconds",
                    minimum=2,
                    maximum=240,
                    value=180,
                    step=1,
                )
                musicgen_crossfade_seconds = gr.Slider(
                    label="Crossfade Seconds",
                    minimum=0,
                    maximum=4,
                    value=1.5,
                    step=0.1,
                )
            with gr.Row():
                musicgen_start_btn = gr.Button("Start Music Stream")
                musicgen_stop_btn = gr.Button("Stop Music Stream")
                fanout_music_btn = gr.Button("Start Twitch Fanout With Music")

            with gr.Row():
                fanout_enable_youtube = gr.Checkbox(value=False, label="Fanout YouTube")
                fanout_enable_twitch = gr.Checkbox(value=True, label="Fanout Twitch")
                fanout_enable_facebook = gr.Checkbox(value=False, label="Fanout Facebook")
                fanout_enable_quote_voice = gr.Checkbox(value=True, label="Mix Quote Voice")
                fanout_use_audio_bus = gr.Checkbox(value=True, label="Use Audio Bus (udp 5006)")

            with gr.Accordion("Audio Bus", open=False):
                with gr.Row():
                    audio_bus_music_volume = gr.Slider(
                        label="Bus Music Volume",
                        minimum=0.0,
                        maximum=2.0,
                        value=0.65,
                        step=0.05,
                    )
                    audio_bus_tts_volume = gr.Slider(
                        label="Bus Quote Volume",
                        minimum=0.0,
                        maximum=4.0,
                        value=2.8,
                        step=0.1,
                    )
                    audio_bus_bitrate = gr.Dropdown(
                        label="Bus Audio Bitrate",
                        choices=["96k", "128k", "160k", "192k"],
                        value="128k",
                        allow_custom_value=False,
                        interactive=True,
                    )
                with gr.Row():
                    audio_bus_start_btn = gr.Button("Start Audio Bus")
                    audio_bus_stop_btn = gr.Button("Stop Audio Bus")
                audio_bus_status = gr.Textbox(label="Audio Bus Status", value=refresh_audio_mix_bus_status(), lines=2)

            with gr.Row():
                music_status_light = gr.HTML(_status_light_html("Music", False))
                quote_status_light = gr.HTML(_status_light_html("Quote Voice", False))
                audio_bus_status_light = gr.HTML(_status_light_html("Audio Bus", False))

            fanout_status = gr.Textbox(label="Fanout Status", value="idle", lines=3)
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
                musicgen_now_status = gr.Textbox(
                    label="Now Feed Status",
                    value="now feed: idle",
                    lines=1,
                )
                musicgen_edit_status = gr.Textbox(
                    label="Edit Feed Status",
                    value="edit feed: idle",
                    lines=1,
                )

            musicgen_audio_preview = gr.Audio(
                label="Latest Clip (Fallback)",
                type="filepath",
                interactive=False,
                visible=False,
            )

            with gr.Accordion("Quote Voice", open=False):
                with gr.Row():
                    quote_json_path = gr.Textbox(
                        label="Quote JSON Path",
                        value="data/quotes/diffusiongemma_quotes.json",
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
                        value=30,
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
                    quote_include_author = gr.Checkbox(value=True, label="Speak Author Name")
                    quote_shuffle = gr.Checkbox(value=True, label="Shuffle Quotes")
                    quote_loop = gr.Checkbox(value=False, label="Loop Quotes")
                    quote_reverb = gr.Checkbox(value=True, label="Quote Reverb")
                    quote_echo = gr.Checkbox(value=True, label="Quote Last-Word Echo")
                with gr.Row():
                    quote_generate_btn = gr.Button("Generate Quote Data")
                    quote_start_btn = gr.Button("Start Quote Voice")
                    quote_stop_btn = gr.Button("Stop Quote Voice")
                quote_status = gr.Textbox(label="Quote Voice Status", value="idle", lines=3)

            ref_image_input = gr.Image(
                label="Reference Image",
                type="numpy",
                sources=["upload"],
                image_mode="RGB",
            )
            ref_image_apply_btn = gr.Button("Apply Image Prompt")

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

        prompt_apply_btn.click(
            set_prompt,
            inputs=[prompt],
            outputs=None,
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
                audio_bus_bitrate,
            ],
            outputs=[fanout_status, musicgen_status],
        )

        audio_bus_start_btn.click(
            start_audio_mix_bus_ui,
            inputs=[audio_bus_music_volume, audio_bus_tts_volume, audio_bus_bitrate],
            outputs=[audio_bus_status],
        )

        audio_bus_stop_btn.click(
            stop_audio_mix_bus_ui,
            outputs=[audio_bus_status],
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
            outputs=[music_status_light, quote_status_light, audio_bus_status_light],
        )

        musicgen_timer.tick(
            refresh_audio_mix_bus_status,
            outputs=[audio_bus_status],
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

    # Queue can stall live webcam callbacks over some tunnels; keep it
    # optional and off by default for responsive webcam processing.
    enable_queue = os.getenv("GRADIO_ENABLE_QUEUE", "0") == "1"
    app = demo
    if enable_queue:
        app = demo.queue(default_concurrency_limit=8)

    app.launch(
        server_name=args.server_name,
        server_port=args.server_port,
    )


if __name__ == "__main__":
    main()
