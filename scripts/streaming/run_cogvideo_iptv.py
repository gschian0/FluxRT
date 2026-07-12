#!/usr/bin/env python3
"""
IPTV → CogVideoX video-to-video streaming generator.

Pulls live video clips from an IPTV HLS stream, then uses CogVideoX-5B
video-to-video to restyle them with AI art direction while preserving
the real motion from the TV footage.

Architecture:
  1. ffmpeg pulls N frames from live IPTV stream → raw video clip
  2. CogVideoXVideoToVideoPipeline restyles the clip with an AI prompt
  3. Crossfade between clips, pipe to UDP 5000 → MediaMTX → Twitch

The 'strength' parameter controls how much the AI deviates from the
original video (0.3 = light stylization, 0.8 = heavy transformation).

Runs on GPU 0 (MusicGen is on GPU 1).

Usage:
    CUDA_VISIBLE_DEVICES=0 python scripts/streaming/run_cogvideo_iptv.py \
        --iptv-url "https://example.com/stream.m3u8" \
        --udp-url "udp://127.0.0.1:5000?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1" \
        --fps 24 --control-port 8889
"""

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from collections import deque

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

COGVIDEO_MODEL_ID = "THUDM/CogVideoX-5b"

# ---------------------------------------------------------------------------
# Artistic style prompts for video-to-video restyling
# ---------------------------------------------------------------------------

PROMPTS = [
    "neo-noir film noir style, high contrast black and white with rain streaks, cinematic shadows, 1940s detective atmosphere",
    "cyberpunk neon overload, electric pink and cyan lighting, holographic advertisements, blade runner aesthetic, rain-soaked streets",
    "oil painting impressionist style, visible brush strokes, vibrant saturated colors, monet-like atmosphere, dreamy soft focus",
    "anime cel-shaded style, bold outlines, flat vibrant colors, studio ghibli atmosphere, whimsical and magical",
    "vaporwave aesthetic, pink and purple gradient sky, retro 80s grid floor, palm trees, VHS scan lines, nostalgic dreamy",
    "underwater scene, everything submerged, caustic light patterns, bubbles rising, blue-green tint, dreamy fluid motion",
    "golden hour cinematic, warm amber lighting, lens flares, dust particles in air, film grain, anamorphic widescreen",
    "ink wash painting style, black ink bleeding into white paper, traditional sumi-e, minimal and elegant",
    "stained glass window style, lead outlines, luminous jewel-tone colors, light shining through, cathedral atmosphere",
    "pixel art 16-bit retro game style, limited color palette, dithering, SNES era graphics, nostalgic chunky pixels",
    "art deco style, geometric patterns, gold and black color scheme, 1920s elegance, symmetrical composition",
    "surreal dreamscape, melting clocks dali style, impossible geometry, floating objects, vivid unnatural colors",
    "watercolor painting style, soft bleeding pigments, wet on wet technique, delicate and ethereal, pastel colors",
    "glitch art digital distortion, datamoshing, RGB channel splitting, pixel sorting, corrupted video aesthetic",
    "baroque oil painting, dramatic chiaroscuro lighting, rich deep colors, ornate golden frames, renaissance masterpiece",
    "synthwave retrofuturism, neon sunset, wireframe mountains, retro car dashboard, 80s nostalgia, magenta and cyan",
    "comic book pop art style, halftone dots, bold black outlines, primary colors, roy lichtenstein style",
    "ethereal fantasy, glowing magical particles, enchanted forest, bioluminescent flora, mystical fog, otherworldly",
    "grunge 90s aesthetic, desaturated colors, film grain, vhs distortion, nirvana era, dirty lens, raw and gritty",
    "ukiyo-e japanese woodblock print style, flat colors, strong outlines, hokusai wave patterns, traditional edo period",
    "steampunk victorian, brass and copper gears, steam pipes, sepia tones, industrial revolution aesthetic, clockwork",
    "cosmic galaxy background, stars and nebulae, everything made of stardust, deep space colors, astronomical scale",
    "retro vhs camcorder style, 1980s home video, tracking errors, color bleeding, timestamp overlay, nostalgic and warm",
    "minimalist scandinavian design, clean white spaces, muted pastel accents, simple geometric shapes, calm and serene",
    "gothic horror atmosphere, dark shadows, candlelight, fog rolling in, victorian architecture, deep purples and blacks",
    "pop art andy warhol style, bright neon colors, repeated images, commercial aesthetic, 1960s campbell soup vibe",
    "ancient egyptian hieroglyphic style, gold and lapis lazuli, papyrus texture, flat profile views, desert heat shimmer",
    "bioluminescent deep ocean, glowing jellyfish, abyssal darkness, ethereal blue light, pressure and vastness",
    "retro 8-bit NES style, extreme limited palette, blocky pixels, classic nintendo aesthetic, chiptune vibes",
    "film noir to color transition, starts black and white gradually becoming vivid color, dramatic transformation",
    "kaleidoscope mirror patterns, symmetrical reflections, psychedelic colors, infinite fractal repetition, mesmerizing",
    "northern lights aurora borealis, green and purple sky curtains, snowy landscape, starfield, silent and majestic",
    "bauhaus design school style, primary colors red blue yellow, geometric shapes, form follows function, 1920s modernism",
    "enchanted winter wonderland, sparkling snow, ice crystals, frozen waterfall, soft blue light, magical and serene",
    "retro futurism 1950s, jet age aesthetic, chrome and tailfins, atomic age patterns, optimistic tomorrowland",
    "dark academia, candlelit library, leather bound books, gothic arches, warm amber glow, scholarly and mysterious",
    "tropical paradise sunset, palm tree silhouettes, orange and pink gradient sky, gentle waves, golden reflection",
    "industrial brutalist concrete, raw textures, imposing geometric structures, overcast sky, stark and powerful",
    "magical realism, floating objects, oversized flora, impossible scale, vivid saturated colors, dreamlike but grounded",
    "retro arcade cabinet glow, CRT scanlines, neon pixel art, 80s carpet pattern, dark room with glowing screens",
    "japanese anime garden, cherry blossoms falling, koi pond, stone lanterns, soft pastel sunset, peaceful and beautiful",
    "dystopian post-apocalyptic, overgrown ruins, faded colors, dust and debris, nature reclaiming civilization",
    "art nouveau alphonse mucha style, flowing organic lines, decorative borders, pastel colors, elegant and ornamental",
    "cosmic horror eldritch, tentacles and impossible geometry, non-euclidean space, sickly green and purple, lovecraftian",
    "retro 70s funk and soul, warm golden tones, vinyl record aesthetic, disco lights, afrofuturism, groove and style",
    "frozen in amber, golden translucent preservation, ancient insects trapped, warm glow, prehistoric stillness",
    "neon noir tokyo night, rainy streets, japanese neon signs, reflections in puddles, electric city atmosphere",
    "paper craft diorama style, layered cut paper, depth shadows, handmade texture, whimsical storybook quality",
    "retro kaiju movie style, miniature city sets, man in rubber suit, practical effects, 1960s godzilla atmosphere",
    "abstract expressionism, jackson pollock style, drips and splashes, raw energy, chaotic but intentional, bold colors",
    "haunted victorian mansion, peeling wallpaper, dusty chandeliers, cobwebs, sepia faded photographs, eerie and still",
    "biotech organic architecture, living walls, growing structures, bioluminescent veins, alien but beautiful, giger inspired",
]

# ---------------------------------------------------------------------------
# Pipeline loading
# ---------------------------------------------------------------------------

def load_cogvideo_v2v_pipeline(device="cuda:0", dtype=None):
    """Load CogVideoX-5B video-to-video pipeline."""
    import torch
    if dtype is None:
        dtype = torch.bfloat16
    from diffusers import CogVideoXVideoToVideoPipeline

    print(f"[cog-iptv] Loading CogVideoX-5B video-to-video pipeline...")
    pipe = CogVideoXVideoToVideoPipeline.from_pretrained(
        COGVIDEO_MODEL_ID,
        torch_dtype=dtype,
    )
    pipe.to(device)

    # Enable memory-efficient attention if available
    try:
        pipe.vae.enable_tiling()
        print(f"[cog-iptv] VAE tiling enabled")
    except Exception:
        pass

    mem = torch.cuda.memory_allocated() / 1e9
    print(f"[cog-iptv] Pipeline loaded. VRAM: {mem:.2f} GB")
    return pipe


# ---------------------------------------------------------------------------
# IPTV clip grabber — pulls N frames from live HLS stream
# ---------------------------------------------------------------------------

class IPTVClipGrabber:
    """Pulls short video clips from a live IPTV HLS stream using ffmpeg.

    Each call to grab_clip() starts a fresh ffmpeg process, reads exactly
    num_frames frames at the target resolution, and returns them as a
    numpy array (N, H, W, 3) BGR uint8.
    """

    def __init__(self, url, width, height, fps=24, num_frames=33):
        self.url = url
        self.width = width
        self.height = height
        self.fps = fps
        self.num_frames = num_frames
        self.frame_size = width * height * 3
        self.lock = threading.Lock()
        self.total_clips = 0
        self.total_frames = 0

    def grab_clip(self, timeout=20.0):
        """Grab num_frames from the live stream.

        Returns numpy array (N, H, W, 3) BGR uint8, or None on failure.
        """
        duration = self.num_frames / self.fps + 1.0  # extra second for buffering

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-fflags", "+genpts",
            "-flags", "low_delay",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", self.url,
            "-vf", f"scale={self.width}:{self.height}:force_original_aspect_ratio=increase,crop={self.width}:{self.height}",
            "-r", str(self.fps),
            "-t", f"{duration}",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-",
        ]

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=self.frame_size * 4,
            )
        except Exception as e:
            print(f"[cog-iptv] ffmpeg start error: {e}")
            return None

        frames = []
        t0 = time.time()

        while len(frames) < self.num_frames:
            if time.time() - t0 > timeout:
                print(f"[cog-iptv] clip grab timeout at {len(frames)}/{self.num_frames} frames")
                break

            raw = proc.stdout.read(self.frame_size)
            if len(raw) == self.frame_size:
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                    (self.height, self.width, 3)
                )
                frames.append(frame)
            elif len(raw) == 0:
                # Stream ended early
                break
            # else partial read, continue

        # Clean up ffmpeg
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass

        if len(frames) == 0:
            print(f"[cog-iptv] no frames grabbed from stream")
            return None

        # Pad with last frame if we got fewer than requested
        while len(frames) < self.num_frames:
            frames.append(frames[-1].copy())

        clip = np.stack(frames, axis=0)  # (N, H, W, 3) BGR

        with self.lock:
            self.total_clips += 1
            self.total_frames += len(frames)

        return clip


# ---------------------------------------------------------------------------
# ffmpeg UDP writer
# ---------------------------------------------------------------------------

def start_udp_writer(width, height, fps, udp_url):
    """Start ffmpeg process that reads raw BGR24 frames from stdin
    and encodes to MPEG-TS over UDP."""
    gop = max(8, int(fps * 2))
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}",
        "-r", str(fps),
        "-i", "-",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-g", str(gop),
        "-keyint_min", str(gop),
        "-sc_threshold", "0",
        "-x264-params", f"repeat-headers=1:keyint={gop}:min-keyint={gop}:scenecut=0",
        "-pix_fmt", "yuv420p",
        "-mpegts_flags", "+resend_headers",
        "-muxdelay", "0",
        "-muxpreload", "0",
        "-flush_packets", "1",
        "-f", "mpegts",
        udp_url,
    ]
    print(f"[cog-iptv] starting ffmpeg UDP writer: {width}x{height}@{fps}fps -> {udp_url}")
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, bufsize=width * height * 3 * 2)


# ---------------------------------------------------------------------------
# Clip generator — IPTV clip → CogVideoX V2V restyle
# ---------------------------------------------------------------------------

class ClipGenerator:
    """Grabs IPTV clips and restyles them with CogVideoX video-to-video."""

    def __init__(self, pipe, grabber, prompts, width=480, height=720,
                 num_frames=33, num_inference_steps=20, guidance_scale=6.0,
                 strength=0.6, frame_rate=24, seed=None,
                 prompt_change_interval=1, device="cuda:0"):
        self.pipe = pipe
        self.grabber = grabber
        self.prompts = list(prompts)
        self.width = width
        self.height = height
        self.num_frames = num_frames
        self.num_inference_steps = num_inference_steps
        self.guidance_scale = guidance_scale
        self.strength = strength
        self.frame_rate = frame_rate
        self.seed = seed
        self.prompt_change_interval = prompt_change_interval
        self.device = device

        self.prompt_index = 0
        self.clip_count = 0
        self.current_seed = seed if seed is not None else random.randint(0, 2**31)

        self.override_prompt = None
        self.override_seed = None
        self.override_steps = None
        self.override_strength = None

        self.last_gen_time = 0.0
        self.last_clip_frames = 0
        self.total_clips = 0

        self.lock = threading.Lock()

    def get_next_prompt(self):
        with self.lock:
            if self.override_prompt:
                return self.override_prompt
            if self.clip_count > 0 and self.clip_count % self.prompt_change_interval == 0:
                self.prompt_index = (self.prompt_index + 1) % len(self.prompts)
            return self.prompts[self.prompt_index]

    def get_next_seed(self):
        with self.lock:
            if self.override_seed is not None:
                return self.override_seed
            self.current_seed = random.randint(0, 2**31)
            return self.current_seed

    def generate_clip(self):
        """Grab an IPTV clip and restyle it with CogVideoX V2V.

        Returns (frames_rgb, prompt, seed).
        """
        import torch

        # Grab a real video clip from the live IPTV stream
        clip_bgr = self.grabber.grab_clip(timeout=20.0)
        if clip_bgr is None:
            print("[cog-iptv] WARNING: no IPTV clip, generating black frames")
            clip_bgr = np.zeros((self.num_frames, self.height, self.width, 3), dtype=np.uint8)

        # Convert BGR -> RGB for the pipeline
        # CogVideoX expects (T, H, W, C) RGB
        clip_rgb = np.array([cv2.cvtColor(f, cv2.COLOR_BGR2RGB) for f in clip_bgr])

        prompt = self.get_next_prompt()
        seed = self.get_next_seed()
        steps = self.override_steps if self.override_steps else self.num_inference_steps
        strength = self.override_strength if self.override_strength else self.strength

        gen = torch.Generator(device=self.device)
        gen.manual_seed(seed)

        t0 = time.time()
        with torch.no_grad():
            result = self.pipe(
                video=clip_rgb,
                prompt=prompt,
                num_inference_steps=steps,
                strength=strength,
                guidance_scale=self.guidance_scale,
                generator=gen,
                output_type="pil",
            )
        gen_time = time.time() - t0

        frames = np.array([np.array(f) for f in result.frames[0]])  # (N, H, W, 3) RGB

        with self.lock:
            self.clip_count += 1
            self.total_clips += 1
            self.last_gen_time = gen_time
            self.last_clip_frames = len(frames)

        print(f"[cog-iptv] clip #{self.total_clips}: {len(frames)} frames in {gen_time:.1f}s "
              f"({len(frames)/gen_time:.1f} fps) prompt=#{self.prompt_index} seed={seed} "
              f"strength={strength:.1f} \"{prompt[:60]}...\"")

        return frames, prompt, seed

    def get_status(self):
        with self.lock:
            return {
                "total_clips": self.total_clips,
                "clip_count": self.clip_count,
                "prompt_index": self.prompt_index,
                "num_prompts": len(self.prompts),
                "current_seed": self.current_seed,
                "override_prompt": self.override_prompt,
                "override_seed": self.override_seed,
                "override_steps": self.override_steps,
                "override_strength": self.override_strength,
                "last_gen_time": self.last_gen_time,
                "last_clip_frames": self.last_clip_frames,
                "num_frames": self.num_frames,
                "width": self.width,
                "height": self.height,
                "num_inference_steps": self.override_steps or self.num_inference_steps,
                "guidance_scale": self.guidance_scale,
                "strength": self.override_strength or self.strength,
                "frame_rate": self.frame_rate,
                "prompt_change_interval": self.prompt_change_interval,
                "iptv_url": self.grabber.url,
                "iptv_clips_grabbed": self.grabber.total_clips,
                "iptv_frames_grabbed": self.grabber.total_frames,
            }


# ---------------------------------------------------------------------------
# Crossfade helper
# ---------------------------------------------------------------------------

def crossfade_clips(clip_a, clip_b, n_frames):
    if n_frames <= 0 or len(clip_a) < n_frames or len(clip_b) < n_frames:
        return np.concatenate([clip_a, clip_b], axis=0)

    tail = clip_a[-n_frames:].astype(np.float32)
    head = clip_b[:n_frames].astype(np.float32)
    alphas = np.linspace(0, 1, n_frames, dtype=np.float32).reshape(n_frames, 1, 1, 1)
    blended = (tail * (1 - alphas) + head * alphas).astype(np.uint8)
    result = np.concatenate([clip_a[:-n_frames], blended, clip_b[n_frames:]], axis=0)
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="IPTV → CogVideoX V2V streamer -> UDP")
    parser.add_argument("--iptv-url", required=True, help="IPTV HLS stream URL (m3u8)")
    parser.add_argument("--udp-url", default="udp://127.0.0.1:5000?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1",
                        help="UDP destination URL")
    parser.add_argument("--fps", type=int, default=24, help="Output FPS")
    parser.add_argument("--width", type=int, default=480, help="Generation width (CogVideoX default: 480)")
    parser.add_argument("--height", type=int, default=720, help="Generation height (CogVideoX default: 720)")
    parser.add_argument("--num-frames", type=int, default=33, help="Frames per clip")
    parser.add_argument("--steps", type=int, default=20, help="Inference steps")
    parser.add_argument("--guidance-scale", type=float, default=6.0, help="CFG guidance scale")
    parser.add_argument("--strength", type=float, default=0.6,
                        help="V2V strength (0.1=light stylization, 0.8=heavy transformation)")
    parser.add_argument("--seed", type=int, default=None, help="Fixed seed")
    parser.add_argument("--prompt-change-interval", type=int, default=1,
                        help="Change prompt every N clips")
    parser.add_argument("--crossfade-frames", type=int, default=5, help="Crossfade frames between clips")
    parser.add_argument("--prompt-file", default=None, help="JSON file with prompts")
    parser.add_argument("--prompt", default=None, help="Single override prompt")
    parser.add_argument("--control-port", type=int, default=8889, help="HTTP control port")
    parser.add_argument("--device", default="cuda:0", help="CUDA device")
    parser.add_argument("--buffer-size", type=int, default=2, help="Clip buffer size")
    args = parser.parse_args()

    # Load prompts
    if args.prompt_file:
        with open(args.prompt_file) as pf:
            prompts = json.load(pf)
    elif args.prompt:
        prompts = [args.prompt]
    else:
        prompts = PROMPTS

    print(f"[cog-iptv] IPTV → CogVideoX-5B video-to-video streamer")
    print(f"[cog-iptv] IPTV source: {args.iptv_url}")
    print(f"[cog-iptv] Resolution: {args.width}x{args.height}")
    print(f"[cog-iptv] Clips: {args.num_frames} frames @ {args.fps}fps = {args.num_frames/args.fps:.1f}s per clip")
    print(f"[cog-iptv] Steps: {args.steps}, Guidance: {args.guidance_scale}, Strength: {args.strength}")
    print(f"[cog-iptv] Crossfade: {args.crossfade_frames} frames")
    print(f"[cog-iptv] Prompts: {len(prompts)} loaded, change every {args.prompt_change_interval} clip(s)")
    print(f"[cog-iptv] Buffer: {args.buffer_size} clips ahead")

    # Load CogVideoX V2V pipeline
    pipe = load_cogvideo_v2v_pipeline(device=args.device)

    # Create IPTV clip grabber
    grabber = IPTVClipGrabber(
        url=args.iptv_url,
        width=args.width,
        height=args.height,
        fps=args.fps,
        num_frames=args.num_frames,
    )

    # Create clip generator
    generator = ClipGenerator(
        pipe=pipe,
        grabber=grabber,
        prompts=prompts,
        width=args.width,
        height=args.height,
        num_frames=args.num_frames,
        num_inference_steps=args.steps,
        guidance_scale=args.guidance_scale,
        strength=args.strength,
        frame_rate=args.fps,
        seed=args.seed,
        prompt_change_interval=args.prompt_change_interval,
        device=args.device,
    )

    # Start UDP writer
    writer = start_udp_writer(args.width, args.height, args.fps, args.udp_url)

    # --- Clip buffer (producer-consumer) ---
    clip_queue = deque(maxlen=args.buffer_size + 2)
    queue_lock = threading.Lock()
    queue_cond = threading.Condition(queue_lock)
    running = [True]

    def producer_loop():
        while running[0]:
            try:
                frames, prompt, seed = generator.generate_clip()
                with queue_cond:
                    while len(clip_queue) >= args.buffer_size + 1 and running[0]:
                        queue_cond.wait(timeout=1.0)
                    clip_queue.append((frames, prompt, seed))
                    queue_cond.notify()
            except Exception as e:
                print(f"[cog-iptv] producer error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(1.0)

    producer = threading.Thread(target=producer_loop, daemon=True)
    producer.start()

    # --- Remote control HTTP server ---
    remote = {"generator": generator, "prompts": prompts, "grabber": grabber}

    if args.control_port > 0:
        class ControlHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def _send_json(self, code, data):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(data, indent=2).encode())

            def do_GET(self):
                parsed = urlparse(self.path)
                gen = remote["generator"]
                if parsed.path == "/status":
                    status = gen.get_status()
                    status["queue_length"] = len(clip_queue)
                    status["running"] = running[0]
                    self._send_json(200, status)
                elif parsed.path == "/prompts":
                    self._send_json(200, {"prompts": remote["prompts"]})
                else:
                    self._send_json(404, {"error": "unknown endpoint"})

            def do_POST(self):
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode() if length else "{}"
                try:
                    data = json.loads(body) if body else {}
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "invalid JSON"})
                    return

                gen = remote["generator"]

                if parsed.path == "/prompt":
                    p = data.get("prompt", "")
                    if p:
                        with gen.lock:
                            gen.override_prompt = p
                        self._send_json(200, {"ok": True, "override_prompt": p})
                    else:
                        self._send_json(400, {"error": "missing 'prompt'"})
                elif parsed.path == "/prompts":
                    plist = data.get("prompts", [])
                    if plist and isinstance(plist, list):
                        with gen.lock:
                            gen.prompts = plist
                            gen.prompt_index = 0
                            gen.override_prompt = None
                        remote["prompts"] = plist
                        self._send_json(200, {"ok": True, "num_prompts": len(plist)})
                    else:
                        self._send_json(400, {"error": "missing 'prompts' list"})
                elif parsed.path == "/seed":
                    s = data.get("seed")
                    if s is not None:
                        with gen.lock:
                            gen.override_seed = int(s)
                        self._send_json(200, {"ok": True, "override_seed": int(s)})
                    else:
                        self._send_json(400, {"error": "missing 'seed'"})
                elif parsed.path == "/steps":
                    st = data.get("steps")
                    if st is not None:
                        with gen.lock:
                            gen.override_steps = int(st)
                        self._send_json(200, {"ok": True, "override_steps": int(st)})
                    else:
                        self._send_json(400, {"error": "missing 'steps'"})
                elif parsed.path == "/strength":
                    st = data.get("strength")
                    if st is not None:
                        with gen.lock:
                            gen.override_strength = float(st)
                        self._send_json(200, {"ok": True, "override_strength": float(st)})
                    else:
                        self._send_json(400, {"error": "missing 'strength'"})
                elif parsed.path == "/rotate":
                    with gen.lock:
                        gen.override_prompt = None
                        gen.override_seed = None
                        gen.override_steps = None
                        gen.override_strength = None
                    self._send_json(200, {"ok": True, "message": "rotation resumed"})
                elif parsed.path == "/next":
                    with gen.lock:
                        gen.prompt_index = (gen.prompt_index + 1) % len(gen.prompts)
                        gen.override_prompt = None
                        np = gen.prompts[gen.prompt_index]
                    self._send_json(200, {"ok": True, "prompt_index": gen.prompt_index, "prompt": np[:100]})
                elif parsed.path == "/interval":
                    pi = data.get("prompt_change_interval")
                    if pi is not None:
                        with gen.lock:
                            gen.prompt_change_interval = int(pi)
                        self._send_json(200, {"ok": True, "prompt_change_interval": gen.prompt_change_interval})
                elif parsed.path == "/channel":
                    url = data.get("url", "")
                    if url:
                        remote["grabber"].url = url
                        self._send_json(200, {"ok": True, "iptv_url": url})
                    else:
                        self._send_json(400, {"error": "missing 'url'"})
                else:
                    self._send_json(404, {"error": "unknown endpoint"})

            def do_OPTIONS(self):
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

        def start_control_server(port):
            server = HTTPServer(("0.0.0.0", port), ControlHandler)
            server.timeout = 0.1
            print(f"[cog-iptv] remote control server on http://0.0.0.0:{port}")
            print(f"[cog-iptv]   GET  /status    — current state")
            print(f"[cog-iptv]   GET  /prompts   — list all prompts")
            print(f"[cog-iptv]   POST /prompt    — set override prompt")
            print(f"[cog-iptv]   POST /prompts   — replace prompt list")
            print(f"[cog-iptv]   POST /seed      — set fixed seed")
            print(f"[cog-iptv]   POST /steps     — set steps")
            print(f"[cog-iptv]   POST /strength  — set V2V strength (0.1-0.9)")
            print(f"[cog-iptv]   POST /next      — jump to next prompt")
            print(f"[cog-iptv]   POST /rotate    — resume auto rotation")
            print(f"[cog-iptv]   POST /interval  — change prompt interval")
            print(f"[cog-iptv]   POST /channel   — switch IPTV source URL")
            while running[0]:
                server.handle_request()
            server.server_close()

        ctrl_thread = threading.Thread(target=start_control_server, args=(args.control_port,), daemon=True)
        ctrl_thread.start()

    # --- Signal handlers ---
    def handle_signal(signum, frame):
        running[0] = False
        with queue_cond:
            queue_cond.notify_all()
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # --- Main playback loop ---
    print(f"[cog-iptv] waiting for first clip to generate...")

    with queue_cond:
        while len(clip_queue) == 0 and running[0]:
            queue_cond.wait(timeout=1.0)

    if not running[0]:
        print("[cog-iptv] shutdown before first clip")
        return

    print(f"[cog-iptv] first clip ready, starting playback at {args.fps}fps")

    prev_clip = None
    total_frames_played = 0
    frames_since_stats = 0
    last_stats_time = time.time()
    frame_interval = 1.0 / args.fps

    try:
        while running[0]:
            with queue_cond:
                while len(clip_queue) == 0 and running[0]:
                    queue_cond.wait(timeout=1.0)
                if not running[0]:
                    break
                clip_frames, clip_prompt, clip_seed = clip_queue.popleft()
                queue_cond.notify()

            # Crossfade with previous clip
            if prev_clip is not None and args.crossfade_frames > 0:
                play_frames = crossfade_clips(prev_clip, clip_frames, args.crossfade_frames)
            else:
                play_frames = clip_frames

            # Play frames at target FPS
            for i in range(len(play_frames)):
                if not running[0]:
                    break

                frame = play_frames[i]  # RGB uint8 (H, W, 3)
                frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

                try:
                    writer.stdin.write(frame_bgr.tobytes())
                    writer.stdin.flush()
                except (BrokenPipeError, IOError):
                    print("[cog-iptv] UDP writer pipe broke, restarting...")
                    try:
                        writer.terminate()
                        writer.wait(timeout=2)
                    except Exception:
                        pass
                    writer = start_udp_writer(args.width, args.height, args.fps, args.udp_url)

                total_frames_played += 1
                frames_since_stats += 1
                time.sleep(frame_interval)

            prev_clip = clip_frames

            # Stats every 10 seconds
            now = time.time()
            if now - last_stats_time >= 10.0:
                elapsed = now - last_stats_time
                actual_fps = frames_since_stats / elapsed
                gen_status = generator.get_status()
                print(f"[cog-iptv] played={total_frames_played} fps={actual_fps:.1f} "
                      f"queue={len(clip_queue)} clips={gen_status['total_clips']} "
                      f"last_gen={gen_status['last_gen_time']:.1f}s "
                      f"prompt=#{gen_status['prompt_index']} "
                      f"iptv_clips={gen_status['iptv_clips_grabbed']}")
                frames_since_stats = 0
                last_stats_time = now

    except KeyboardInterrupt:
        pass
    finally:
        print("[cog-iptv] shutting down...")
        running[0] = False
        with queue_cond:
            queue_cond.notify_all()

        try:
            writer.stdin.close()
            writer.terminate()
            writer.wait(timeout=3)
        except Exception:
            pass

        producer.join(timeout=5)
        print("[cog-iptv] done.")


if __name__ == "__main__":
    main()
