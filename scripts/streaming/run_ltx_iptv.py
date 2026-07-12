#!/usr/bin/env python3
"""
Headless IPTV → LTX-Video image-to-video streaming generator.

Pulls live video from an IPTV HLS stream (m3u8), grabs frames from it,
and uses each frame as the conditioning image for LTX-Video img2vid.
The AI prompt restyles the real TV footage with artistic motion.

Architecture:
  1. ffmpeg pulls live IPTV stream → extracts frames at target FPS
  2. Every N frames, grab one as the "seed frame" for LTX img2vid
  3. LTXImageToVideoPipeline generates a short clip starting from that frame
  4. Crossfade between clips, pipe to UDP 5000 → MediaMTX → Twitch

Runs on GPU 0 (MusicGen is on GPU 1).

Usage:
    CUDA_VISIBLE_DEVICES=0 python scripts/streaming/run_ltx_iptv.py \
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
from io import BytesIO

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Model path
# ---------------------------------------------------------------------------

LTX_MODEL_ID = "Lightricks/LTX-Video-0.9.5"

# ---------------------------------------------------------------------------
# Prompt library — artistic styles to apply over IPTV footage
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
    "comic book pop art style, halftone dots, bold black outlines, primary colors, roy lichtenstein style speech bubbles",
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
    "cosmic horror elderitch, tentacles and impossible geometry, non-euclidean space, sickly green and purple, lovecraftian",
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
# Pipeline loading (with transformers 5.x tokenizer fix)
# ---------------------------------------------------------------------------

def load_ltx_i2v_pipeline(device="cuda:0", dtype=None):
    """Load the LTX-Video image-to-video pipeline with manual tokenizer fix."""
    import torch
    if dtype is None:
        dtype = torch.bfloat16
    from transformers import T5Tokenizer, T5EncoderModel
    from diffusers import LTXImageToVideoPipeline

    from huggingface_hub import snapshot_download
    model_path = snapshot_download(LTX_MODEL_ID, cache_dir="/root/.cache/huggingface/hub")

    print(f"[ltx-iptv] Loading tokenizer from {model_path}/tokenizer ...")
    tokenizer = T5Tokenizer.from_pretrained(
        os.path.join(model_path, "tokenizer"), legacy=False
    )

    print(f"[ltx-iptv] Loading text encoder (T5) ...")
    text_encoder = T5EncoderModel.from_pretrained(
        os.path.join(model_path, "text_encoder"), torch_dtype=dtype
    )

    print(f"[ltx-iptv] Loading LTX img2vid pipeline ...")
    pipe = LTXImageToVideoPipeline.from_pretrained(
        LTX_MODEL_ID,
        torch_dtype=dtype,
        tokenizer=tokenizer,
        text_encoder=text_encoder,
    )
    pipe.to(device)

    mem = torch.cuda.memory_allocated() / 1e9
    print(f"[ltx-iptv] Pipeline loaded. VRAM: {mem:.2f} GB")
    return pipe


# ---------------------------------------------------------------------------
# IPTV frame grabber — pulls live frames from HLS stream via ffmpeg
# ---------------------------------------------------------------------------

class IPTVGrabber:
    """Pulls frames from a live IPTV HLS stream using ffmpeg.

    Runs ffmpeg as a subprocess that decodes the stream and outputs
    raw BGR24 frames to stdout.  Frames are read one at a time.
    """

    def __init__(self, url, width, height, fps=24, device="cuda:0"):
        self.url = url
        self.width = width
        self.height = height
        self.fps = fps
        self.device = device
        self.proc = None
        self.frame_size = width * height * 3
        self.lock = threading.Lock()
        self.reconnect_delay = 2.0
        self.total_frames = 0

    def start(self):
        """Start the ffmpeg subprocess."""
        self._start_ffmpeg()
        print(f"[ltx-iptv] IPTV grabber started: {self.url}")
        print(f"[ltx-iptv]   -> {self.width}x{self.height}@{self.fps}fps")

    def _start_ffmpeg(self):
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
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-",
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=self.frame_size * 2,
        )

    def grab_frame(self, timeout=10.0):
        """Read one raw BGR24 frame from the stream.

        Returns numpy array (H, W, 3) BGR uint8, or None on failure.
        """
        t0 = time.time()
        while True:
            if self.proc is None or self.proc.poll() is not None:
                # Process died — reconnect
                print(f"[ltx-iptv] stream ended, reconnecting to {self.url}...")
                self._start_ffmpeg()
                time.sleep(self.reconnect_delay)
                continue

            raw = self.proc.stdout.read(self.frame_size)
            if len(raw) == self.frame_size:
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                    (self.height, self.width, 3)
                )
                self.total_frames += 1
                return frame
            elif len(raw) == 0:
                # EOF — reconnect
                print(f"[ltx-iptv] stream EOF, reconnecting...")
                self._restart()
                continue
            else:
                # Partial read — try again
                if time.time() - t0 > timeout:
                    print(f"[ltx-iptv] frame read timeout, reconnecting...")
                    self._restart()
                    continue
                continue

    def _restart(self):
        try:
            if self.proc:
                self.proc.kill()
                self.proc.wait(timeout=3)
        except Exception:
            pass
        time.sleep(self.reconnect_delay)
        self._start_ffmpeg()

    def stop(self):
        try:
            if self.proc:
                self.proc.kill()
                self.proc.wait(timeout=3)
        except Exception:
            pass


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
    print(f"[ltx-iptv] starting ffmpeg UDP writer: {width}x{height}@{fps}fps -> {udp_url}")
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, bufsize=width * height * 3 * 2)


# ---------------------------------------------------------------------------
# Clip generator — grabs IPTV frame, runs LTX img2vid
# ---------------------------------------------------------------------------

class ClipGenerator:
    """Generates LTX img2vid clips conditioned on live IPTV frames."""

    def __init__(self, pipe, grabber, prompts, width=768, height=512,
                 num_frames=33, num_inference_steps=8, guidance_scale=3.0,
                 frame_rate=24, seed=None, prompt_change_interval=1,
                 device="cuda:0"):
        self.pipe = pipe
        self.grabber = grabber
        self.prompts = list(prompts)
        self.width = width
        self.height = height
        self.num_frames = num_frames
        self.num_inference_steps = num_inference_steps
        self.guidance_scale = guidance_scale
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

        self.last_gen_time = 0.0
        self.last_clip_frames = 0
        self.total_clips = 0
        self.last_source_frame = None

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
        """Grab a frame from IPTV, then generate an LTX img2vid clip from it.

        Returns (frames_rgb, prompt, seed, source_frame_bgr).
        """
        import torch
        from PIL import Image

        # Grab a fresh frame from the live IPTV stream
        source_bgr = self.grabber.grab_frame(timeout=15.0)
        if source_bgr is None:
            print("[ltx-iptv] WARNING: no IPTV frame, using black frame")
            source_bgr = np.zeros((self.height, self.width, 3), dtype=np.uint8)

        # Convert BGR -> RGB for PIL
        source_rgb = cv2.cvtColor(source_bgr, cv2.COLOR_BGR2RGB)
        pil_image = Image.fromarray(source_rgb)

        prompt = self.get_next_prompt()
        seed = self.get_next_seed()
        steps = self.override_steps if self.override_steps else self.num_inference_steps

        gen = torch.Generator(device=self.device)
        gen.manual_seed(seed)

        t0 = time.time()
        with torch.no_grad():
            result = self.pipe(
                image=pil_image,
                prompt=prompt,
                num_frames=self.num_frames,
                height=self.height,
                width=self.width,
                num_inference_steps=steps,
                guidance_scale=self.guidance_scale,
                frame_rate=self.frame_rate,
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
            self.last_source_frame = source_bgr

        print(f"[ltx-iptv] clip #{self.total_clips}: {len(frames)} frames in {gen_time:.1f}s "
              f"({len(frames)/gen_time:.1f} fps) prompt=#{self.prompt_index} seed={seed} "
              f"\"{prompt[:60]}...\"")

        return frames, prompt, seed, source_bgr

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
                "last_gen_time": self.last_gen_time,
                "last_clip_frames": self.last_clip_frames,
                "num_frames": self.num_frames,
                "width": self.width,
                "height": self.height,
                "num_inference_steps": self.override_steps or self.num_inference_steps,
                "guidance_scale": self.guidance_scale,
                "frame_rate": self.frame_rate,
                "prompt_change_interval": self.prompt_change_interval,
                "iptv_url": self.grabber.url,
                "iptv_frames_grabbed": self.grabber.total_frames,
            }


# ---------------------------------------------------------------------------
# Crossfade helper
# ---------------------------------------------------------------------------

def crossfade_clips(clip_a, clip_b, n_frames):
    """Crossfade end of clip_a with beginning of clip_b."""
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
    parser = argparse.ArgumentParser(description="IPTV → LTX img2vid streamer -> UDP")
    parser.add_argument("--iptv-url", required=True,
                        help="IPTV HLS stream URL (m3u8)")
    parser.add_argument("--udp-url", default="udp://127.0.0.1:5000?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1",
                        help="UDP destination URL for video frames")
    parser.add_argument("--fps", type=int, default=24, help="Output video FPS")
    parser.add_argument("--width", type=int, default=768, help="Generation width")
    parser.add_argument("--height", type=int, default=512, help="Generation height")
    parser.add_argument("--num-frames", type=int, default=33, help="Frames per clip")
    parser.add_argument("--steps", type=int, default=8, help="Inference steps per clip")
    parser.add_argument("--guidance-scale", type=float, default=3.0, help="CFG guidance scale")
    parser.add_argument("--seed", type=int, default=None, help="Fixed seed")
    parser.add_argument("--prompt-change-interval", type=int, default=1,
                        help="Change prompt every N clips (default: 1)")
    parser.add_argument("--crossfade-frames", type=int, default=5,
                        help="Frames to crossfade between clips")
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

    print(f"[ltx-iptv] IPTV → LTX-Video img2vid streamer")
    print(f"[ltx-iptv] IPTV source: {args.iptv_url}")
    print(f"[ltx-iptv] Resolution: {args.width}x{args.height}")
    print(f"[ltx-iptv] Clips: {args.num_frames} frames @ {args.fps}fps = {args.num_frames/args.fps:.1f}s per clip")
    print(f"[ltx-iptv] Steps: {args.steps}, Guidance: {args.guidance_scale}")
    print(f"[ltx-iptv] Crossfade: {args.crossfade_frames} frames")
    print(f"[ltx-iptv] Prompts: {len(prompts)} loaded, change every {args.prompt_change_interval} clip(s)")
    print(f"[ltx-iptv] Buffer: {args.buffer_size} clips ahead")

    # Load LTX img2vid pipeline
    print("[ltx-iptv] Loading LTX-Video img2vid pipeline...")
    pipe = load_ltx_i2v_pipeline(device=args.device)

    # Start IPTV grabber
    grabber = IPTVGrabber(
        url=args.iptv_url,
        width=args.width,
        height=args.height,
        fps=args.fps,
        device=args.device,
    )
    grabber.start()

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
                frames, prompt, seed, src = generator.generate_clip()
                with queue_cond:
                    while len(clip_queue) >= args.buffer_size + 1 and running[0]:
                        queue_cond.wait(timeout=1.0)
                    clip_queue.append((frames, prompt, seed))
                    queue_cond.notify()
            except Exception as e:
                print(f"[ltx-iptv] producer error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(1.0)

    producer = threading.Thread(target=producer_loop, daemon=True)
    producer.start()

    # --- Remote control HTTP server ---
    remote = {"generator": generator, "prompts": prompts}

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
                elif parsed.path == "/rotate":
                    with gen.lock:
                        gen.override_prompt = None
                        gen.override_seed = None
                        gen.override_steps = None
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
                    # Switch IPTV channel
                    url = data.get("url", "")
                    if url:
                        grabber.stop()
                        grabber.url = url
                        grabber.start()
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
            print(f"[ltx-iptv] remote control server on http://0.0.0.0:{port}")
            print(f"[ltx-iptv]   GET  /status    — current state")
            print(f"[ltx-iptv]   GET  /prompts   — list all prompts")
            print(f"[ltx-iptv]   POST /prompt    — set override prompt")
            print(f"[ltx-iptv]   POST /prompts   — replace prompt list")
            print(f"[ltx-iptv]   POST /seed      — set fixed seed")
            print(f"[ltx-iptv]   POST /steps     — set steps")
            print(f"[ltx-iptv]   POST /next      — jump to next prompt")
            print(f"[ltx-iptv]   POST /rotate    — resume auto rotation")
            print(f"[ltx-iptv]   POST /interval  — change prompt interval")
            print(f"[ltx-iptv]   POST /channel   — switch IPTV source URL")
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
    print(f"[ltx-iptv] waiting for first clip to generate...")

    with queue_cond:
        while len(clip_queue) == 0 and running[0]:
            queue_cond.wait(timeout=1.0)

    if not running[0]:
        print("[ltx-iptv] shutdown before first clip")
        return

    print(f"[ltx-iptv] first clip ready, starting playback at {args.fps}fps")

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
                    print("[ltx-iptv] UDP writer pipe broke, restarting...")
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
                print(f"[ltx-iptv] played={total_frames_played} fps={actual_fps:.1f} "
                      f"queue={len(clip_queue)} clips={gen_status['total_clips']} "
                      f"last_gen={gen_status['last_gen_time']:.1f}s "
                      f"prompt=#{gen_status['prompt_index']} "
                      f"iptv_frames={gen_status['iptv_frames_grabbed']}")
                frames_since_stats = 0
                last_stats_time = now

    except KeyboardInterrupt:
        pass
    finally:
        print("[ltx-iptv] shutting down...")
        running[0] = False
        with queue_cond:
            queue_cond.notify_all()

        grabber.stop()
        print("[ltx-iptv] IPTV grabber stopped")

        try:
            writer.stdin.close()
            writer.terminate()
            writer.wait(timeout=3)
        except Exception:
            pass

        producer.join(timeout=5)
        print("[ltx-iptv] done.")


if __name__ == "__main__":
    main()
