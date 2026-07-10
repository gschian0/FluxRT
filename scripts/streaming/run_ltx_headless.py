#!/usr/bin/env python3
"""
Headless LTX-Video text-to-video streaming generator.

Uses LTX-Video 2B (v0.9.5) from Lightricks to generate actual video clips
with real motion coherence — not an img2img feedback loop.

Generates short video clips (1-2 seconds) back-to-back, crossfades between
them, and pipes frames to UDP 5000 via ffmpeg.  Uses a producer-consumer
architecture: a background thread generates the next clip while the main
thread plays the current one.

Runs on GPU 0 (MusicGen is on GPU 1).

Usage:
    CUDA_VISIBLE_DEVICES=0 python scripts/streaming/run_ltx_headless.py \
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
# Prompt library — same varied set as run_flux_headless.py
# ---------------------------------------------------------------------------

PROMPTS = [
    # --- Animals & nature ---
    "lion walking through tron abyss, glowing neon grid floor, electric blue light trails, majestic mane flowing, cinematic low angle, volumetric fog, vivid cyan and orange contrast, sharp detail",
    "giant octopus gliding through a neon coral reef, bioluminescent tentacles, deep ocean darkness, glowing plankton, cinematic underwater camera, vivid teal and magenta, sharp macro detail",
    "wolf made of fire running across a dark volcanic plain, embers trailing, glowing lava cracks, dramatic silhouette, cinematic wide shot, vivid orange and red, sharp detail",
    "hummingbird hovering in a crystal garden, iridescent feathers, dewdrops on petals, morning sunlight, macro lens, vivid greens and pinks, dreamy bokeh, crisp detail",
    "elephant made of clouds walking across a sunset sky, golden hour light, volumetric clouds, dreamy atmosphere, cinematic wide shot, warm orange and purple palette, soft sharp detail",
    "snake made of liquid mercury slithering through a dark mirror maze, reflective surfaces, chrome highlights, dramatic lighting, cinematic angle, vivid silver and blue, sharp detail",
    "butterfly with stained glass wings flying through a cathedral, luminous backlight, jewel tone colors, dust motes in light beams, macro detail, vivid prismatic colors, crisp frame",
    "whale swimming through a galaxy of stars, cosmic nebula background, bioluminescent skin, vast scale, cinematic deep space shot, vivid purples and blues, dreamy sharp detail",
    # --- Landscapes & environments ---
    "endless desert of black sand with glowing blue rivers of light, towering crystal formations, alien sky with two moons, cinematic landscape, vivid teal and amber, sharp detail",
    "rainforest made of neon circuit boards, glowing fiber optic vines, waterfalls of liquid light, exotic mechanical flowers, cinematic wide shot, vivid greens and cyans, sharp detail",
    "frozen tundra with aurora borealis reflected in ice mirrors, crystalline trees, vast silent landscape, cinematic panorama, vivid greens purples and blues, crisp cold detail",
    "volcanic landscape with rivers of rainbow lava, smoking obsidian rocks, dramatic red sky, cinematic wide angle, vivid reds oranges and electric blue, sharp detail",
    "floating islands in a pastel sky, waterfalls cascading into clouds, ancient ruins, dreamy atmosphere, cinematic aerial shot, vivid pinks and teals, soft sharp detail",
    "underwater city of coral and glass, schools of glowing fish, shafts of sunlight from above, cinematic wide shot, vivid blues and golds, dreamy sharp detail",
    "cherry blossom valley at night with paper lanterns, petals falling in wind, traditional Japanese bridge, cinematic atmosphere, vivid pinks and warm golds, crisp detail",
    "Mars landscape with blue sunset, dust storms, ancient alien ruins, vast red desert, cinematic panorama, vivid rust red and cobalt blue, sharp detail",
    # --- Surreal & dreamlike ---
    "giant eye opening in the sky over a mirror city, clouds reflected in the iris, surreal dreamscape, cinematic wide shot, vivid blues and golds, sharp surreal detail",
    "stairs leading into a painting on a wall, the painting becomes real, Escher-like geometry, surreal atmosphere, cinematic angle, vivid multicolor palette, crisp detail",
    "room where the walls are made of flowing water, furniture floating, light refracting through walls, surreal interior, cinematic wide shot, vivid blues and greens, dreamy sharp detail",
    "giant lotus flower blooming in space, petals made of galaxies, stardust falling, cosmic background, cinematic macro shot, vivid purples pinks and golds, sharp detail",
    "clockwork forest where trees are gears, leaves are brass cogs, steam rising from roots, steampunk atmosphere, cinematic wide shot, warm brass and copper tones, sharp mechanical detail",
    "desert where the sand is made of tiny mirrors, reflecting sky in infinite fragments, heat shimmer, surreal landscape, cinematic panorama, vivid silver and blue, sharp detail",
    # --- Artistic styles ---
    "oil painting of a stormy sea, thick impasto waves, dramatic brush strokes, luminous foam, moody sky, museum quality, vivid blues and whites, sharp textured detail",
    "watercolor of a Japanese koi pond, bleeding pigments, soft washes, lily pads, gentle ripples, textured paper grain, vivid oranges and greens, dreamy crisp detail",
    "ink wash painting of mountains in mist, traditional Chinese style, sharp brush lines, gradient grays, tiny temple on cliff, elegant minimal composition, crisp detail",
    "art nouveau illustration of a peacock, flowing organic lines, gold and emerald colors, decorative border, Alphonse Mucha style, vivid jewel tones, sharp ornamental detail",
    "Bauhaus geometric composition, primary colors, bold shapes, intersecting circles and squares, modernist aesthetic, vivid red blue and yellow, sharp clean edges",
    "psychedelic 1960s poster art, swirling patterns, vibrating colors, peace symbols, flowing typography aesthetic, vivid magenta orange and lime green, sharp bold detail",
    "ukiyo-e woodblock print of a great wave, Hokusai style, dramatic curling water, tiny boats, distant mountain, vivid blue and cream, sharp traditional detail",
    "stained glass window of a phoenix rising, lead outlines, luminous backlight, cathedral setting, vivid reds oranges and golds, sharp crystalline detail",
    # --- Retro & digital ---
    "retro 80s synthwave landscape, chrome sun on horizon, neon grid floor, palm tree silhouettes, VHS aesthetic, vivid magenta and cyan, sharp nostalgic detail",
    "16-bit pixel art RPG village, cozy cottages, pixelated trees, warm torch glow, retro game aesthetic, vivid greens and warm browns, crisp chunky pixels",
    "early 3D CGI render, low-poly landscape, flat shaded polygons, 1990s aesthetic, dithered textures, vivid primary colors, sharp retro digital frame",
    "wireframe vector graphics landscape, glowing green lines on black, Tron-style terrain, retro CRT aesthetic, vivid neon green, sharp digital edges",
    "demoscene fractal landscape, raymarched mountains, infinite zoom, vivid gradient sky, retro computer art, electric blues and purples, sharp mathematical detail",
    # --- Craft & material ---
    "claymation lion walking through a miniature savanna, handmade clay texture, visible fingerprints, tall grass made of felt, warm sunset lighting, macro detail, vivid oranges and golds",
    "origami crane flying through a paper city, crisp folded edges, geometric faceted surfaces, paper buildings, bright studio lighting, vivid colors, sharp macro detail",
    "knitted world, everything made of yarn, fuzzy trees, woven roads, crochet characters, cozy texture, warm lighting, vivid multicolor, sharp macro detail",
    "glass sculpture garden, translucent flowers, prismatic light, internal refractions, dark background, dramatic spotlight, vivid jewel tones, sharp crystalline detail",
    "sand mandala being created, flowing colored sand, intricate geometric patterns, warm amber light, spiritual atmosphere, vivid colors, sharp macro detail",
    # --- Cosmic & abstract ---
    "cosmic background, multicolor ink swirling in zero gravity, nebula clouds, stardust, vivid purples blues and golds, abstract beauty, sharp fluid detail",
    "black hole consuming a star, accretion disk glowing, gravitational lensing, cosmic scale, cinematic shot, vivid oranges and blues, sharp astronomical detail",
    "aurora borealis over a mirror lake, perfect reflection, starry sky, vast silence, cinematic panorama, vivid greens purples and blues, crisp detail",
    "fractal universe, infinite recursive patterns, mandelbrot zoom, psychedelic colors, mathematical beauty, vivid electric spectrum, sharp crystalline detail",
    "liquid gold flowing through a black marble labyrinth, reflective surfaces, dramatic lighting, abstract luxury, vivid gold and black, sharp fluid detail",
    # --- Character variety ---
    "robot gardener tending a holographic garden, glowing plants, mechanical hands, gentle atmosphere, cinematic close-up, vivid greens and warm golds, sharp detail",
    "astronaut floating in a nebula, helmet reflection showing galaxies, tethered to a tiny station, cinematic shot, vivid blues purples and golds, sharp detail",
    "samurai standing in bamboo forest at dusk, fireflies glowing, mist between stalks, cinematic atmosphere, vivid greens and warm oranges, sharp dramatic detail",
    "deep sea diver discovering a glowing ruin, bioluminescent fish, ancient stone arches, cinematic underwater shot, vivid teals and golds, dreamy sharp detail",
    "wizard casting a spell in a crystal cave, glowing runes, floating shards of light, dramatic shadows, cinematic angle, vivid purples and blues, sharp magical detail",
    "claymation, sketch, multicolor ink, cosmic background, handmade character with expressive features, visible fingerprints, tactile texture, vivid colors, crisp macro lens, cinematic depth of field",
]

# ---------------------------------------------------------------------------
# LTX-Video pipeline loader (handles transformers 5.x tokenizer issue)
# ---------------------------------------------------------------------------

LTX_MODEL_ID = "Lightricks/LTX-Video-0.9.5"


def load_ltx_pipeline(device="cuda:0", dtype=None):
    """Load the LTX-Video pipeline with manual tokenizer fix for transformers 5.x."""
    import torch
    if dtype is None:
        dtype = torch.bfloat16
    from transformers import T5Tokenizer, T5EncoderModel
    from diffusers import LTXPipeline

    # Find the cached snapshot path
    from huggingface_hub import snapshot_download
    model_path = snapshot_download(LTX_MODEL_ID, cache_dir="/root/.cache/huggingface/hub")

    print(f"[ltx-stream] Loading tokenizer from {model_path}/tokenizer ...")
    tokenizer = T5Tokenizer.from_pretrained(
        os.path.join(model_path, "tokenizer"), legacy=False
    )

    print(f"[ltx-stream] Loading text encoder (T5) ...")
    text_encoder = T5EncoderModel.from_pretrained(
        os.path.join(model_path, "text_encoder"), torch_dtype=dtype
    )

    print(f"[ltx-stream] Loading LTX pipeline ...")
    pipe = LTXPipeline.from_pretrained(
        LTX_MODEL_ID,
        torch_dtype=dtype,
        tokenizer=tokenizer,
        text_encoder=text_encoder,
    )
    pipe.to(device)

    mem = torch.cuda.memory_allocated() / 1e9
    print(f"[ltx-stream] Pipeline loaded. VRAM: {mem:.2f} GB")
    return pipe


# ---------------------------------------------------------------------------
# ffmpeg UDP writer
# ---------------------------------------------------------------------------

def start_udp_writer(width: int, height: int, fps: int, udp_url: str) -> subprocess.Popen:
    """Start an ffmpeg process that reads raw BGR24 frames from stdin
    and encodes them to MPEG-TS over UDP."""
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
    print(f"[ltx-stream] starting ffmpeg UDP writer: {width}x{height}@{fps}fps -> {udp_url}")
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, bufsize=width * height * 3 * 2)


# ---------------------------------------------------------------------------
# Clip generation worker
# ---------------------------------------------------------------------------

class ClipGenerator:
    """Generates LTX video clips in a background thread."""

    def __init__(self, pipe, prompts, width=768, height=512, num_frames=25,
                 num_inference_steps=8, guidance_scale=3.0, frame_rate=24,
                 seed=None, prompt_change_interval=1, crossfade_frames=5,
                 device="cuda:0"):
        self.pipe = pipe
        self.prompts = list(prompts)
        self.width = width
        self.height = height
        self.num_frames = num_frames
        self.num_inference_steps = num_inference_steps
        self.guidance_scale = guidance_scale
        self.frame_rate = frame_rate
        self.seed = seed
        self.prompt_change_interval = prompt_change_interval  # clips per prompt
        self.crossfade_frames = crossfade_frames
        self.device = device

        self.prompt_index = 0
        self.clip_count = 0
        self.current_seed = seed if seed is not None else random.randint(0, 2**31)

        # Override state (set by remote control)
        self.override_prompt = None
        self.override_seed = None
        self.override_steps = None

        # Stats
        self.last_gen_time = 0.0
        self.last_clip_frames = 0
        self.total_clips = 0

        # Lock for thread-safe prompt/seed access
        self.lock = threading.Lock()

    def get_next_prompt(self):
        """Get the next prompt, handling overrides and rotation."""
        with self.lock:
            if self.override_prompt:
                return self.override_prompt

            # Rotate prompt every prompt_change_interval clips
            if self.clip_count > 0 and self.clip_count % self.prompt_change_interval == 0:
                self.prompt_index = (self.prompt_index + 1) % len(self.prompts)

            return self.prompts[self.prompt_index]

    def get_next_seed(self):
        """Get the next seed, handling overrides."""
        with self.lock:
            if self.override_seed is not None:
                return self.override_seed
            # Vary seed each clip for variety
            self.current_seed = random.randint(0, 2**31)
            return self.current_seed

    def generate_clip(self):
        """Generate one video clip. Returns numpy array of shape (N, H, W, 3) RGB."""
        import torch

        prompt = self.get_next_prompt()
        seed = self.get_next_seed()
        steps = self.override_steps if self.override_steps else self.num_inference_steps

        gen = torch.Generator(device=self.device)
        gen.manual_seed(seed)

        t0 = time.time()
        with torch.no_grad():
            result = self.pipe(
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

        print(f"[ltx-stream] clip #{self.total_clips}: {len(frames)} frames in {gen_time:.1f}s "
              f"({len(frames)/gen_time:.1f} fps) prompt=#{self.prompt_index} seed={seed} "
              f"\"{prompt[:60]}...\"")

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
                "last_gen_time": self.last_gen_time,
                "last_clip_frames": self.last_clip_frames,
                "num_frames": self.num_frames,
                "width": self.width,
                "height": self.height,
                "num_inference_steps": self.override_steps or self.num_inference_steps,
                "guidance_scale": self.guidance_scale,
                "frame_rate": self.frame_rate,
                "crossfade_frames": self.crossfade_frames,
                "prompt_change_interval": self.prompt_change_interval,
            }


# ---------------------------------------------------------------------------
# Crossfade helper
# ---------------------------------------------------------------------------

def crossfade_clips(clip_a, clip_b, n_frames):
    """Crossfade the end of clip_a with the beginning of clip_b.

    Returns a new array that is clip_a[:-n] + crossfade + clip_b[n:].
    """
    if n_frames <= 0 or len(clip_a) < n_frames or len(clip_b) < n_frames:
        # Not enough frames — just concatenate
        return np.concatenate([clip_a, clip_b], axis=0)

    # Take the last n_frames of clip_a and first n_frames of clip_b
    tail = clip_a[-n_frames:].astype(np.float32)
    head = clip_b[:n_frames].astype(np.float32)

    # Linear blend
    alphas = np.linspace(0, 1, n_frames, dtype=np.float32).reshape(n_frames, 1, 1, 1)
    blended = (tail * (1 - alphas) + head * alphas).astype(np.uint8)

    # Assemble: clip_a without tail + blended + clip_b without head
    result = np.concatenate([clip_a[:-n_frames], blended, clip_b[n_frames:]], axis=0)
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Headless LTX-Video text-to-video streamer -> UDP")
    parser.add_argument("--udp-url", default="udp://127.0.0.1:5000?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1",
                        help="UDP destination URL for video frames")
    parser.add_argument("--fps", type=int, default=24, help="Output video FPS")
    parser.add_argument("--width", type=int, default=768, help="Generation width")
    parser.add_argument("--height", type=int, default=512, help="Generation height")
    parser.add_argument("--num-frames", type=int, default=25, help="Frames per clip")
    parser.add_argument("--steps", type=int, default=8, help="Inference steps per clip")
    parser.add_argument("--guidance-scale", type=float, default=3.0, help="CFG guidance scale")
    parser.add_argument("--seed", type=int, default=None, help="Fixed seed (default: random per clip)")
    parser.add_argument("--prompt-change-interval", type=int, default=1,
                        help="Change prompt every N clips (default: 1 = every clip)")
    parser.add_argument("--crossfade-frames", type=int, default=5,
                        help="Number of frames to crossfade between clips")
    parser.add_argument("--prompt-file", default=None,
                        help="JSON file with list of prompts (defaults to built-in PROMPTS)")
    parser.add_argument("--prompt", default=None, help="Use a single prompt")
    parser.add_argument("--control-port", type=int, default=8889,
                        help="HTTP port for remote control (0=disable)")
    parser.add_argument("--device", default="cuda:0", help="CUDA device (default: cuda:0)")
    parser.add_argument("--output-width", type=int, default=None,
                        help="Output width to ffmpeg (default: same as generation width)")
    parser.add_argument("--output-height", type=int, default=None,
                        help="Output height to ffmpeg (default: same as generation height)")
    parser.add_argument("--enable-shader", action="store_true", default=False,
                        help="Enable GLSL shader post-processing")
    parser.add_argument("--shader-dir", default=None,
                        help="Directory of .glsl shader files to rotate through")
    parser.add_argument("--shader-warp", type=float, default=0.4)
    parser.add_argument("--shader-chroma", type=float, default=0.3)
    parser.add_argument("--shader-displace", type=float, default=0.4)
    parser.add_argument("--shader-change-interval", type=int, default=300)
    parser.add_argument("--no-hot-reload", action="store_true", default=False)
    parser.add_argument("--temporal-blend", type=float, default=0.6)
    parser.add_argument("--motion-blur", type=float, default=0.3)
    parser.add_argument("--motion-intensity", type=float, default=0.7)
    parser.add_argument("--buffer-size", type=int, default=2,
                        help="Number of clips to buffer ahead (default: 2)")
    args = parser.parse_args()

    # Load prompts
    if args.prompt_file:
        with open(args.prompt_file) as pf:
            prompts = json.load(pf)
    elif args.prompt:
        prompts = [args.prompt]
    else:
        prompts = PROMPTS

    out_w = args.output_width or args.width
    out_h = args.output_height or args.height
    need_resize = (out_w != args.width or out_h != args.height)

    print(f"[ltx-stream] LTX-Video text-to-video streamer")
    print(f"[ltx-stream] Resolution: {args.width}x{args.height} -> output {out_w}x{out_h}")
    print(f"[ltx-stream] Clips: {args.num_frames} frames @ {args.fps}fps = {args.num_frames/args.fps:.1f}s per clip")
    print(f"[ltx-stream] Steps: {args.steps}, Guidance: {args.guidance_scale}")
    print(f"[ltx-stream] Crossfade: {args.crossfade_frames} frames")
    print(f"[ltx-stream] Prompts: {len(prompts)} loaded, change every {args.prompt_change_interval} clip(s)")
    print(f"[ltx-stream] Buffer: {args.buffer_size} clips ahead")

    # Load LTX pipeline
    print("[ltx-stream] Loading LTX-Video pipeline...")
    pipe = load_ltx_pipeline(device=args.device)

    # Create clip generator
    generator = ClipGenerator(
        pipe=pipe,
        prompts=prompts,
        width=args.width,
        height=args.height,
        num_frames=args.num_frames,
        num_inference_steps=args.steps,
        guidance_scale=args.guidance_scale,
        frame_rate=args.fps,
        seed=args.seed,
        prompt_change_interval=args.prompt_change_interval,
        crossfade_frames=args.crossfade_frames,
        device=args.device,
    )

    # Start GLSL shader processor (optional)
    shader = None
    if args.enable_shader:
        try:
            sys.path.insert(0, os.path.dirname(__file__))
            from glsl_shader_processor import ShaderProcessor
            shader = ShaderProcessor(
                width=out_w, height=out_h,
                shader_dir=args.shader_dir,
                warp_amount=args.shader_warp,
                chroma_amount=args.shader_chroma,
                displace_amount=args.shader_displace,
                shader_change_interval=args.shader_change_interval,
                hot_reload=not args.no_hot_reload,
                temporal_blend=args.temporal_blend,
                motion_blur=args.motion_blur,
                motion_intensity=args.motion_intensity,
            )
            shader.start()
            print(f"[ltx-stream] GLSL shader enabled: warp={args.shader_warp} "
                  f"chroma={args.shader_chroma} displace={args.shader_displace} "
                  f"blend={args.temporal_blend} blur={args.motion_blur} "
                  f"motion={args.motion_intensity}")
        except Exception as e:
            print(f"[ltx-stream] WARNING: shader init failed: {e}, continuing without shader")
            shader = None

    # Start UDP writer
    writer = start_udp_writer(out_w, out_h, args.fps, args.udp_url)

    # --- Clip buffer (producer-consumer) ---
    clip_queue = deque(maxlen=args.buffer_size + 2)
    queue_lock = threading.Lock()
    queue_cond = threading.Condition(queue_lock)
    running = [True]

    def producer_loop():
        """Background thread: continuously generate clips and put them in the queue."""
        while running[0]:
            try:
                frames, prompt, seed = generator.generate_clip()
                with queue_cond:
                    # If queue is full, wait (backpressure)
                    while len(clip_queue) >= args.buffer_size + 1 and running[0]:
                        queue_cond.wait(timeout=1.0)
                    clip_queue.append((frames, prompt, seed))
                    queue_cond.notify()
            except Exception as e:
                print(f"[ltx-stream] producer error: {e}")
                time.sleep(1.0)

    # Start producer thread
    producer = threading.Thread(target=producer_loop, daemon=True)
    producer.start()

    # --- Remote control HTTP server ---
    remote = {
        "generator": generator,
        "shader": shader,
        "prompts": prompts,
    }

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
                    if remote["shader"] is not None:
                        status["shader"] = remote["shader"].get_status()
                    self._send_json(200, status)
                elif parsed.path == "/prompts":
                    self._send_json(200, {"prompts": remote["prompts"]})
                elif parsed.path == "/shaders":
                    if remote["shader"] is not None:
                        self._send_json(200, remote["shader"].get_status())
                    else:
                        self._send_json(200, {"shader": "disabled"})
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

                elif parsed.path == "/shader":
                    sh = remote["shader"]
                    if sh is None:
                        self._send_json(400, {"error": "shader not enabled"})
                        return
                    action = data.get("action", "")
                    if action == "next":
                        sh.next_shader()
                        self._send_json(200, {"ok": True, **sh.get_status()})
                    elif action == "prev":
                        sh.prev_shader()
                        self._send_json(200, {"ok": True, **sh.get_status()})
                    elif action == "set":
                        name = data.get("name", "")
                        if sh.set_shader(name):
                            self._send_json(200, {"ok": True, **sh.get_status()})
                        else:
                            self._send_json(400, {"error": f"shader '{name}' not found"})
                    elif action == "params":
                        w = data.get("warp")
                        c = data.get("chroma")
                        d = data.get("displace")
                        tb = data.get("temporal_blend")
                        mb = data.get("motion_blur")
                        mi = data.get("motion_intensity")
                        if w is not None: sh.set_warp(float(w))
                        if c is not None: sh.set_chroma(float(c))
                        if d is not None: sh.set_displace(float(d))
                        if tb is not None: sh.set_temporal_blend(float(tb))
                        if mb is not None: sh.set_motion_blur(float(mb))
                        if mi is not None: sh.set_motion_intensity(float(mi))
                        self._send_json(200, {"ok": True, **sh.get_status()})
                    elif action == "interval":
                        interval = data.get("shader_change_interval")
                        if interval is not None:
                            sh.shader_change_interval = int(interval)
                        self._send_json(200, {"ok": True, **sh.get_status()})
                    else:
                        self._send_json(400, {"error": "unknown action", "actions": "next, prev, set, params, interval"})
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
            print(f"[ltx-stream] remote control server on http://0.0.0.0:{port}")
            print(f"[ltx-stream]   GET  /status    — current state")
            print(f"[ltx-stream]   GET  /prompts   — list all prompts")
            print(f"[ltx-stream]   POST /prompt    — set single override prompt")
            print(f"[ltx-stream]   POST /prompts   — replace entire prompt list")
            print(f"[ltx-stream]   POST /seed      — set fixed seed")
            print(f"[ltx-stream]   POST /steps     — set steps")
            print(f"[ltx-stream]   POST /next      — jump to next prompt")
            print(f"[ltx-stream]   POST /rotate    — resume auto rotation")
            print(f"[ltx-stream]   POST /interval  — change prompt rotation interval")
            print(f"[ltx-stream]   GET  /shaders   — shader status")
            print(f"[ltx-stream]   POST /shader    — actions: next, prev, set, params, interval")
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
    print(f"[ltx-stream] waiting for first clip to generate...")

    # Wait for first clip
    with queue_cond:
        while len(clip_queue) == 0 and running[0]:
            queue_cond.wait(timeout=1.0)

    if not running[0]:
        print("[ltx-stream] shutdown before first clip")
        return

    print(f"[ltx-stream] first clip ready, starting playback at {args.fps}fps")

    prev_clip = None
    prev_prompt = None
    prev_seed = None
    total_frames_played = 0
    frames_since_stats = 0
    last_stats_time = time.time()
    frame_interval = 1.0 / args.fps

    try:
        while running[0]:
            # Get next clip from queue
            with queue_cond:
                while len(clip_queue) == 0 and running[0]:
                    queue_cond.wait(timeout=1.0)
                if not running[0]:
                    break
                clip_frames, clip_prompt, clip_seed = clip_queue.popleft()
                queue_cond.notify()  # wake up producer

            # Crossfade with previous clip if available
            if prev_clip is not None and args.crossfade_frames > 0:
                play_frames = crossfade_clips(prev_clip, clip_frames, args.crossfade_frames)
            else:
                play_frames = clip_frames

            # Play frames at target FPS
            for i in range(len(play_frames)):
                if not running[0]:
                    break

                frame = play_frames[i]  # RGB uint8 (H, W, 3)

                # Resize if needed
                if need_resize:
                    frame = cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_LANCZOS4)

                # Apply GLSL shader post-processing if enabled
                if shader is not None and shader._initialized:
                    try:
                        frame = shader.process(frame)
                    except Exception as e:
                        print(f"[ltx-stream] shader error: {e}")

                # Convert RGB -> BGR for ffmpeg
                frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

                try:
                    writer.stdin.write(frame_bgr.tobytes())
                    writer.stdin.flush()
                except (BrokenPipeError, IOError):
                    print("[ltx-stream] UDP writer pipe broke, restarting...")
                    try:
                        writer.terminate()
                        writer.wait(timeout=2)
                    except Exception:
                        pass
                    writer = start_udp_writer(out_w, out_h, args.fps, args.udp_url)

                total_frames_played += 1
                frames_since_stats += 1

                # Pace to target FPS
                time.sleep(frame_interval)

            # Save current clip for next crossfade
            prev_clip = clip_frames
            prev_prompt = clip_prompt
            prev_seed = clip_seed

            # Stats every 10 seconds
            now = time.time()
            if now - last_stats_time >= 10.0:
                elapsed = now - last_stats_time
                actual_fps = frames_since_stats / elapsed
                gen_status = generator.get_status()
                print(f"[ltx-stream] played={total_frames_played} fps={actual_fps:.1f} "
                      f"queue={len(clip_queue)} clips={gen_status['total_clips']} "
                      f"last_gen={gen_status['last_gen_time']:.1f}s "
                      f"prompt=#{gen_status['prompt_index']}")
                frames_since_stats = 0
                last_stats_time = now

    except KeyboardInterrupt:
        pass
    finally:
        print("[ltx-stream] shutting down...")
        running[0] = False
        with queue_cond:
            queue_cond.notify_all()

        if shader is not None:
            try:
                shader.stop()
                print("[ltx-stream] shader stopped")
            except Exception:
                pass

        try:
            writer.stdin.close()
            writer.terminate()
            writer.wait(timeout=3)
        except Exception:
            pass

        producer.join(timeout=5)
        print("[ltx-stream] done.")


if __name__ == "__main__":
    main()
