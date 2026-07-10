#!/usr/bin/env python3
"""
Headless Flux video generator.

Loads the StreamProcessor (Flux.2-klein-4B with int8 quantization),
continuously generates images, and pipes them as a video stream to
UDP 5000 via ffmpeg.  This replaces the synthetic test-pattern source
in the fanout pipeline.

Runs on GPU 0 (MusicGen is on GPU 1).

Usage:
    CUDA_VISIBLE_DEVICES=0 python scripts/streaming/run_flux_headless.py \
        --config configs/stream_demo_config.json \
        --udp-url udp://127.0.0.1:5000?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1 \
        --fps 8
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

import cv2
import numpy as np

# Ensure src/ and scripts/streaming/ are on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from fluxrt.stream_processor.stream_processor import StreamProcessor


# ---------------------------------------------------------------------------
# Prompt library — 50 highly varied visual styles rotating through the stream
# Animals, landscapes, surreal scenes, abstract, some aliens — not just aliens
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
    # --- Character variety (not always alien) ---
    "robot gardener tending a holographic garden, glowing plants, mechanical hands, gentle atmosphere, cinematic close-up, vivid greens and warm golds, sharp detail",
    "astronaut floating in a nebula, helmet reflection showing galaxies, tethered to a tiny station, cinematic shot, vivid blues purples and golds, sharp detail",
    "samurai standing in bamboo forest at dusk, fireflies glowing, mist between stalks, cinematic atmosphere, vivid greens and warm oranges, sharp dramatic detail",
    "deep sea diver discovering a glowing ruin, bioluminescent fish, ancient stone arches, cinematic underwater shot, vivid teals and golds, dreamy sharp detail",
    "wizard casting a spell in a crystal cave, glowing runes, floating shards of light, dramatic shadows, cinematic angle, vivid purples and blues, sharp magical detail",
    "claymation, sketch, multicolor ink, cosmic background, handmade character with expressive features, visible fingerprints, tactile texture, vivid colors, crisp macro lens, cinematic depth of field",
]


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
    print(f"[flux-headless] starting ffmpeg UDP writer: {width}x{height}@{fps}fps -> {udp_url}")
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, bufsize=width * height * 3 * 2)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Headless Flux video generator -> UDP")
    parser.add_argument("--config", default="configs/stream_demo_config.json",
                        help="Path to stream processor config JSON")
    parser.add_argument("--udp-url", default="udp://127.0.0.1:5000?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1",
                        help="UDP destination URL for video frames")
    parser.add_argument("--fps", type=int, default=8, help="Output video FPS")
    parser.add_argument("--prompt", default=None, help="Override prompt from config")
    parser.add_argument("--seed", type=int, default=None, help="Override seed from config")
    parser.add_argument("--steps", type=int, default=None, help="Override steps from config")
    parser.add_argument("--seed-change-interval", type=int, default=1,
                        help="Change seed every N frames for visual evolution (0=never, 1=every frame)")
    parser.add_argument("--prompt-change-interval", type=int, default=240,
                        help="Change prompt every N frames (0=use single prompt)")
    parser.add_argument("--prompt-file", default=None,
                        help="JSON file with list of prompts (defaults to built-in PROMPTS)")
    parser.add_argument("--control-port", type=int, default=8888,
                        help="HTTP port for remote prompt control (0=disable)")
    parser.add_argument("--enable-shader", action="store_true", default=False,
                        help="Enable GLSL displacement/warp shader post-processing")
    parser.add_argument("--shader-warp", type=float, default=0.5,
                        help="Shader warp amount (0.0=none, 1.0=strong)")
    parser.add_argument("--shader-chroma", type=float, default=0.5,
                        help="Shader chromatic aberration amount")
    parser.add_argument("--shader-displace", type=float, default=0.5,
                        help="Shader displacement amount")
    parser.add_argument("--shader-dir", default=None,
                        help="Directory of .glsl shader files to rotate through")
    parser.add_argument("--shader-change-interval", type=int, default=600,
                        help="Seconds between shader rotation (0=never)")
    parser.add_argument("--no-hot-reload", action="store_true", default=False,
                        help="Disable shader hot-reload")
    parser.add_argument("--temporal-blend", type=float, default=0.6,
                        help="Temporal blend factor (0.0=sticky, 1.0=instant new frame)")
    parser.add_argument("--motion-blur", type=float, default=0.3,
                        help="Motion blur / trail amount (0.0=none, 1.0=heavy)")
    parser.add_argument("--motion-intensity", type=float, default=0.7,
                        help="Camera motion intensity (0.0=static, 1.0=normal, 2.0=extreme)")
    args = parser.parse_args()

    # Load config to get resolution
    with open(args.config) as f:
        config = json.load(f)
    height = config["resolution"]["height"]
    width = config["resolution"]["width"]
    # Load prompts
    if args.prompt_file:
        with open(args.prompt_file) as pf:
            prompts = json.load(pf)
    elif args.prompt:
        prompts = [args.prompt]
    else:
        prompts = PROMPTS

    prompt = prompts[0]
    seed = args.seed if args.seed is not None else config.get("default_seed", 52)
    steps = args.steps if args.steps is not None else config.get("default_steps", 1)

    print(f"[flux-headless] config: {args.config}")
    print(f"[flux-headless] resolution: {width}x{height}")
    print(f"[flux-headless] prompts: {len(prompts)} loaded")
    print(f"[flux-headless] first prompt: {prompt[:80]}...")
    print(f"[flux-headless] seed={seed}, steps={steps}, fps={args.fps}")
    print(f"[flux-headless] seed_change_interval={args.seed_change_interval}, prompt_change_interval={args.prompt_change_interval}")

    # Start the StreamProcessor
    print("[flux-headless] starting StreamProcessor (loads Flux models)...")
    sp = StreamProcessor(args.config)
    if config.get("enable_int8_quantization", False):
        sp.enable_quantization()
    sp.start()
    sp.set_prompt(prompt)
    sp.set_seed(seed)
    sp.set_steps(steps)

    input_tensor = sp.get_input_tensor()
    output_tensor = sp.get_output_tensor()

    # Wait for the model to be ready (int8 quantization loading takes ~5 min)
    print("[flux-headless] waiting for model to initialise...")
    wait_start = time.time()
    while not sp.is_ready():
        if time.time() - wait_start > 600:
            print("[flux-headless] ERROR: model did not become ready within 600s")
            sp.stop()
            sys.exit(1)
        time.sleep(1)
    print(f"[flux-headless] model ready after {time.time() - wait_start:.1f}s")

    # Start the GLSL shader processor (optional)
    shader = None
    if args.enable_shader:
        try:
            from glsl_shader_processor import ShaderProcessor
            shader = ShaderProcessor(
                width=width, height=height,
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
            print(f"[flux-headless] GLSL shader enabled: warp={args.shader_warp} "
                  f"chroma={args.shader_chroma} displace={args.shader_displace} "
                  f"blend={args.temporal_blend} blur={args.motion_blur} "
                  f"motion={args.motion_intensity} "
                  f"rotate={args.shader_change_interval}s")
        except Exception as e:
            print(f"[flux-headless] WARNING: shader init failed: {e}, continuing without shader")
            shader = None

    # Start the UDP writer
    writer = start_udp_writer(width, height, args.fps, args.udp_url)

    # Feedback loop: start with a random noise frame, then feed each output
    # back as the next input. This creates an evolving video where each frame
    # is a diffusion transformation of the previous one.
    current_frame = np.random.randint(0, 256, (height, width, 3), dtype=np.uint8)

    # Vary the seed every N frames to introduce gradual changes
    seed_change_interval = args.seed_change_interval
    prompt_change_interval = args.prompt_change_interval
    current_seed = seed
    prompt_index = 0

    frame_count = 0
    total_frames = 0
    last_stats_time = time.time()
    running = [True]

    def handle_signal(signum, frame):
        running[0] = False
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print(f"[flux-headless] entering main loop — feedback loop, {len(prompts)} prompts, seed every {seed_change_interval} frame(s), prompt every {prompt_change_interval} frames")

    # --- Remote control state (shared between HTTP handler and main loop) ---
    remote = {
        "prompts": prompts,           # current prompt list (can be swapped remotely)
        "prompt_index": 0,            # current index
        "override_prompt": None,      # if set, use this instead of rotating
        "override_seed": None,        # if set, use this seed instead of random
        "override_steps": None,       # if set, use this many steps
        "prompt_change_interval": prompt_change_interval,
        "seed_change_interval": seed_change_interval,
        "total_frames": 0,
        "current_prompt": prompt,
        "current_seed": current_seed,
        "sp": sp,
        "shader": shader,
    }

    # --- Remote control HTTP server ---
    if args.control_port > 0:
        class ControlHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass  # silence default logging

            def _send_json(self, code, data):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(data, indent=2).encode())

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/status":
                    status = {
                        "total_frames": remote["total_frames"],
                        "current_prompt": remote["current_prompt"],
                        "current_seed": remote["current_seed"],
                        "prompt_index": remote["prompt_index"],
                        "num_prompts": len(remote["prompts"]),
                        "override_prompt": remote["override_prompt"],
                        "override_seed": remote["override_seed"],
                        "override_steps": remote["override_steps"],
                        "prompt_change_interval": remote["prompt_change_interval"],
                        "seed_change_interval": remote["seed_change_interval"],
                    }
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

                if parsed.path == "/prompt":
                    # Set a single override prompt (stops rotation)
                    p = data.get("prompt", "")
                    if p:
                        remote["override_prompt"] = p
                        remote["sp"].set_prompt(p)
                        self._send_json(200, {"ok": True, "override_prompt": p})
                    else:
                        self._send_json(400, {"error": "missing 'prompt'"})

                elif parsed.path == "/prompts":
                    # Replace the entire prompt list (resumes rotation)
                    plist = data.get("prompts", [])
                    if plist and isinstance(plist, list):
                        remote["prompts"] = plist
                        remote["prompt_index"] = 0
                        remote["override_prompt"] = None
                        remote["sp"].set_prompt(plist[0])
                        self._send_json(200, {"ok": True, "num_prompts": len(plist)})
                    else:
                        self._send_json(400, {"error": "missing 'prompts' list"})

                elif parsed.path == "/seed":
                    s = data.get("seed")
                    if s is not None:
                        remote["override_seed"] = int(s)
                        remote["sp"].set_seed(int(s))
                        self._send_json(200, {"ok": True, "override_seed": int(s)})
                    else:
                        self._send_json(400, {"error": "missing 'seed'"})

                elif parsed.path == "/steps":
                    st = data.get("steps")
                    if st is not None:
                        remote["override_steps"] = int(st)
                        remote["sp"].set_steps(int(st))
                        self._send_json(200, {"ok": True, "override_steps": int(st)})
                    else:
                        self._send_json(400, {"error": "missing 'steps'"})

                elif parsed.path == "/rotate":
                    # Resume rotation, clear overrides
                    remote["override_prompt"] = None
                    remote["override_seed"] = None
                    remote["override_steps"] = None
                    self._send_json(200, {"ok": True, "message": "rotation resumed"})

                elif parsed.path == "/next":
                    # Jump to next prompt immediately
                    remote["prompt_index"] = (remote["prompt_index"] + 1) % len(remote["prompts"])
                    np = remote["prompts"][remote["prompt_index"]]
                    remote["override_prompt"] = None
                    remote["sp"].set_prompt(np)
                    self._send_json(200, {"ok": True, "prompt_index": remote["prompt_index"], "prompt": np[:100]})

                elif parsed.path == "/interval":
                    pi = data.get("prompt_change_interval")
                    si = data.get("seed_change_interval")
                    if pi is not None:
                        remote["prompt_change_interval"] = int(pi)
                    if si is not None:
                        remote["seed_change_interval"] = int(si)
                    self._send_json(200, {"ok": True, "prompt_change_interval": remote["prompt_change_interval"], "seed_change_interval": remote["seed_change_interval"]})

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
                        if w is not None:
                            sh.set_warp(float(w))
                        if c is not None:
                            sh.set_chroma(float(c))
                        if d is not None:
                            sh.set_displace(float(d))
                        if tb is not None:
                            sh.set_temporal_blend(float(tb))
                        if mb is not None:
                            sh.set_motion_blur(float(mb))
                        mi = data.get("motion_intensity")
                        if mi is not None:
                            sh.set_motion_intensity(float(mi))
                        self._send_json(200, {"ok": True, **sh.get_status()})
                    elif action == "interval":
                        interval = data.get("shader_change_interval")
                        if interval is not None:
                            sh.shader_change_interval = int(interval)
                        self._send_json(200, {"ok": True, **sh.get_status()})
                    else:
                        self._send_json(400, {"error": "unknown action", "actions": ["next", "prev", "set", "params", "interval"]})

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
            print(f"[flux-headless] remote control server on http://0.0.0.0:{port}")
            print(f"[flux-headless]   GET  /status    — current state")
            print(f"[flux-headless]   GET  /prompts   — list all prompts")
            print(f"[flux-headless]   POST /prompt    — set single override prompt")
            print(f"[flux-headless]   POST /prompts   — replace entire prompt list")
            print(f"[flux-headless]   POST /seed      — set fixed seed")
            print(f"[flux-headless]   POST /steps     — set steps")
            print(f"[flux-headless]   POST /next      — jump to next prompt")
            print(f"[flux-headless]   POST /rotate    — resume auto rotation")
            print(f"[flux-headless]   POST /interval  — change rotation intervals")
            print(f"[flux-headless]   GET  /shaders   — shader status")
            print(f"[flux-headless]   POST /shader    — actions: next, prev, set, params, interval")
            while running[0]:
                server.handle_request()
            server.server_close()

        ctrl_thread = threading.Thread(target=start_control_server, args=(args.control_port,), daemon=True)
        ctrl_thread.start()

    try:
        while running[0]:
            # Sync remote state
            prompts = remote["prompts"]
            prompt_change_interval = remote["prompt_change_interval"]
            seed_change_interval = remote["seed_change_interval"]

            # Rotate prompt (unless override is active)
            if not remote["override_prompt"]:
                if prompt_change_interval > 0 and total_frames > 0 and total_frames % prompt_change_interval == 0:
                    remote["prompt_index"] = (remote["prompt_index"] + 1) % len(prompts)
                    new_prompt = prompts[remote["prompt_index"]]
                    sp.set_prompt(new_prompt)
                    print(f"[flux-headless] prompt rotated to #{remote['prompt_index']}: {new_prompt[:80]}...")

            # Update current_prompt for status reporting
            if remote["override_prompt"]:
                remote["current_prompt"] = remote["override_prompt"]
            else:
                remote["current_prompt"] = prompts[remote["prompt_index"]]

            # Vary seed for visual evolution
            if remote["override_seed"] is not None:
                current_seed = remote["override_seed"]
            elif seed_change_interval > 0 and total_frames > 0 and total_frames % seed_change_interval == 0:
                current_seed = random.randint(0, 2**31)
                sp.set_seed(current_seed)

            remote["current_seed"] = current_seed
            remote["total_frames"] = total_frames

            # Feed the current frame (previous output or initial noise) to Flux
            input_tensor.copy_from(current_frame)

            # Read the processed output
            processed = output_tensor.to_numpy()

            if processed is not None and processed.size > 0 and processed.max() > 0:
                # The output is RGB — use it as the next input (feedback loop)
                current_frame = processed.copy()

                # Apply GLSL shader post-processing if enabled
                if shader is not None and shader._initialized:
                    try:
                        processed = shader.process(processed)
                    except Exception as e:
                        print(f"[flux-headless] shader error: {e}")

                # Convert to BGR for ffmpeg (which expects bgr24)
                frame_bgr = cv2.cvtColor(processed, cv2.COLOR_RGB2BGR)
                try:
                    writer.stdin.write(frame_bgr.tobytes())
                    writer.stdin.flush()
                except (BrokenPipeError, IOError):
                    print("[flux-headless] UDP writer pipe broke, restarting...")
                    try:
                        writer.terminate()
                        writer.wait(timeout=2)
                    except Exception:
                        pass
                    writer = start_udp_writer(width, height, args.fps, args.udp_url)

                frame_count += 1
                total_frames += 1

            # Stats every 10 seconds
            now = time.time()
            if now - last_stats_time >= 10.0:
                elapsed = now - last_stats_time
                actual_fps = frame_count / elapsed
                proc_time = sp.get_last_processing_time()
                print(f"[flux-headless] frames={frame_count} total={total_frames} fps={actual_fps:.1f} "
                      f"proc_time={proc_time:.3f}s last_proc_fps={1/max(proc_time,0.001):.1f} "
                      f"prompt=#{remote['prompt_index']} seed={current_seed}")
                frame_count = 0
                last_stats_time = now

            # Pace the loop — don't spin faster than the model can produce
            # The output scheduler already paces output, but we should also
            # not flood the input tensor. Sleep a tiny bit.
            time.sleep(0.005)

    except KeyboardInterrupt:
        pass
    finally:
        print("[flux-headless] shutting down...")
        if shader is not None:
            try:
                shader.stop()
                print("[flux-headless] shader stopped")
            except Exception:
                pass
        try:
            writer.stdin.close()
            writer.terminate()
            writer.wait(timeout=3)
        except Exception:
            pass
        sp.stop()
        print("[flux-headless] done.")


if __name__ == "__main__":
    main()
