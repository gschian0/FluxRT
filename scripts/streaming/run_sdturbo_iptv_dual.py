#!/usr/bin/env python3
"""
IPTV → SD-Turbo DUAL-GPU real-time frame-by-frame img2img streamer.

Uses BOTH L40 GPUs in parallel — each GPU runs its own SD-Turbo instance
and processes alternate frames. This nearly doubles throughput.

Also optimizes by:
  - Caching text embeddings (CLIP only runs when prompt changes)
  - Using TAESD for faster VAE decode
  - Pre-encoding input latents (VAE encode only once per frame)
  - Manual 1-step UNet call (bypasses diffusers 2-step minimum bug)

Usage:
    CUDA_VISIBLE_DEVICES=0,1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    python scripts/streaming/run_sdturbo_iptv_dual.py \
        --iptv-url "https://live.enhdtv.com:8081/8150/index.m3u8" \
        --udp-url "udp://127.0.0.1:5000?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1" \
        --fps 8 --width 256 --height 256 --strength 0.5 --control-port 8889
"""

import argparse
import json
import os

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

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

cv2.setNumThreads(1)

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

DEFAULT_MODEL_ID = "stabilityai/sd-turbo"
TAESD_PATH = "/root/.cache/huggingface/hub/models--madebyollin--taesd/snapshots/614f76814bbe30edbe2e627ace1c2234c81a2c0e"

# ---------------------------------------------------------------------------
# Artistic style prompts
# ---------------------------------------------------------------------------

PROMPTS = [
    "neo-noir film noir style, high contrast black and white, cinematic shadows, rain, 1940s detective atmosphere",
    "cyberpunk neon overload, electric pink and cyan lighting, holographic advertisements, blade runner aesthetic",
    "oil painting impressionist style, visible brush strokes, vibrant saturated colors, monet-like, dreamy soft focus",
    "anime cel-shaded style, bold outlines, flat vibrant colors, studio ghibli atmosphere, whimsical and magical",
    "vaporwave aesthetic, pink and purple gradient, retro 80s grid, VHS scan lines, nostalgic dreamy",
    "underwater scene, caustic light patterns, bubbles rising, blue-green tint, dreamy fluid motion",
    "golden hour cinematic, warm amber lighting, lens flares, dust particles, film grain, anamorphic",
    "ink wash painting style, black ink on white paper, traditional sumi-e, minimal and elegant",
    "stained glass window style, lead outlines, luminous jewel-tone colors, cathedral atmosphere",
    "pixel art 16-bit retro game style, limited palette, dithering, SNES era graphics, chunky pixels",
    "art deco style, geometric patterns, gold and black, 1920s elegance, symmetrical composition",
    "surreal dreamscape, melting clocks dali style, impossible geometry, floating objects, vivid colors",
    "watercolor painting style, soft bleeding pigments, wet on wet, delicate ethereal, pastel colors",
    "glitch art digital distortion, datamoshing, RGB channel splitting, pixel sorting, corrupted video",
    "baroque oil painting, dramatic chiaroscuro, rich deep colors, renaissance masterpiece",
    "synthwave retrofuturism, neon sunset, wireframe mountains, 80s nostalgia, magenta and cyan",
    "comic book pop art style, halftone dots, bold black outlines, primary colors, lichtenstein style",
    "ethereal fantasy, glowing magical particles, enchanted forest, bioluminescent, mystical fog",
    "grunge 90s aesthetic, desaturated, film grain, vhs distortion, nirvana era, raw and gritty",
    "ukiyo-e japanese woodblock print, flat colors, strong outlines, hokusai style, edo period",
    "steampunk victorian, brass and copper gears, steam pipes, sepia tones, industrial revolution",
    "cosmic galaxy background, stars and nebulae, stardust, deep space colors, astronomical scale",
    "retro vhs camcorder style, 1980s home video, tracking errors, color bleeding, nostalgic",
    "minimalist scandinavian design, clean white, muted pastels, simple geometric shapes, serene",
    "gothic horror atmosphere, dark shadows, candlelight, fog, victorian architecture, deep purples",
    "pop art andy warhol style, bright neon colors, repeated images, 1960s commercial aesthetic",
    "ancient egyptian hieroglyphic style, gold and lapis lazuli, papyrus texture, desert heat",
    "bioluminescent deep ocean, glowing jellyfish, abyssal darkness, ethereal blue light",
    "retro 8-bit NES style, extreme limited palette, blocky pixels, classic nintendo, chiptune vibes",
    "kaleidoscope mirror patterns, symmetrical reflections, psychedelic colors, fractal repetition",
    "northern lights aurora borealis, green and purple sky, snowy landscape, starfield, majestic",
    "bauhaus design, primary colors red blue yellow, geometric shapes, 1920s modernism",
    "enchanted winter wonderland, sparkling snow, ice crystals, soft blue light, magical serene",
    "retro futurism 1950s, jet age, chrome and tailfins, atomic age, optimistic tomorrowland",
    "dark academia, candlelit library, leather books, gothic arches, warm amber glow, mysterious",
    "tropical paradise sunset, palm silhouettes, orange pink sky, gentle waves, golden reflection",
    "industrial brutalist concrete, raw textures, imposing structures, overcast sky, stark",
    "magical realism, floating objects, oversized flora, impossible scale, vivid colors, dreamlike",
    "retro arcade cabinet glow, CRT scanlines, neon pixel art, 80s carpet, dark room glowing screens",
    "japanese anime garden, cherry blossoms falling, koi pond, stone lanterns, soft pastel sunset",
    "dystopian post-apocalyptic, overgrown ruins, faded colors, dust debris, nature reclaiming",
    "art nouveau alphonse mucha style, flowing organic lines, decorative borders, pastel, elegant",
    "cosmic horror eldritch, tentacles, impossible geometry, non-euclidean, sickly green purple",
    "retro 70s funk and soul, warm golden tones, vinyl record aesthetic, disco lights, afrofuturism",
    "neon noir tokyo night, rainy streets, japanese neon signs, reflections in puddles, electric city",
    "paper craft diorama style, layered cut paper, depth shadows, handmade texture, storybook",
    "retro kaiju movie style, miniature city sets, practical effects, 1960s godzilla atmosphere",
    "abstract expressionism, jackson pollock style, drips and splashes, raw energy, bold colors",
    "haunted victorian mansion, peeling wallpaper, dusty chandeliers, cobwebs, eerie and still",
    "biotech organic architecture, living walls, bioluminescent veins, alien but beautiful, giger",
    "low poly 3d render style, flat shading, geometric facets, vibrant colors, modern digital art",
    "claymation stop motion style, visible fingerprints, clay texture, handmade, whimsical and charming",
]

NEGATIVE_PROMPT = "blurry, low quality, distorted, deformed, ugly, watermark, text, logo, jpeg artifacts"


# ---------------------------------------------------------------------------
# GPU worker — one SD-Turbo instance per GPU
# ---------------------------------------------------------------------------

class GPUWorker:
    """One SD-Turbo instance running on a single CUDA device.

    Each worker owns its own pipeline, text-embedding cache, and prompt state.
    The main thread pushes raw frames into a per-GPU work queue; a dedicated
    GPU thread pulls from that queue, stylizes the frame, and pushes the result
    to a shared ordered output queue.
    """

    def __init__(self, gpu_id, device, prompts, width, height,
                 strength, guidance_scale, num_inference_steps,
                 seed, prompt_change_interval, model_id, taesd_path):
        self.gpu_id = gpu_id
        self.device = device
        self.model_id = model_id
        self.taesd_path = taesd_path
        self.prompts = list(prompts)
        self.width = width
        self.height = height
        self.strength = strength
        self.guidance_scale = guidance_scale
        self.num_inference_steps = num_inference_steps
        self.seed = seed if seed is not None else random.randint(0, 2**31)
        self.prompt_change_interval = prompt_change_interval

        self.pipe = None
        self.vae_tiny = None
        self.tokenizer = None
        self.text_encoder = None
        self.unet = None
        self.scheduler = None

        # Cached text embeddings
        self._cached_prompt = None
        self._cached_neg_embeds = None
        self._cached_pos_embeds = None

        # Stats
        self.total_frames = 0
        self.last_gen_time = 0.0
        self.frame_count = 0
        self.prompt_index = 0

        # Overrides
        self.override_prompt = None
        self.override_strength = None
        self.override_steps = None
        self.lock = threading.Lock()
        self.last_good_output = None

    def _fallback_frame(self):
        canvas = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        cv2.rectangle(canvas, (0, 0), (self.width - 1, self.height - 1), (32, 32, 32), 1)
        cv2.putText(
            canvas,
            f"SD GPU{self.gpu_id} LOADING",
            (10, max(24, self.height // 2)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (220, 220, 220),
            2,
            cv2.LINE_AA,
        )
        return canvas

    def load(self):
        """Load the configured img2img pipeline on this GPU."""
        import torch
        from diffusers import AutoPipelineForImage2Image, AutoencoderTiny

        print(f"[gpu{self.gpu_id}] Loading {self.model_id} on {self.device}...")
        pipe = AutoPipelineForImage2Image.from_pretrained(
            self.model_id,
            torch_dtype=torch.float16,
            safety_checker=None,
            requires_safety_checker=False,
            local_files_only=os.environ.get("HF_HUB_OFFLINE") == "1",
        )
        pipe.to(self.device)
        pipe.set_progress_bar_config(disable=True)

        # Replace VAE with TAESD for faster decode
        try:
            if not self.taesd_path:
                raise RuntimeError("TAESD disabled")
            vae_tiny = AutoencoderTiny.from_pretrained(self.taesd_path, torch_dtype=torch.float16)
            vae_tiny.to(self.device)
            pipe.vae = vae_tiny
            print(f"[gpu{self.gpu_id}] TAESD VAE loaded")
        except Exception as e:
            print(f"[gpu{self.gpu_id}] TAESD load failed, using default VAE: {e}")

        # Extract components for manual pipeline
        self.pipe = pipe
        self.tokenizer = pipe.tokenizer
        self.text_encoder = pipe.text_encoder
        self.unet = pipe.unet
        self.scheduler = pipe.scheduler
        self.vae_tiny = pipe.vae

        # Pre-compute negative prompt embeddings (never changes)
        self._cached_neg_embeds = self._encode_prompt(NEGATIVE_PROMPT)

        mem = torch.cuda.memory_allocated() / 1e9
        print(f"[gpu{self.gpu_id}] Ready. VRAM: {mem:.2f} GB")

    def _encode_prompt(self, prompt):
        """Encode a text prompt to embeddings using CLIP text encoder."""
        import torch

        tokens = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=self.tokenizer.model_max_length,
            truncation=True,
            return_tensors="pt",
        )
        with torch.no_grad():
            embeds = self.text_encoder(tokens.input_ids.to(self.device))[0]
        return embeds

    def _get_prompt_embeds(self):
        """Get prompt embeddings, using cache if prompt hasn't changed."""
        prompt = self.override_prompt or self.prompts[self.prompt_index]
        if prompt != self._cached_prompt:
            self._cached_prompt = prompt
            self._cached_pos_embeds = self._encode_prompt(prompt)
        return self._cached_pos_embeds, self._cached_neg_embeds

    def stylize_frame(self, frame_bgr):
        """Stylize a single BGR frame. Returns BGR uint8 or None."""
        import torch
        from PIL import Image

        if frame_bgr is None or frame_bgr.size == 0:
            return None
        if frame_bgr.std() < 1.0:
            return self.last_good_output if self.last_good_output is not None else self._fallback_frame()

        # BGR → RGB → PIL
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        pil_image = Image.fromarray(frame_rgb)

        strength = self.override_strength or self.strength
        steps = self.override_steps or self.num_inference_steps

        t0 = time.time()
        try:
            with torch.no_grad():
                prompt_embeds, negative_prompt_embeds = self._get_prompt_embeds()
                result = self.pipe(
                    image=pil_image,
                    prompt_embeds=prompt_embeds,
                    negative_prompt_embeds=negative_prompt_embeds if self.guidance_scale > 1.0 else None,
                    strength=strength,
                    guidance_scale=self.guidance_scale,
                    num_inference_steps=steps,
                )
        except Exception as e:
            print(f"[gpu{self.gpu_id}] pipeline error: {e}")
            return self.last_good_output if self.last_good_output is not None else self._fallback_frame()

        gen_time = time.time() - t0

        if not getattr(result, "images", None):
            print(f"[gpu{self.gpu_id}] pipeline returned no images")
            return self.last_good_output if self.last_good_output is not None else self._fallback_frame()

        output_np = np.array(result.images[0])
        output_bgr = cv2.cvtColor(output_np, cv2.COLOR_RGB2BGR)

        # Ensure output matches input frame dimensions
        if output_bgr.shape[:2] != frame_bgr.shape[:2]:
            output_bgr = cv2.resize(output_bgr, (frame_bgr.shape[1], frame_bgr.shape[0]),
                                    interpolation=cv2.INTER_LINEAR)

        with self.lock:
            self.total_frames += 1
            self.frame_count += 1
            self.last_gen_time = gen_time
            self.last_good_output = output_bgr
            if self.frame_count > 0 and self.frame_count % self.prompt_change_interval == 0:
                if not self.override_prompt:
                    self.prompt_index = (self.prompt_index + 1) % len(self.prompts)
                    self._cached_prompt = None  # Force re-encode

        return output_bgr

    def get_status(self):
        with self.lock:
            return {
                "gpu_id": self.gpu_id,
                "total_frames": self.total_frames,
                "frame_count": self.frame_count,
                "prompt_index": self.prompt_index,
                "model_id": self.model_id,
                "last_gen_time": self.last_gen_time,
                "override_prompt": self.override_prompt,
                "override_steps": self.override_steps,
                "override_strength": self.override_strength,
            }


# ---------------------------------------------------------------------------
# IPTV frame reader (same as before)
# ---------------------------------------------------------------------------

class IPTVFrameReader:
    """Continuously pulls raw BGR24 frames from a live IPTV HLS stream.

    Keeps a persistent ffmpeg subprocess open and automatically reconnects
    when the stream drops or returns EOF.
    """

    def __init__(self, url, width, height, fps=20):
        self.url = url
        self.width = width
        self.height = height
        self.fps = fps
        self.frame_size = width * height * 3
        self.proc = None
        self.lock = threading.Lock()
        self.total_frames = 0
        self.reconnect_count = 0

    def _start_ffmpeg(self):
        if self.proc:
            try:
                self.proc.kill()
                self.proc.wait(timeout=3)
            except Exception:
                pass

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-fflags", "+genpts", "-flags", "low_delay",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
            "-i", self.url,
            "-vf", f"scale={self.width}:{self.height}:force_original_aspect_ratio=increase,crop={self.width}:{self.height}",
            "-r", str(self.fps),
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
        ]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         bufsize=self.frame_size * 4)
        self.reconnect_count += 1
        print(f"[sd-iptv] ffmpeg started (reconnect #{self.reconnect_count})")

    def read_frame(self, timeout=5.0):
        if self.proc is None:
            self._start_ffmpeg()
            time.sleep(1.0)

        raw = self.proc.stdout.read(self.frame_size)
        if len(raw) == self.frame_size:
            frame = np.frombuffer(raw, dtype=np.uint8).reshape((self.height, self.width, 3))
            with self.lock:
                self.total_frames += 1
            return frame
        elif len(raw) == 0:
            print(f"[sd-iptv] stream ended, reconnecting...")
            self._start_ffmpeg()
            time.sleep(1.0)
            return None
        else:
            remaining = self.frame_size - len(raw)
            extra = self.proc.stdout.read(remaining)
            raw += extra
            if len(raw) == self.frame_size:
                frame = np.frombuffer(raw, dtype=np.uint8).reshape((self.height, self.width, 3))
                with self.lock:
                    self.total_frames += 1
                return frame
            return None

    def close(self):
        if self.proc:
            try:
                self.proc.kill()
                self.proc.wait(timeout=3)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# UDP writer
# ---------------------------------------------------------------------------

def start_udp_writer(width, height, fps, udp_url):
    gop = max(8, int(fps * 2))
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
        "-x264-params", f"repeat-headers=1:keyint={gop}:min-keyint={gop}:scenecut=0",
        "-pix_fmt", "yuv420p",
        "-mpegts_flags", "+resend_headers",
        "-muxdelay", "0", "-muxpreload", "0", "-flush_packets", "1",
        "-f", "mpegts", udp_url,
    ]
    print(f"[sd-iptv] UDP writer: {width}x{height}@{fps}fps -> {udp_url}")
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, bufsize=width * height * 3 * 2)


# ---------------------------------------------------------------------------
# Dual-GPU frame processor with thread pool
# ---------------------------------------------------------------------------

class DualGPUProcessor:
    """Coordinates multiple GPUWorker instances and the shared UDP writer.

    Responsibilities:
      - Start the ffmpeg UDP writer once.
      - Provide a thread-safe write path that restarts the writer on broken pipe.
      - Track how many frames have been written.
    """

    def __init__(self, workers, width, height, fps, udp_url):
        self.workers = workers
        self.width = width
        self.height = height
        self.fps = fps
        self.udp_url = udp_url
        self.writer = None
        self.running = True
        self.total_written = 0
        self.write_lock = threading.Lock()

    def start(self):
        self.writer = start_udp_writer(self.width, self.height, self.fps, self.udp_url)

    def write_frame(self, frame_bgr):
        """Write a frame to the UDP writer."""
        try:
            with self.write_lock:
                self.writer.stdin.write(frame_bgr.tobytes())
                self.writer.stdin.flush()
            self.total_written += 1
        except (BrokenPipeError, IOError):
            print("[sd-iptv] UDP writer pipe broke, restarting...")
            try:
                self.writer.terminate()
                self.writer.wait(timeout=2)
            except Exception:
                pass
            self.writer = start_udp_writer(self.width, self.height, self.fps, self.udp_url)
            try:
                with self.write_lock:
                    self.writer.stdin.write(frame_bgr.tobytes())
                    self.writer.stdin.flush()
                self.total_written += 1
            except Exception:
                print("[sd-iptv] failed to restart writer")

    def stop(self):
        self.running = False
        if self.writer:
            try:
                self.writer.stdin.close()
                self.writer.terminate()
                self.writer.wait(timeout=3)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="IPTV → SD-Turbo DUAL-GPU img2img streamer")
    parser.add_argument("--iptv-url", required=True)
    parser.add_argument("--udp-url", default="udp://127.0.0.1:5000?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1")
    parser.add_argument("--fps", type=int, default=8)
    parser.add_argument("--width", type=int, default=256)
    parser.add_argument("--height", type=int, default=256)
    parser.add_argument("--strength", type=float, default=0.5)
    parser.add_argument("--guidance-scale", type=float, default=1.0)
    parser.add_argument("--steps", type=int, default=2)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--prompt-change-interval", type=int, default=120)
    parser.add_argument("--prompt-file", default=None)
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--control-port", type=int, default=8889)
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID,
                        help="Diffusers img2img model id or local path")
    parser.add_argument("--taesd-path", default=TAESD_PATH,
                        help="TAESD model path/id for faster VAE; ignored with --no-taesd")
    parser.add_argument("--no-taesd", action="store_true",
                        help="Use the model's default VAE instead of TAESD")
    parser.add_argument("--max-ai-frame-age", type=float, default=0.75,
                        help="Maximum seconds to repeat an AI frame before falling back to live source motion")
    parser.add_argument("--stale-fallback", choices=("effect", "source", "blend", "hold"), default="effect",
                        help="What to write when the latest AI frame is stale")
    parser.add_argument("--fallback-source-weight", type=float, default=0.75,
                        help="Source-frame weight for --stale-fallback blend")
    parser.add_argument("--work-queue-size", type=int, default=1,
                        help="Pending frames per GPU; 1 keeps latency lowest")
    parser.add_argument("--replace-queued-frames", action=argparse.BooleanOptionalAction, default=True,
                        help="Replace stale queued GPU work with the newest source frame")
    parser.add_argument("--devices", default="cuda:0,cuda:1",
                        help="Comma-separated CUDA devices for dual GPU")
    args = parser.parse_args()

    min_strength_for_timesteps = 1.0 / max(1, args.steps)
    if args.strength < min_strength_for_timesteps:
        print(
            f"[sd-iptv] strength={args.strength} with steps={args.steps} yields no denoise timesteps; "
            f"using strength={min_strength_for_timesteps:.2f}"
        )
        args.strength = min_strength_for_timesteps

    # Load prompts
    if args.prompt_file:
        with open(args.prompt_file) as pf:
            prompts = json.load(pf)
    elif args.prompt:
        prompts = [args.prompt]
    else:
        prompts = PROMPTS

    devices = args.devices.split(",")

    print(f"[sd-iptv] DUAL-GPU IPTV → SD-Turbo streamer")
    print(f"[sd-iptv] Model: {args.model_id}")
    print(f"[sd-iptv] IPTV source: {args.iptv_url}")
    print(f"[sd-iptv] Resolution: {args.width}x{args.height}")
    print(f"[sd-iptv] Target: {args.fps}fps, Strength: {args.strength}, Steps: {args.steps}")
    print(f"[sd-iptv] GPUs: {devices}")
    print(f"[sd-iptv] TAESD: {'OFF' if args.no_taesd else args.taesd_path}")
    print(f"[sd-iptv] Stale fallback: {args.stale_fallback} after {args.max_ai_frame_age:.2f}s")
    print(f"[sd-iptv] Prompts: {len(prompts)} loaded, change every {args.prompt_change_interval} frames")

    # Load workers on each GPU
    workers = []
    for i, dev in enumerate(devices):
        w = GPUWorker(
            gpu_id=i, device=dev, prompts=prompts,
            width=args.width, height=args.height,
            strength=args.strength, guidance_scale=args.guidance_scale,
            num_inference_steps=args.steps,
            seed=args.seed + i if args.seed else None,
            prompt_change_interval=args.prompt_change_interval,
            model_id=args.model_id,
            taesd_path=None if args.no_taesd else args.taesd_path,
        )
        w.load()
        workers.append(w)

    # Create IPTV reader
    reader = IPTVFrameReader(url=args.iptv_url, width=args.width, height=args.height, fps=args.fps)

    # Create processor
    processor = DualGPUProcessor(workers, args.width, args.height, args.fps, args.udp_url)
    processor.start()

    # Remote control
    running = [True]
    remote = {"workers": workers, "prompts": prompts, "reader": reader, "processor": processor}

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
                if parsed.path == "/status":
                    w0 = remote["workers"][0].get_status()
                    w1 = remote["workers"][1].get_status() if len(remote["workers"]) > 1 else {}
                    self._send_json(200, {
                        "gpu0": w0, "gpu1": w1,
                        "total_written": remote["processor"].total_written,
                        "iptv_frames_read": remote["reader"].total_frames,
                        "iptv_reconnects": remote["reader"].reconnect_count,
                        "running": running[0],
                        "width": args.width, "height": args.height,
                        "fps": args.fps,
                        "steps": args.steps,
                        "strength": args.strength,
                    })
                elif parsed.path == "/prompts":
                    self._send_json(200, {"prompts": remote["prompts"]})
                else:
                    self._send_json(404, {"error": "unknown"})

            def do_POST(self):
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode() if length else "{}"
                try:
                    data = json.loads(body) if body else {}
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "invalid JSON"})
                    return

                for w in remote["workers"]:
                    if parsed.path == "/prompt":
                        p = data.get("prompt", "")
                        if p:
                            with w.lock:
                                w.override_prompt = p
                                w._cached_prompt = None
                    elif parsed.path == "/steps":
                        sp = data.get("steps")
                        if sp is not None:
                            with w.lock:
                                w.override_steps = int(sp)
                    elif parsed.path == "/strength":
                        sp = data.get("strength")
                        if sp is not None:
                            with w.lock:
                                w.override_strength = float(sp)
                    elif parsed.path == "/rotate":
                        with w.lock:
                            w.override_prompt = None
                            w.override_steps = None
                            w.override_strength = None
                            w._cached_prompt = None
                    elif parsed.path == "/next":
                        with w.lock:
                            w.prompt_index = (w.prompt_index + 1) % len(w.prompts)
                            w.override_prompt = None
                            w._cached_prompt = None
                    elif parsed.path == "/channel":
                        url = data.get("url", "")
                        if url:
                            remote["reader"].url = url
                            remote["reader"]._start_ffmpeg()

                if parsed.path == "/prompt":
                    self._send_json(200, {"ok": True, "override_prompt": data.get("prompt", "")})
                elif parsed.path == "/steps":
                    self._send_json(200, {"ok": True, "override_steps": int(data.get("steps", 0))})
                elif parsed.path == "/strength":
                    self._send_json(200, {"ok": True, "override_strength": float(data.get("strength", 0))})
                elif parsed.path == "/rotate":
                    self._send_json(200, {"ok": True, "message": "rotation resumed"})
                elif parsed.path == "/next":
                    self._send_json(200, {"ok": True, "prompt_index": remote["workers"][0].prompt_index})
                elif parsed.path == "/channel":
                    self._send_json(200, {"ok": True, "iptv_url": data.get("url", "")})
                else:
                    self._send_json(404, {"error": "unknown"})

            def do_OPTIONS(self):
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

        def start_control_server(port):
            server = HTTPServer(("0.0.0.0", port), ControlHandler)
            server.timeout = 0.1
            print(f"[sd-iptv] remote control on http://0.0.0.0:{port}")
            while running[0]:
                server.handle_request()
            server.server_close()

        ctrl_thread = threading.Thread(target=start_control_server, args=(args.control_port,), daemon=True)
        ctrl_thread.start()

    # Signal handlers
    def handle_signal(signum, frame):
        running[0] = False
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # --- Parallel GPU workers ---
    # Each GPU runs in its own thread. The main thread reads IPTV frames and
    # distributes them to GPU work queues. A writer thread pulls completed
    # frames from the output queue (in order) and sends to UDP.
    #
    # Architecture:
    #   main thread: IPTV reader → frame N → gpu_work_queue[N % 2]
    #   gpu0 thread: gpu_work_queue[0] → stylize → output_queue
    #   gpu1 thread: gpu_work_queue[1] → stylize → output_queue
    #   writer thread: output_queue (ordered) → UDP
    #
    # Because GPU inference times can vary, frames may complete out of order.
    # The writer thread buffers them in frame_buffer and releases them in
    # strict frame-number order so the output stream stays sequential.

    import queue

    # Work queues: one per GPU, each holds (frame_num, frame_bgr). Keeping this
    # tiny avoids spending GPU time on frames that are already visually stale.
    work_queue_size = max(1, args.work_queue_size)
    work_queues = [queue.Queue(maxsize=work_queue_size) for _ in workers]
    # Output queue: holds (frame_num, frame_bgr) — writer drains in order
    output_queue = queue.Queue(maxsize=16)
    # Live output uses the newest completed frame immediately. Exact source
    # ordering is less important than avoiding held frames in a paced stream.
    latest_completed_count = [0]
    latest_frame_lock = threading.Lock()
    latest_output_frame = {
        "frame": np.zeros((args.height, args.width, 3), dtype=np.uint8),
        "frame_num": -1,
        "updated_at": 0.0,
    }
    latest_source_frame = {
        "frame": np.zeros((args.height, args.width, 3), dtype=np.uint8),
        "frame_num": -1,
        "updated_at": 0.0,
    }
    stale_fallback_count = [0]

    cv2.putText(
        latest_output_frame["frame"],
        "SD-TURBO STARTING",
        (10, max(24, args.height // 2)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (220, 220, 220),
        2,
        cv2.LINE_AA,
    )

    def gpu_worker_thread(worker, work_q):
        """Per-GPU consumer: stylize frames and push to the ordered output queue."""
        while running[0]:
            try:
                item = work_q.get(timeout=2.0)
            except queue.Empty:
                continue
            if item is None:
                break
            frame_num, frame_bgr = item
            stylized = worker.stylize_frame(frame_bgr)
            if stylized is not None:
                output_queue.put((frame_num, stylized))
            work_q.task_done()

    def writer_thread():
        """Collector: publish each completed GPU result as the newest live frame."""
        while running[0]:
            try:
                item = output_queue.get(timeout=2.0)
            except queue.Empty:
                continue
            if item is None:
                break
            frame_num, frame_bgr = item
            with latest_frame_lock:
                latest_output_frame["frame"] = frame_bgr.copy()
                latest_output_frame["frame_num"] = frame_num
                latest_output_frame["updated_at"] = time.time()
                latest_completed_count[0] += 1
            output_queue.task_done()

    def effect_fallback_frame(frame_bgr):
        """Cheap stylized motion frame used only while the AI result is stale."""
        smoothed = cv2.bilateralFilter(frame_bgr, 5, 55, 55)
        poster = (smoothed // 48) * 48 + 24

        hsv = cv2.cvtColor(poster, cv2.COLOR_BGR2HSV)
        hsv[:, :, 1] = np.clip(hsv[:, :, 1].astype(np.int16) * 1.45, 0, 255).astype(np.uint8)
        hsv[:, :, 2] = np.clip(hsv[:, :, 2].astype(np.int16) * 1.10 + 8, 0, 255).astype(np.uint8)
        color = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)

        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 70, 150)
        edge_bgr = np.zeros_like(color)
        edge_bgr[:, :, 1] = edges
        edge_bgr[:, :, 2] = edges // 2
        return cv2.addWeighted(color, 0.86, edge_bgr, 0.42, 0.0)

    def paced_writer_thread():
        """Writes the latest generated frame at the target FPS for stable ingest."""
        frame_interval = 1.0 / max(1, args.fps)
        next_frame_time = time.monotonic()
        while running[0]:
            now = time.time()
            with latest_frame_lock:
                frame_bgr = latest_output_frame["frame"].copy()
                ai_age = now - latest_output_frame["updated_at"] if latest_output_frame["updated_at"] else float("inf")
                source_frame = latest_source_frame["frame"].copy()
                source_ready = latest_source_frame["updated_at"] > 0.0

            if source_ready and args.stale_fallback != "hold" and ai_age > args.max_ai_frame_age:
                if args.stale_fallback == "effect":
                    frame_bgr = effect_fallback_frame(source_frame)
                elif args.stale_fallback == "blend":
                    source_weight = min(1.0, max(0.0, args.fallback_source_weight))
                    frame_bgr = cv2.addWeighted(source_frame, source_weight, frame_bgr, 1.0 - source_weight, 0.0)
                else:
                    frame_bgr = source_frame
                stale_fallback_count[0] += 1
            processor.write_frame(frame_bgr)

            next_frame_time += frame_interval
            sleep_time = next_frame_time - time.monotonic()
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                next_frame_time = time.monotonic()

    # Start GPU worker threads
    gpu_threads = []
    for i, w in enumerate(workers):
        t = threading.Thread(target=gpu_worker_thread, args=(w, work_queues[i]), daemon=True)
        t.start()
        gpu_threads.append(t)

    # Start output collector and paced writer threads
    w_thread = threading.Thread(target=writer_thread, daemon=True)
    w_thread.start()
    paced_thread = threading.Thread(target=paced_writer_thread, daemon=True)
    paced_thread.start()

    print(f"[sd-iptv] starting dual-GPU PARALLEL stream at {args.fps}fps...")
    print(f"[sd-iptv] {len(workers)} GPU workers + 1 collector thread + 1 paced UDP writer")

    frames_since_stats = 0
    skipped_since_stats = 0
    replaced_since_stats = 0
    last_stats_time = time.time()
    frame_num = 0

    def put_latest_frame(work_q, item):
        try:
            work_q.put_nowait(item)
            return True, False
        except queue.Full:
            if not args.replace_queued_frames:
                return False, False
            try:
                work_q.get_nowait()
                work_q.task_done()
            except queue.Empty:
                pass
            try:
                work_q.put_nowait(item)
                return True, True
            except queue.Full:
                return False, False

    try:
        while running[0]:
            frame = reader.read_frame(timeout=5.0)
            if frame is None:
                continue

            with latest_frame_lock:
                latest_source_frame["frame"] = frame.copy()
                latest_source_frame["frame_num"] = frame_num
                latest_source_frame["updated_at"] = time.time()

            # Send each source frame to the least-backed-up GPU queue. This is
            # deliberately not strict round-robin; the live stream wants the
            # freshest finished frame, not perfect frame order.
            gpu_idx = min(range(len(work_queues)), key=lambda idx: work_queues[idx].qsize())
            queued, replaced = put_latest_frame(work_queues[gpu_idx], (frame_num, frame))
            if queued:
                frame_num += 1
                frames_since_stats += 1
                if replaced:
                    replaced_since_stats += 1
            else:
                # GPU is backed up — skip this frame to stay real-time
                skipped_since_stats += 1

            # Stats every 10 seconds
            now = time.time()
            if now - last_stats_time >= 10.0:
                elapsed = now - last_stats_time
                actual_fps = frames_since_stats / elapsed
                w0 = workers[0].get_status()
                w1 = workers[1].get_status() if len(workers) > 1 else {}
                g0_time = w0.get("last_gen_time", 0) * 1000
                g1_time = w1.get("last_gen_time", 0) * 1000
                q0_size = work_queues[0].qsize() if len(workers) > 0 else 0
                q1_size = work_queues[1].qsize() if len(workers) > 1 else 0
                out_q_size = output_queue.qsize()
                with latest_frame_lock:
                    latest_age = now - latest_output_frame["updated_at"] if latest_output_frame["updated_at"] else -1
                    latest_num = latest_output_frame["frame_num"]
                    source_age = now - latest_source_frame["updated_at"] if latest_source_frame["updated_at"] else -1
                    source_num = latest_source_frame["frame_num"]
                stats_msg = (
                    f"[sd-iptv] sent={processor.total_written} queued_fps={actual_fps:.1f} "
                    f"gpu0={g0_time:.0f}ms gpu1={g1_time:.0f}ms "
                    f"gpu0_f={w0.get('total_frames', 0)} gpu1_f={w1.get('total_frames', 0)} "
                    f"q0={q0_size} q1={q1_size} out_q={out_q_size} "
                    f"latest={latest_num} age={latest_age:.1f}s source={source_num} source_age={source_age:.1f}s "
                    f"completed={latest_completed_count[0]} fallback={stale_fallback_count[0]} "
                    f"replaced={replaced_since_stats} skipped={skipped_since_stats} "
                    f"iptv={reader.total_frames} reconnects={reader.reconnect_count}"
                )
                print(stats_msg, flush=True)
                frames_since_stats = 0
                skipped_since_stats = 0
                replaced_since_stats = 0
                last_stats_time = now

    except KeyboardInterrupt:
        pass
    finally:
        print("[sd-iptv] shutting down...")
        running[0] = False
        # Signal threads to stop
        for q in work_queues:
            q.put(None)
        output_queue.put(None)
        reader.close()
        processor.stop()
        print("[sd-iptv] done.")


if __name__ == "__main__":
    main()
