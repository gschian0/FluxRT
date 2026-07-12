#!/usr/bin/env python3
"""
IPTV → SD-Turbo real-time frame-by-frame img2img streamer.

Pulls live video frames from an IPTV HLS stream in real-time, then
AI-stylizes each frame with SD-Turbo (1-step diffusion model) before
sending to UDP → MediaMTX → Twitch.

This gives REAL MOTION from live TV footage + AI art styling at 15-20fps.
Unlike video-to-video models (CogVideoX, LTX), this processes each frame
independently so there's no clip buffering delay — it's truly real-time.

SD-Turbo is a 1-step distilled model: ~50-80ms per frame on L40 GPU.

Usage:
    CUDA_VISIBLE_DEVICES=0 python scripts/streaming/run_sdturbo_iptv.py \
        --iptv-url "https://example.com/stream.m3u8" \
        --udp-url "udp://127.0.0.1:5000?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1" \
        --fps 20 --control-port 8889
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

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

SDTURBO_MODEL_ID = "stabilityai/sd-turbo"
TRT_ENGINE_PATH = "/workspace/FluxRT/models/sd-turbo-unet-trt/engine.plan"
TAESD_MODEL_ID = "madebyollin/taesd"


# ---------------------------------------------------------------------------
# TensorRT UNet wrapper — drop-in replacement for diffusers UNet
# ---------------------------------------------------------------------------

class TRTLogger:
    """Silent TensorRT logger."""
    def __init__(self):
        import tensorrt as trt
        class _Impl(trt.ILogger):
            def __init__(self):
                super().__init__()
            def log(self, severity, msg):
                pass
        self._impl = _Impl()

    def get(self):
        return self._impl


class TensorRTUNetWrapper:
    """Drop-in replacement for diffusers UNet that runs inference via TensorRT.

    Mimics UNet2DConditionModel.__call__:
        unet(sample, timestep, encoder_hidden_states) -> noise_pred
    """

    def __init__(self, engine_path, original_unet=None):
        import tensorrt as trt

        logger = TRTLogger().get()
        runtime = trt.Runtime(logger)
        with open(engine_path, "rb") as f:
            self.engine = runtime.deserialize_cuda_engine(f.read())
        self.context = self.engine.create_execution_context()

        # Get tensor info
        self.input_names = []
        self.output_names = []
        for i in range(self.engine.num_io_tensors):
            name = self.engine.get_tensor_name(i)
            if self.engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT:
                self.input_names.append(name)
            else:
                self.output_names.append(name)

        # Copy config from original UNet
        if original_unet is not None:
            self._config = original_unet.config
        else:
            self._config = type('Config', (), {
                'in_channels': 4, 'sample_size': 32,
                'time_cond_proj_dim': None, 'addition_time_embed_dim': None,
                'projection_class_embeddings_input_dim': 1024,
                'cross_attention_dim': 1024, 'class_embed_type': None,
                'only_cross_attention': False, 'num_class_embeds': None,
                'upcast_attention': False,
            })()

        print(f"  TRT UNet loaded: {len(self.input_names)} inputs, {len(self.output_names)} outputs")
        for name in self.input_names + self.output_names:
            shape = self.engine.get_tensor_shape(name)
            dtype = self.engine.get_tensor_dtype(name)
            print(f"    {name}: shape={shape}, dtype={dtype}")

    def __call__(self, sample, timestep, encoder_hidden_states, **kwargs):
        import torch
        import tensorrt as trt

        sample = sample.half().cuda()
        if timestep.dim() == 0:
            timestep = timestep.unsqueeze(0).half().cuda()
        else:
            timestep = timestep.half().cuda()
        encoder_hidden_states = encoder_hidden_states.half().cuda()

        out_shape = tuple(self.engine.get_tensor_shape(self.output_names[0]))
        output = torch.empty(out_shape, dtype=torch.float16, device='cuda')

        self.context.set_input_shape(self.input_names[0], tuple(sample.shape))
        self.context.set_input_shape(self.input_names[1], tuple(timestep.shape))
        self.context.set_input_shape(self.input_names[2], tuple(encoder_hidden_states.shape))

        self.context.set_tensor_address(self.input_names[0], sample.data_ptr())
        self.context.set_tensor_address(self.input_names[1], timestep.data_ptr())
        self.context.set_tensor_address(self.input_names[2], encoder_hidden_states.data_ptr())
        self.context.set_tensor_address(self.output_names[0], output.data_ptr())

        self.context.execute_async_v3(torch.cuda.current_stream().cuda_stream)
        torch.cuda.synchronize()

        return output

    @property
    def config(self):
        return self._config

# ---------------------------------------------------------------------------
# Artistic style prompts for frame-by-frame restyling
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
# SD-Turbo img2img pipeline
# ---------------------------------------------------------------------------

def load_sdturbo_pipeline(device="cuda:0", use_tensorrt=False, use_taesd=False):
    """Load SD-Turbo img2img pipeline for real-time frame stylization.

    Args:
        device: CUDA device string
        use_tensorrt: If True, replace UNet with TensorRT engine
        use_taesd: If True, replace VAE with TAESD (tiny autoencoder, ~5x faster)
    """
    import torch
    from diffusers import AutoPipelineForImage2Image

    print(f"[sd-iptv] Loading SD-Turbo img2img pipeline...")
    pipe = AutoPipelineForImage2Image.from_pretrained(
        SDTURBO_MODEL_ID,
        torch_dtype=torch.float16,
        safety_checker=None,
        requires_safety_checker=False,
    )
    pipe.to(device)

    # --- TensorRT UNet replacement ---
    if use_tensorrt:
        import os
        if not os.path.exists(TRT_ENGINE_PATH):
            print(f"[sd-iptv] WARNING: TensorRT engine not found at {TRT_ENGINE_PATH}")
            print(f"[sd-iptv] Falling back to PyTorch UNet")
        else:
            print(f"[sd-iptv] Loading TensorRT UNet engine...")
            original_unet = pipe.unet
            trt_unet = TensorRTUNetWrapper(TRT_ENGINE_PATH, original_unet=original_unet)
            pipe.unet = trt_unet
            # Free original UNet from GPU memory
            del original_unet
            torch.cuda.empty_cache()
            print(f"[sd-iptv] TensorRT UNet active (4x faster UNet)")

    # --- TAESD VAE replacement ---
    if use_taesd:
        try:
            from diffusers import AutoencoderTiny
            print(f"[sd-iptv] Loading TAESD (tiny VAE)...")
            taesd_vae = AutoencoderTiny.from_pretrained(TAESD_MODEL_ID, torch_dtype=torch.float16)
            taesd_vae = taesd_vae.to(device)
            pipe.vae = taesd_vae
            print(f"[sd-iptv] TAESD VAE active (~5x faster VAE encode/decode)")
        except Exception as e:
            print(f"[sd-iptv] WARNING: Could not load TAESD: {e}")
            print(f"[sd-iptv] Falling back to standard VAE")
    else:
        # Enable VAE slicing on standard VAE
        try:
            pipe.vae.enable_slicing()
            print(f"[sd-iptv] VAE slicing enabled")
        except Exception:
            pass

    # Optimize for speed
    try:
        pipe.set_progress_bar_config(disable=True)
    except Exception:
        pass

    mem = torch.cuda.memory_allocated() / 1e9
    print(f"[sd-iptv] Pipeline loaded. VRAM: {mem:.2f} GB")
    return pipe


# ---------------------------------------------------------------------------
# IPTV frame reader — continuous real-time frame pull
# ---------------------------------------------------------------------------

class IPTVFrameReader:
    """Continuously pulls frames from a live IPTV HLS stream using ffmpeg.

    Runs ffmpeg in a persistent process, reading raw frames from stdout.
    Automatically reconnects if the stream drops.
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
        """Start or restart the ffmpeg process."""
        if self.proc:
            try:
                self.proc.kill()
                self.proc.wait(timeout=3)
            except Exception:
                pass

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
            stderr=subprocess.DEVNULL,
            bufsize=self.frame_size * 4,
        )
        self.reconnect_count += 1
        print(f"[sd-iptv] ffmpeg started (reconnect #{self.reconnect_count})")

    def read_frame(self, timeout=5.0):
        """Read a single frame from the stream. Returns (N, H, W, 3) BGR or None."""
        if self.proc is None:
            self._start_ffmpeg()
            time.sleep(1.0)  # let ffmpeg buffer up

        t0 = time.time()
        raw = self.proc.stdout.read(self.frame_size)

        if len(raw) == self.frame_size:
            frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                (self.height, self.width, 3)
            )
            with self.lock:
                self.total_frames += 1
            return frame
        elif len(raw) == 0:
            # Stream ended, reconnect
            print(f"[sd-iptv] stream ended, reconnecting...")
            self._start_ffmpeg()
            time.sleep(1.0)
            return None
        else:
            # Partial read, try again
            remaining = self.frame_size - len(raw)
            extra = self.proc.stdout.read(remaining)
            raw += extra
            if len(raw) == self.frame_size:
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                    (self.height, self.width, 3)
                )
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
    print(f"[sd-iptv] starting ffmpeg UDP writer: {width}x{height}@{fps}fps -> {udp_url}")
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, bufsize=width * height * 3 * 2)


# ---------------------------------------------------------------------------
# Realtime feedback shader-style post processing
# ---------------------------------------------------------------------------

class FeedbackShaderProcessor:
    """Fast CPU approximation of a GLSL feedback shader for generated frames."""

    MODES = ("off", "feedback", "prism", "bloom", "edge")
    TRANSFORMS = ("none", "swirl", "tunnel", "kaleidoscope", "orbit3d", "fold3d", "noisewarp")

    def __init__(self, width, height, mode="off", mix=0.65, decay=0.86,
                 zoom=1.012, rotate_degrees=0.35, shift_pixels=2,
                 transform="none", transform_amount=0.45, perspective=0.35,
                 saturation=1.0, contrast=1.0, sharpen=0.0):
        self.width = width
        self.height = height
        self.mode = mode if mode in self.MODES else "off"
        self.transform = transform if transform in self.TRANSFORMS else "none"
        self.mix = float(np.clip(mix, 0.0, 1.0))
        self.decay = float(np.clip(decay, 0.0, 0.98))
        self.zoom = max(1.0, float(zoom))
        self.rotate_degrees = float(rotate_degrees)
        self.shift_pixels = int(max(0, shift_pixels))
        self.transform_amount = float(np.clip(transform_amount, 0.0, 2.0))
        self.perspective = float(np.clip(perspective, 0.0, 1.5))
        self.saturation = float(np.clip(saturation, 0.0, 3.0))
        self.contrast = float(np.clip(contrast, 0.0, 2.5))
        self.sharpen = float(np.clip(sharpen, 0.0, 2.0))
        self.feedback = None
        self.noise_texture = self._create_noise_texture()
        self.frame_index = 0
        self.last_effect_time = 0.0
        self.lock = threading.Lock()

    def configure(self, mode=None, mix=None, decay=None, zoom=None,
                  rotate_degrees=None, shift_pixels=None, transform=None,
                  transform_amount=None, perspective=None, saturation=None,
                  contrast=None, sharpen=None, reset=False):
        with self.lock:
            if mode is not None:
                self.mode = mode if mode in self.MODES else "off"
            if transform is not None:
                self.transform = transform if transform in self.TRANSFORMS else "none"
            if mix is not None:
                self.mix = float(np.clip(float(mix), 0.0, 1.0))
            if decay is not None:
                self.decay = float(np.clip(float(decay), 0.0, 0.98))
            if zoom is not None:
                self.zoom = max(1.0, float(zoom))
            if rotate_degrees is not None:
                self.rotate_degrees = float(rotate_degrees)
            if shift_pixels is not None:
                self.shift_pixels = int(max(0, int(shift_pixels)))
            if transform_amount is not None:
                self.transform_amount = float(np.clip(float(transform_amount), 0.0, 2.0))
            if perspective is not None:
                self.perspective = float(np.clip(float(perspective), 0.0, 1.5))
            if saturation is not None:
                self.saturation = float(np.clip(float(saturation), 0.0, 3.0))
            if contrast is not None:
                self.contrast = float(np.clip(float(contrast), 0.0, 2.5))
            if sharpen is not None:
                self.sharpen = float(np.clip(float(sharpen), 0.0, 2.0))
            if reset:
                self.feedback = None

    def get_status(self):
        with self.lock:
            return {
                "backend": "cpu",
                "mode": self.mode,
                "transform": self.transform,
                "mix": self.mix,
                "decay": self.decay,
                "zoom": self.zoom,
                "rotate_degrees": self.rotate_degrees,
                "shift_pixels": self.shift_pixels,
                "transform_amount": self.transform_amount,
                "perspective": self.perspective,
                "saturation": self.saturation,
                "contrast": self.contrast,
                "sharpen": self.sharpen,
                "frame_index": self.frame_index,
                "last_effect_time": self.last_effect_time,
            }

    def _mirror_repeat(self, values):
        values = np.mod(values, 2.0)
        return np.where(values > 1.0, 2.0 - values, values)

    def _create_noise_texture(self):
        rng = np.random.default_rng(42069)
        noise = rng.random((self.height, self.width, 2), dtype=np.float32)
        noise = cv2.GaussianBlur(noise, (0, 0), max(1.0, min(self.width, self.height) / 42.0))
        detail = rng.random((self.height, self.width, 2), dtype=np.float32)
        detail = cv2.GaussianBlur(detail, (0, 0), max(0.5, min(self.width, self.height) / 96.0))
        return np.clip(noise * 0.72 + detail * 0.28, 0.0, 1.0).astype(np.float32)

    def _sample_noise_texture(self, u, v, offset_u=0.0, offset_v=0.0):
        sample_u = self._mirror_repeat(u + offset_u)
        sample_v = self._mirror_repeat(v + offset_v)
        map_x = (sample_u * (self.width - 1)).astype(np.float32)
        map_y = (sample_v * (self.height - 1)).astype(np.float32)
        return cv2.remap(
            self.noise_texture,
            map_x,
            map_y,
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT,
        )

    def _apply_transform_map(self, u, v, transform, amount, perspective):
        if transform == "none":
            return u, v

        x = u - 0.5
        y = v - 0.5
        radius = np.sqrt(x * x + y * y) + 1e-6
        theta = np.arctan2(y, x)

        if transform == "swirl":
            theta = theta + amount * 4.5 * (1.0 - np.clip(radius * 1.6, 0.0, 1.0))
            u = np.cos(theta) * radius + 0.5
            v = np.sin(theta) * radius + 0.5
        elif transform == "kaleidoscope":
            segments = max(3.0, 3.0 + amount * 9.0)
            sector = (np.pi * 2.0) / segments
            theta = np.abs(np.mod(theta + sector * 0.5, sector) - sector * 0.5)
            u = np.cos(theta) * radius + 0.5
            v = np.sin(theta) * radius + 0.5
        elif transform == "tunnel":
            u = theta / (np.pi * 2.0) + 0.5 + self.frame_index * 0.006
            v = np.log(radius + 0.16) * max(amount, 0.05) + self.frame_index * 0.004
        elif transform in ("orbit3d", "fold3d"):
            if transform == "fold3d":
                segments = max(3.0, 4.0 + amount * 8.0)
                sector = (np.pi * 2.0) / segments
                theta = np.abs(np.mod(theta + sector * 0.5, sector) - sector * 0.5)
                x = np.cos(theta) * radius
                y = np.sin(theta) * radius

            t = self.frame_index * 0.035
            ax = np.sin(t * 0.73) * amount * 0.85
            ay = np.cos(t * 0.51) * amount * 0.85
            z = np.full_like(x, 0.72)
            cy, sy = np.cos(ay), np.sin(ay)
            cx, sx = np.cos(ax), np.sin(ax)
            x2 = x * cy + z * sy
            z2 = -x * sy + z * cy
            y2 = y * cx - z2 * sx
            z3 = y * sx + z2 * cx
            denom = np.maximum(0.25, 1.0 + z3 * perspective)
            u = x2 / denom + 0.5
            v = y2 / denom + 0.5
        elif transform == "noisewarp":
            phase = self.frame_index * 0.012
            broad_noise = self._sample_noise_texture(u, v, phase, -phase * 0.67)
            detail_noise = self._sample_noise_texture(u * 1.9, v * 1.9, -phase * 1.7, phase * 1.31)
            displacement = (broad_noise * 0.72 + detail_noise * 0.28 - 0.5) * amount
            ripple = np.sin((radius * 18.0 - self.frame_index * 0.08) + displacement[..., 0] * 5.0)
            u = u + displacement[..., 0] * 0.16 + np.cos(theta) * ripple * amount * 0.018
            v = v + displacement[..., 1] * 0.16 + np.sin(theta) * ripple * amount * 0.018

        return self._mirror_repeat(u), self._mirror_repeat(v)

    def _warp_feedback(self, feedback, angle, transform, transform_amount, perspective):
        center = (self.width * 0.5, self.height * 0.5)
        matrix = cv2.getRotationMatrix2D(center, angle, self.zoom)
        warped = cv2.warpAffine(
            feedback,
            matrix,
            (self.width, self.height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT,
        )

        if transform == "none":
            return warped

        ys, xs = np.indices((self.height, self.width), dtype=np.float32)
        u = xs / max(1.0, float(self.width - 1))
        v = ys / max(1.0, float(self.height - 1))
        u, v = self._apply_transform_map(u, v, transform, transform_amount, perspective)
        map_x = (u * (self.width - 1)).astype(np.float32)
        map_y = (v * (self.height - 1)).astype(np.float32)
        return cv2.remap(warped, map_x, map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)

    def _shift_channels(self, frame, shift):
        if shift <= 0:
            return frame
        b, g, r = cv2.split(frame)
        b = np.roll(b, -shift, axis=1)
        r = np.roll(r, shift, axis=1)
        g = np.roll(g, shift // 2, axis=0)
        return cv2.merge((b, g, r))

    def _grade_frame(self, frame, saturation, contrast, sharpen):
        if sharpen > 0.0:
            blur = cv2.GaussianBlur(frame, (0, 0), 0.75)
            frame = cv2.addWeighted(frame, 1.0 + sharpen, blur, -sharpen, 0.0)
        if saturation != 1.0:
            luma = frame[..., 0:1] * 0.114 + frame[..., 1:2] * 0.587 + frame[..., 2:3] * 0.299
            frame = luma + (frame - luma) * saturation
        if contrast != 1.0:
            frame = (frame - 127.5) * contrast + 127.5
        return frame

    def process(self, frame_bgr):
        with self.lock:
            mode = self.mode
            transform = self.transform
            mix = self.mix
            decay = self.decay
            rotate_degrees = self.rotate_degrees
            shift_pixels = self.shift_pixels
            transform_amount = self.transform_amount
            perspective = self.perspective
            saturation = self.saturation
            contrast = self.contrast
            sharpen = self.sharpen

        if mode == "off" or frame_bgr is None:
            return frame_bgr

        t0 = time.time()
        frame = frame_bgr.astype(np.float32)
        if self.feedback is None or self.feedback.shape != frame.shape:
            self.feedback = frame.copy()

        angle = np.sin(self.frame_index * 0.047) * rotate_degrees
        warped = self._warp_feedback(self.feedback, angle, transform, transform_amount, perspective)
        current = self._shift_channels(frame, shift_pixels if mode in ("feedback", "prism") else 0)
        blended = cv2.addWeighted(current, mix, warped * decay, 1.0 - mix, 0.0)

        if mode in ("bloom", "prism"):
            bloom = cv2.GaussianBlur(blended, (0, 0), 2.4)
            blended = cv2.addWeighted(blended, 0.78, bloom, 0.32, 0.0)
        if mode == "edge":
            gray = cv2.cvtColor(np.clip(frame, 0, 255).astype(np.uint8), cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, 70, 150)
            edge_bgr = cv2.cvtColor(edges, cv2.COLOR_GRAY2BGR).astype(np.float32)
            blended = cv2.addWeighted(blended, 0.86, edge_bgr, 0.45, 0.0)

        blended = self._grade_frame(blended, saturation, contrast, sharpen)

        self.feedback = blended
        self.frame_index += 1
        out = np.clip(blended, 0, 255).astype(np.uint8)
        with self.lock:
            self.last_effect_time = time.time() - t0
        return out


GLSL_VERTEX_SHADER = """
#version 330
in vec2 in_pos;
in vec2 in_uv;
out vec2 uv;

void main() {
    uv = in_uv;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""


GLSL_FEEDBACK_FRAGMENT_SHADER = """
#version 330
uniform sampler2D current_tex;
uniform sampler2D feedback_tex;
uniform sampler2D noise_tex;
uniform int mode_id;
uniform int frame_index;
uniform float mix_amount;
uniform float decay;
uniform float zoom_amount;
uniform float rotate_degrees;
uniform float shift_pixels;
uniform int transform_id;
uniform float transform_amount;
uniform float perspective_amount;
uniform float saturation_amount;
uniform float contrast_amount;
uniform float sharpen_amount;
uniform vec2 texel;
in vec2 uv;
out vec4 fragColor;

const float PI = 3.141592653589793;

vec2 mirrorRepeat(vec2 p) {
    vec2 q = mod(p, 2.0);
    q = mix(q, 2.0 - q, step(vec2(1.0), q));
    return clamp(q, vec2(0.001), vec2(0.999));
}

float luma(vec3 color) {
    return dot(color, vec3(0.2126, 0.7152, 0.0722));
}

vec3 gradeColor(vec3 color) {
    float y = luma(color);
    color = vec3(y) + (color - vec3(y)) * saturation_amount;
    color = (color - vec3(0.5)) * contrast_amount + vec3(0.5);
    return clamp(color, 0.0, 1.0);
}

vec3 sharpenSample(sampler2D tex, vec2 p, vec3 color) {
    if (sharpen_amount <= 0.0) {
        return color;
    }
    vec3 blur = texture(tex, p + vec2( texel.x, 0.0)).rgb;
    blur += texture(tex, p + vec2(-texel.x, 0.0)).rgb;
    blur += texture(tex, p + vec2(0.0,  texel.y)).rgb;
    blur += texture(tex, p + vec2(0.0, -texel.y)).rgb;
    blur *= 0.25;
    return clamp(color + (color - blur) * sharpen_amount, 0.0, 1.0);
}

vec2 kaleidoscopeUv(vec2 p, float segments) {
    vec2 c = p - vec2(0.5);
    float radius = length(c);
    float theta = atan(c.y, c.x);
    float sector = (2.0 * PI) / max(3.0, segments);
    theta = abs(mod(theta + sector * 0.5, sector) - sector * 0.5);
    return vec2(cos(theta), sin(theta)) * radius + vec2(0.5);
}

vec2 project3dUv(vec2 p, float folded) {
    vec2 c = p - vec2(0.5);
    if (folded > 0.5) {
        vec2 k = kaleidoscopeUv(p, 4.0 + transform_amount * 8.0) - vec2(0.5);
        c = k;
    }

    float t = float(frame_index) * 0.035;
    float ax = sin(t * 0.73) * transform_amount * 0.85;
    float ay = cos(t * 0.51) * transform_amount * 0.85;
    float cx = cos(ax);
    float sx = sin(ax);
    float cy = cos(ay);
    float sy = sin(ay);
    vec3 v = vec3(c, 0.72);
    v.xz = mat2(cy, sy, -sy, cy) * v.xz;
    v.yz = mat2(cx, -sx, sx, cx) * v.yz;
    float denom = max(0.25, 1.0 + v.z * perspective_amount);
    return v.xy / denom + vec2(0.5);
}

vec2 noiseWarpUv(vec2 p) {
    vec2 centered = p - vec2(0.5);
    float radius = length(centered) + 0.0001;
    float angle = atan(centered.y, centered.x);
    float phase = float(frame_index) * 0.012;
    vec2 broad = texture(noise_tex, mirrorRepeat(p + vec2(phase, -phase * 0.67))).rg;
    vec2 detail = texture(noise_tex, mirrorRepeat(p * 1.9 + vec2(-phase * 1.7, phase * 1.31))).rg;
    vec2 displacement = ((broad * 0.72 + detail * 0.28) - vec2(0.5)) * transform_amount;
    float ripple = sin(radius * 18.0 - float(frame_index) * 0.08 + displacement.x * 5.0);
    vec2 radial = vec2(cos(angle), sin(angle)) * ripple * transform_amount * 0.018;
    return p + displacement * 0.16 + radial;
}

vec2 transformFeedbackUv(vec2 p) {
    if (transform_id == 1) {
        vec2 c = p - vec2(0.5);
        float radius = length(c) + 0.0001;
        float theta = atan(c.y, c.x) + transform_amount * 4.5 * (1.0 - clamp(radius * 1.6, 0.0, 1.0));
        p = vec2(cos(theta), sin(theta)) * radius + vec2(0.5);
    } else if (transform_id == 2) {
        vec2 c = p - vec2(0.5);
        float radius = length(c) + 0.0001;
        float theta = atan(c.y, c.x);
        p = vec2(theta / (2.0 * PI) + 0.5 + float(frame_index) * 0.006,
                 log(radius + 0.16) * max(transform_amount, 0.05) + float(frame_index) * 0.004);
    } else if (transform_id == 3) {
        p = kaleidoscopeUv(p, 3.0 + transform_amount * 9.0);
    } else if (transform_id == 4) {
        p = project3dUv(p, 0.0);
    } else if (transform_id == 5) {
        p = project3dUv(p, 1.0);
    } else if (transform_id == 6) {
        p = noiseWarpUv(p);
    }
    return mirrorRepeat(p);
}

vec3 chromaShift(sampler2D tex, vec2 p, float amount) {
    vec2 dx = vec2(amount * texel.x, 0.0);
    vec2 dy = vec2(0.0, amount * 0.5 * texel.y);
    float r = texture(tex, p + dx).r;
    float g = texture(tex, p + dy).g;
    float b = texture(tex, p - dx).b;
    return vec3(r, g, b);
}

vec3 bloomSample(sampler2D tex, vec2 p) {
    vec3 acc = texture(tex, p).rgb * 0.28;
    acc += texture(tex, p + vec2( texel.x * 2.0, 0.0)).rgb * 0.12;
    acc += texture(tex, p + vec2(-texel.x * 2.0, 0.0)).rgb * 0.12;
    acc += texture(tex, p + vec2(0.0,  texel.y * 2.0)).rgb * 0.12;
    acc += texture(tex, p + vec2(0.0, -texel.y * 2.0)).rgb * 0.12;
    acc += texture(tex, p + vec2( texel.x * 3.0,  texel.y * 3.0)).rgb * 0.06;
    acc += texture(tex, p + vec2(-texel.x * 3.0,  texel.y * 3.0)).rgb * 0.06;
    acc += texture(tex, p + vec2( texel.x * 3.0, -texel.y * 3.0)).rgb * 0.06;
    acc += texture(tex, p + vec2(-texel.x * 3.0, -texel.y * 3.0)).rgb * 0.06;
    return acc;
}

float sobelEdge(sampler2D tex, vec2 p) {
    float tl = luma(texture(tex, p + texel * vec2(-1.0, -1.0)).rgb);
    float tc = luma(texture(tex, p + texel * vec2( 0.0, -1.0)).rgb);
    float tr = luma(texture(tex, p + texel * vec2( 1.0, -1.0)).rgb);
    float ml = luma(texture(tex, p + texel * vec2(-1.0,  0.0)).rgb);
    float mr = luma(texture(tex, p + texel * vec2( 1.0,  0.0)).rgb);
    float bl = luma(texture(tex, p + texel * vec2(-1.0,  1.0)).rgb);
    float bc = luma(texture(tex, p + texel * vec2( 0.0,  1.0)).rgb);
    float br = luma(texture(tex, p + texel * vec2( 1.0,  1.0)).rgb);
    float gx = -tl - 2.0 * ml - bl + tr + 2.0 * mr + br;
    float gy = -tl - 2.0 * tc - tr + bl + 2.0 * bc + br;
    return clamp(length(vec2(gx, gy)) * 1.4, 0.0, 1.0);
}

void main() {
    if (mode_id == 0) {
        fragColor = vec4(texture(current_tex, uv).rgb, 1.0);
        return;
    }

    float angle = sin(float(frame_index) * 0.047) * radians(rotate_degrees);
    float c = cos(angle);
    float s = sin(angle);
    mat2 rot = mat2(c, -s, s, c);
    vec2 centered = uv - vec2(0.5);
    vec2 feedback_uv = rot * (centered / max(zoom_amount, 1.0)) + vec2(0.5);
    feedback_uv = transformFeedbackUv(feedback_uv);

    vec3 current = texture(current_tex, uv).rgb;
    if (mode_id == 1 || mode_id == 2) {
        current = chromaShift(current_tex, uv, shift_pixels);
    }
    current = sharpenSample(current_tex, uv, current);

    vec3 history = texture(feedback_tex, feedback_uv).rgb * decay;
    vec3 color = mix(history, current, mix_amount);

    if (mode_id == 2 || mode_id == 3) {
        vec3 bloom = bloomSample(current_tex, uv);
        color = color * 0.82 + bloom * 0.36;
    }
    if (mode_id == 4) {
        float edge = sobelEdge(current_tex, uv);
        color = color * 0.86 + vec3(edge) * 0.52;
    }

    color = gradeColor(color);
    fragColor = vec4(clamp(color, 0.0, 1.0), 1.0);
}
"""


class ModernGLFeedbackShaderProcessor(FeedbackShaderProcessor):
    """Actual GLSL fragment-shader feedback processor using ModernGL/EGL."""

    MODE_IDS = {"off": 0, "feedback": 1, "prism": 2, "bloom": 3, "edge": 4}
    TRANSFORM_IDS = {
        "none": 0,
        "swirl": 1,
        "tunnel": 2,
        "kaleidoscope": 3,
        "orbit3d": 4,
        "fold3d": 5,
        "noisewarp": 6,
    }

    def __init__(self, width, height, mode="off", mix=0.65, decay=0.86,
                 zoom=1.012, rotate_degrees=0.35, shift_pixels=2,
                 transform="none", transform_amount=0.45, perspective=0.35,
                 saturation=1.0, contrast=1.0, sharpen=0.0,
                 backend="egl"):
        super().__init__(
            width,
            height,
            mode,
            mix,
            decay,
            zoom,
            rotate_degrees,
            shift_pixels,
            transform,
            transform_amount,
            perspective,
            saturation,
            contrast,
            sharpen,
        )
        import moderngl

        self.ctx = moderngl.create_standalone_context(require=330, backend=backend)
        self.program = self.ctx.program(
            vertex_shader=GLSL_VERTEX_SHADER,
            fragment_shader=GLSL_FEEDBACK_FRAGMENT_SHADER,
        )
        vertices = np.array([
            -1.0, -1.0, 0.0, 0.0,
             1.0, -1.0, 1.0, 0.0,
            -1.0,  1.0, 0.0, 1.0,
             1.0,  1.0, 1.0, 1.0,
        ], dtype="f4")
        self.vbo = self.ctx.buffer(vertices.tobytes())
        self.vao = self.ctx.vertex_array(self.program, [(self.vbo, "2f 2f", "in_pos", "in_uv")])
        self.current_tex = self.ctx.texture((width, height), 3, dtype="f1")
        self.feedback_tex = self.ctx.texture((width, height), 3, dtype="f1")
        self.output_tex = self.ctx.texture((width, height), 3, dtype="f1")
        noise_pixels = np.clip(self.noise_texture * 255.0, 0.0, 255.0).astype(np.uint8)
        self.noise_tex = self.ctx.texture((width, height), 2, data=noise_pixels.tobytes(), dtype="f1")
        self.framebuffer = self.ctx.framebuffer(color_attachments=[self.output_tex])
        self.current_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.feedback_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.output_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.noise_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.current_tex.use(0)
        self.feedback_tex.use(1)
        self.noise_tex.use(2)
        self.program["current_tex"].value = 0
        self.program["feedback_tex"].value = 1
        self.program["noise_tex"].value = 2
        self.program["texel"].value = (1.0 / width, 1.0 / height)
        self.feedback_ready = False
        self.reset_pending = False
        self.gl_backend = backend
        self.gl_renderer = self.ctx.info.get("GL_RENDERER", "unknown")

    def configure(self, mode=None, mix=None, decay=None, zoom=None,
                  rotate_degrees=None, shift_pixels=None, transform=None,
                  transform_amount=None, perspective=None, saturation=None,
                  contrast=None, sharpen=None, reset=False):
        with self.lock:
            if mode is not None:
                self.mode = mode if mode in self.MODES else "off"
            if transform is not None:
                self.transform = transform if transform in self.TRANSFORMS else "none"
            if mix is not None:
                self.mix = float(np.clip(float(mix), 0.0, 1.0))
            if decay is not None:
                self.decay = float(np.clip(float(decay), 0.0, 0.98))
            if zoom is not None:
                self.zoom = max(1.0, float(zoom))
            if rotate_degrees is not None:
                self.rotate_degrees = float(rotate_degrees)
            if shift_pixels is not None:
                self.shift_pixels = int(max(0, int(shift_pixels)))
            if transform_amount is not None:
                self.transform_amount = float(np.clip(float(transform_amount), 0.0, 2.0))
            if perspective is not None:
                self.perspective = float(np.clip(float(perspective), 0.0, 1.5))
            if saturation is not None:
                self.saturation = float(np.clip(float(saturation), 0.0, 3.0))
            if contrast is not None:
                self.contrast = float(np.clip(float(contrast), 0.0, 2.5))
            if sharpen is not None:
                self.sharpen = float(np.clip(float(sharpen), 0.0, 2.0))
            if reset:
                self.reset_pending = True

    def get_status(self):
        status = super().get_status()
        status["backend"] = "moderngl"
        status["gl_backend"] = self.gl_backend
        status["gl_renderer"] = self.gl_renderer
        return status

    def process(self, frame_bgr):
        with self.lock:
            mode = self.mode
            transform = self.transform
            mix = self.mix
            decay = self.decay
            zoom = self.zoom
            rotate_degrees = self.rotate_degrees
            shift_pixels = self.shift_pixels
            transform_amount = self.transform_amount
            perspective = self.perspective
            saturation = self.saturation
            contrast = self.contrast
            sharpen = self.sharpen
            reset_feedback = self.reset_pending
            self.reset_pending = False

        if mode == "off" or frame_bgr is None:
            return frame_bgr

        t0 = time.time()
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        frame_rgb = np.ascontiguousarray(frame_rgb)
        self.current_tex.write(frame_rgb.tobytes(), alignment=1)
        if reset_feedback or not self.feedback_ready:
            self.feedback_tex.write(frame_rgb.tobytes(), alignment=1)
            self.feedback_ready = True

        self.current_tex.use(0)
        self.feedback_tex.use(1)
        self.noise_tex.use(2)
        self.program["mode_id"].value = self.MODE_IDS.get(mode, 0)
        self.program["frame_index"].value = int(self.frame_index)
        self.program["mix_amount"].value = float(mix)
        self.program["decay"].value = float(decay)
        self.program["zoom_amount"].value = float(zoom)
        self.program["rotate_degrees"].value = float(rotate_degrees)
        self.program["shift_pixels"].value = float(shift_pixels)
        self.program["transform_id"].value = self.TRANSFORM_IDS.get(transform, 0)
        self.program["transform_amount"].value = float(transform_amount)
        self.program["perspective_amount"].value = float(perspective)
        self.program["saturation_amount"].value = float(saturation)
        self.program["contrast_amount"].value = float(contrast)
        self.program["sharpen_amount"].value = float(sharpen)
        self.framebuffer.use()
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)
        self.vao.render(mode=self.ctx.TRIANGLE_STRIP)

        data = self.output_tex.read(alignment=1)
        out_rgb = np.frombuffer(data, dtype=np.uint8).reshape((self.height, self.width, 3))
        out_rgb = np.ascontiguousarray(out_rgb)
        self.feedback_tex.write(out_rgb.tobytes(), alignment=1)
        self.frame_index += 1
        out_bgr = cv2.cvtColor(out_rgb, cv2.COLOR_RGB2BGR)
        with self.lock:
            self.last_effect_time = time.time() - t0
        return out_bgr


def create_shader_processor(width, height, mode="off", mix=0.65, decay=0.86,
                            zoom=1.012, rotate_degrees=0.35, shift_pixels=2,
                            transform="none", transform_amount=0.45, perspective=0.35,
                            saturation=1.0, contrast=1.0, sharpen=0.0,
                            backend="auto"):
    if backend in ("auto", "moderngl") and mode != "off":
        try:
            shader = ModernGLFeedbackShaderProcessor(
                width=width,
                height=height,
                mode=mode,
                mix=mix,
                decay=decay,
                zoom=zoom,
                rotate_degrees=rotate_degrees,
                shift_pixels=shift_pixels,
                transform=transform,
                transform_amount=transform_amount,
                perspective=perspective,
                saturation=saturation,
                contrast=contrast,
                sharpen=sharpen,
                backend="egl",
            )
            print(f"[sd-iptv] ModernGL GLSL feedback active: {shader.gl_renderer}")
            return shader
        except Exception as exc:
            print(f"[sd-iptv] WARNING: ModernGL GLSL unavailable: {exc}")
            print("[sd-iptv] Falling back to CPU feedback shader-style processor")

    return FeedbackShaderProcessor(
        width=width,
        height=height,
        mode=mode,
        mix=mix,
        decay=decay,
        zoom=zoom,
        rotate_degrees=rotate_degrees,
        shift_pixels=shift_pixels,
        transform=transform,
        transform_amount=transform_amount,
        perspective=perspective,
        saturation=saturation,
        contrast=contrast,
        sharpen=sharpen,
    )


# ---------------------------------------------------------------------------
# Frame stylizer
# ---------------------------------------------------------------------------

class FrameStylizer:
    """Stylizes frames with SD-Turbo img2img in real-time."""

    def __init__(self, pipe, prompts, width=512, height=512,
                 strength=0.5, guidance_scale=1.0, num_inference_steps=1,
                 seed=None, prompt_change_interval=300,
                 device="cuda:0", cache_embeddings=True):
        self.pipe = pipe
        self.prompts = list(prompts)
        self.width = width
        self.height = height
        self.strength = strength
        self.guidance_scale = guidance_scale
        self.num_inference_steps = num_inference_steps
        self.seed = seed
        self.prompt_change_interval = prompt_change_interval
        self.device = device
        self.cache_embeddings = cache_embeddings

        self.prompt_index = 0
        self.frame_count = 0
        self.current_seed = seed if seed is not None else random.randint(0, 2**31)

        self.override_prompt = None
        self.override_seed = None
        self.override_steps = None
        self.override_strength = None

        self.last_gen_time = 0.0
        self.total_frames = 0
        self.lock = threading.Lock()

        # --- Text embedding cache ---
        # prompt string -> (prompt_embeds, negative_prompt_embeds)
        self._embed_cache = {}
        self._cached_prompt_key = None  # tracks which prompt was last encoded

    def _get_cached_embeddings(self, prompt, negative_prompt):
        """Get cached text embeddings, or compute and cache them.

        Since prompts only change every N frames, this avoids running the
        CLIP text encoder on every single frame (~20-30ms savings).
        """
        import torch

        cache_key = prompt + "||" + (negative_prompt or "")
        if cache_key in self._embed_cache:
            return self._embed_cache[cache_key]

        # Compute embeddings using the pipeline's text encoder
        with torch.no_grad():
            prompt_embeds, negative_prompt_embeds = self.pipe.encode_prompt(
                prompt=prompt,
                negative_prompt=negative_prompt,
                device=self.device,
                num_images_per_prompt=1,
                do_classifier_free_guidance=(self.guidance_scale > 1.0),
            )

        self._embed_cache[cache_key] = (prompt_embeds, negative_prompt_embeds)
        return prompt_embeds, negative_prompt_embeds

    def get_current_prompt(self):
        with self.lock:
            if self.override_prompt:
                return self.override_prompt
            return self.prompts[self.prompt_index]

    def _maybe_rotate_prompt(self):
        with self.lock:
            if self.override_prompt:
                return
            if self.frame_count > 0 and self.frame_count % self.prompt_change_interval == 0:
                self.prompt_index = (self.prompt_index + 1) % len(self.prompts)

    def stylize_frame(self, frame_bgr):
        """Stylize a single BGR frame with SD-Turbo img2img.

        Args:
            frame_bgr: numpy array (H, W, 3) BGR uint8

        Returns:
            numpy array (H, W, 3) BGR uint8 — stylized frame, or None on error
        """
        import torch
        from PIL import Image

        # Validate frame
        if frame_bgr is None or frame_bgr.size == 0 or frame_bgr.shape[0] == 0 or frame_bgr.shape[1] == 0:
            return None
        if frame_bgr.std() < 1.0:
            # Frame is essentially blank/uniform — skip stylization
            return frame_bgr

        # BGR -> RGB -> PIL
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        pil_image = Image.fromarray(frame_rgb)

        prompt = self.get_current_prompt()
        strength = self.override_strength if self.override_strength else self.strength
        steps = self.override_steps if self.override_steps else self.num_inference_steps

        gen = torch.Generator(device=self.device)
        gen.manual_seed(self.current_seed)

        t0 = time.time()
        try:
            with torch.no_grad():
                if self.cache_embeddings:
                    # Use cached text embeddings — skips CLIP text encoder
                    prompt_embeds, negative_prompt_embeds = self._get_cached_embeddings(
                        prompt, NEGATIVE_PROMPT
                    )
                    result = self.pipe(
                        image=pil_image,
                        prompt_embeds=prompt_embeds,
                        negative_prompt_embeds=negative_prompt_embeds if self.guidance_scale > 1.0 else None,
                        strength=strength,
                        guidance_scale=self.guidance_scale,
                        num_inference_steps=steps,
                        generator=gen,
                    )
                else:
                    result = self.pipe(
                        image=pil_image,
                        prompt=prompt,
                        strength=strength,
                        guidance_scale=self.guidance_scale,
                        num_inference_steps=steps,
                        generator=gen,
                        negative_prompt=NEGATIVE_PROMPT,
                    )
        except RuntimeError as e:
            print(f"[sd-iptv] pipeline error: {e}")
            return frame_bgr  # Return original frame on error

        gen_time = time.time() - t0

        output_image = result.images[0]
        output_np = np.array(output_image)  # RGB
        output_bgr = cv2.cvtColor(output_np, cv2.COLOR_RGB2BGR)

        with self.lock:
            self.frame_count += 1
            self.total_frames += 1
            self.last_gen_time = gen_time

        self._maybe_rotate_prompt()

        return output_bgr

    def get_status(self):
        with self.lock:
            return {
                "total_frames": self.total_frames,
                "frame_count": self.frame_count,
                "prompt_index": self.prompt_index,
                "num_prompts": len(self.prompts),
                "current_seed": self.current_seed,
                "override_prompt": self.override_prompt,
                "override_seed": self.override_seed,
                "override_steps": self.override_steps,
                "override_strength": self.override_strength,
                "last_gen_time": self.last_gen_time,
                "width": self.width,
                "height": self.height,
                "num_inference_steps": self.override_steps or self.num_inference_steps,
                "guidance_scale": self.guidance_scale,
                "strength": self.override_strength or self.strength,
                "prompt_change_interval": self.prompt_change_interval,
            }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="IPTV → SD-Turbo real-time img2img streamer -> UDP")
    parser.add_argument("--iptv-url", required=True, help="IPTV HLS stream URL (m3u8)")
    parser.add_argument("--udp-url", default="udp://127.0.0.1:5000?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1",
                        help="UDP destination URL")
    parser.add_argument("--fps", type=int, default=20, help="Target FPS")
    parser.add_argument("--width", type=int, default=512, help="Frame width")
    parser.add_argument("--height", type=int, default=512, help="Frame height")
    parser.add_argument("--strength", type=float, default=0.5,
                        help="img2img strength (0.3=light, 0.7=heavy stylization)")
    parser.add_argument("--guidance-scale", type=float, default=1.0,
                        help="CFG guidance scale (SD-Turbo works best at 1.0)")
    parser.add_argument("--steps", type=int, default=2,
                        help="Inference steps (SD-Turbo needs min 2 for img2img in diffusers 0.37)")
    parser.add_argument("--seed", type=int, default=None, help="Fixed seed")
    parser.add_argument("--prompt-change-interval", type=int, default=300,
                        help="Change prompt every N frames (300 = ~15s at 20fps)")
    parser.add_argument("--prompt-file", default=None, help="JSON file with prompts")
    parser.add_argument("--prompt", default=None, help="Single override prompt")
    parser.add_argument("--control-port", type=int, default=8889, help="HTTP control port")
    parser.add_argument("--device", default="cuda:0", help="CUDA device")
    parser.add_argument("--use-tensorrt", action="store_true",
                        help="Use TensorRT UNet engine (4x faster UNet)")
    parser.add_argument("--use-taesd", action="store_true",
                        help="Use TAESD tiny VAE (5x faster VAE encode/decode)")
    parser.add_argument("--no-cache-embeddings", action="store_true",
                        help="Disable text embedding caching (default: caching ON)")
    parser.add_argument("--paced-output", action=argparse.BooleanOptionalAction, default=True,
                        help="Write a steady FPS stream by repeating the latest AI frame between generations")
    parser.add_argument("--glsl-effect", choices=FeedbackShaderProcessor.MODES, default="off",
                        help="Realtime GLSL-style post effect after SD-Turbo")
    parser.add_argument("--glsl-backend", choices=("auto", "moderngl", "cpu"), default="auto",
                        help="Shader backend: auto uses real ModernGL/EGL when available, cpu uses NumPy/OpenCV")
    parser.add_argument("--glsl-mix", type=float, default=0.65,
                        help="Current-frame mix for GLSL-style feedback effect")
    parser.add_argument("--glsl-decay", type=float, default=0.86,
                        help="Feedback-frame decay for GLSL-style effect")
    parser.add_argument("--glsl-zoom", type=float, default=1.012,
                        help="Feedback warp zoom for GLSL-style effect")
    parser.add_argument("--glsl-rotate", type=float, default=0.35,
                        help="Feedback warp rotation amplitude in degrees")
    parser.add_argument("--glsl-shift", type=int, default=2,
                        help="Chromatic channel shift in pixels for GLSL-style effect")
    parser.add_argument("--glsl-transform", choices=FeedbackShaderProcessor.TRANSFORMS, default="none",
                        help="Feedback UV transform pattern")
    parser.add_argument("--glsl-transform-amount", type=float, default=0.45,
                        help="Amount for feedback UV transforms")
    parser.add_argument("--glsl-perspective", type=float, default=0.35,
                        help="Perspective depth for orbit3d/fold3d feedback transforms")
    parser.add_argument("--glsl-saturation", type=float, default=1.0,
                        help="Post-effect saturation multiplier")
    parser.add_argument("--glsl-contrast", type=float, default=1.0,
                        help="Post-effect contrast multiplier")
    parser.add_argument("--glsl-sharpen", type=float, default=0.0,
                        help="Post-effect unsharp mask amount")
    args = parser.parse_args()

    # Load prompts
    if args.prompt_file:
        with open(args.prompt_file) as pf:
            prompts = json.load(pf)
    elif args.prompt:
        prompts = [args.prompt]
    else:
        prompts = PROMPTS

    print(f"[sd-iptv] IPTV → SD-Turbo real-time img2img streamer")
    print(f"[sd-iptv] IPTV source: {args.iptv_url}")
    print(f"[sd-iptv] Resolution: {args.width}x{args.height}")
    print(f"[sd-iptv] Target: {args.fps}fps, Strength: {args.strength}, Steps: {args.steps}")
    print(f"[sd-iptv] Prompts: {len(prompts)} loaded, change every {args.prompt_change_interval} frames")
    print(f"[sd-iptv] Guidance: {args.guidance_scale}")
    print(f"[sd-iptv] Optimizations: TensorRT={'ON' if args.use_tensorrt else 'OFF'} "
          f"TAESD={'ON' if args.use_taesd else 'OFF'} "
          f"EmbedCache={'ON' if not args.no_cache_embeddings else 'OFF'}")
    print(f"[sd-iptv] Output pacing: {'ON' if args.paced_output else 'OFF'}")
    print(f"[sd-iptv] GLSL-style effect: {args.glsl_effect} "
                    f"backend={args.glsl_backend} "
          f"mix={args.glsl_mix:.2f} decay={args.glsl_decay:.2f} "
                    f"zoom={args.glsl_zoom:.3f} rotate={args.glsl_rotate:.2f} shift={args.glsl_shift} "
                    f"transform={args.glsl_transform} amount={args.glsl_transform_amount:.2f} "
                    f"perspective={args.glsl_perspective:.2f} "
                    f"sat={args.glsl_saturation:.2f} contrast={args.glsl_contrast:.2f} sharpen={args.glsl_sharpen:.2f}")

    # Load SD-Turbo pipeline
    pipe = load_sdturbo_pipeline(
        device=args.device,
        use_tensorrt=args.use_tensorrt,
        use_taesd=args.use_taesd,
    )

    # Create IPTV frame reader
    reader = IPTVFrameReader(
        url=args.iptv_url,
        width=args.width,
        height=args.height,
        fps=args.fps,
    )

    # Create frame stylizer
    stylizer = FrameStylizer(
        pipe=pipe,
        prompts=prompts,
        width=args.width,
        height=args.height,
        strength=args.strength,
        guidance_scale=args.guidance_scale,
        num_inference_steps=args.steps,
        seed=args.seed,
        prompt_change_interval=args.prompt_change_interval,
        device=args.device,
        cache_embeddings=not args.no_cache_embeddings,
    )

    shader = create_shader_processor(
        width=args.width,
        height=args.height,
        mode=args.glsl_effect,
        mix=args.glsl_mix,
        decay=args.glsl_decay,
        zoom=args.glsl_zoom,
        rotate_degrees=args.glsl_rotate,
        shift_pixels=args.glsl_shift,
        transform=args.glsl_transform,
        transform_amount=args.glsl_transform_amount,
        perspective=args.glsl_perspective,
        saturation=args.glsl_saturation,
        contrast=args.glsl_contrast,
        sharpen=args.glsl_sharpen,
        backend=args.glsl_backend,
    )
    shader_enabled = args.glsl_effect != "off"

    # Start UDP writer
    writer = start_udp_writer(args.width, args.height, args.fps, args.udp_url)

    # --- Remote control HTTP server ---
    remote = {"stylizer": stylizer, "prompts": prompts, "reader": reader, "shader": shader}
    running = [True]

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
                st = remote["stylizer"]
                if parsed.path == "/status":
                    status = st.get_status()
                    status["iptv_url"] = remote["reader"].url
                    status["iptv_frames_read"] = remote["reader"].total_frames
                    status["iptv_reconnects"] = remote["reader"].reconnect_count
                    status["glsl_effect"] = remote["shader"].get_status()
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

                st = remote["stylizer"]

                if parsed.path == "/prompt":
                    p = data.get("prompt", "")
                    if p:
                        with st.lock:
                            st.override_prompt = p
                        self._send_json(200, {"ok": True, "override_prompt": p})
                    else:
                        self._send_json(400, {"error": "missing 'prompt'"})
                elif parsed.path == "/prompts":
                    plist = data.get("prompts", [])
                    if plist and isinstance(plist, list):
                        with st.lock:
                            st.prompts = plist
                            st.prompt_index = 0
                            st.override_prompt = None
                        remote["prompts"] = plist
                        self._send_json(200, {"ok": True, "num_prompts": len(plist)})
                    else:
                        self._send_json(400, {"error": "missing 'prompts' list"})
                elif parsed.path == "/seed":
                    s = data.get("seed")
                    if s is not None:
                        with st.lock:
                            st.current_seed = int(s)
                            st.override_seed = int(s)
                        self._send_json(200, {"ok": True, "seed": int(s)})
                    else:
                        self._send_json(400, {"error": "missing 'seed'"})
                elif parsed.path == "/steps":
                    sp = data.get("steps")
                    if sp is not None:
                        with st.lock:
                            st.override_steps = int(sp)
                        self._send_json(200, {"ok": True, "override_steps": int(sp)})
                    else:
                        self._send_json(400, {"error": "missing 'steps'"})
                elif parsed.path == "/strength":
                    sp = data.get("strength")
                    if sp is not None:
                        with st.lock:
                            st.override_strength = float(sp)
                        self._send_json(200, {"ok": True, "override_strength": float(sp)})
                    else:
                        self._send_json(400, {"error": "missing 'strength'"})
                elif parsed.path == "/rotate":
                    with st.lock:
                        st.override_prompt = None
                        st.override_seed = None
                        st.override_steps = None
                        st.override_strength = None
                    self._send_json(200, {"ok": True, "message": "rotation resumed"})
                elif parsed.path == "/next":
                    with st.lock:
                        st.prompt_index = (st.prompt_index + 1) % len(st.prompts)
                        st.override_prompt = None
                        np = st.prompts[st.prompt_index]
                    self._send_json(200, {"ok": True, "prompt_index": st.prompt_index, "prompt": np[:100]})
                elif parsed.path == "/interval":
                    pi = data.get("prompt_change_interval")
                    if pi is not None:
                        with st.lock:
                            st.prompt_change_interval = int(pi)
                        self._send_json(200, {"ok": True, "prompt_change_interval": st.prompt_change_interval})
                elif parsed.path == "/channel":
                    url = data.get("url", "")
                    if url:
                        remote["reader"].url = url
                        remote["reader"]._start_ffmpeg()
                        self._send_json(200, {"ok": True, "iptv_url": url})
                    else:
                        self._send_json(400, {"error": "missing 'url'"})
                elif parsed.path == "/effect":
                    shader = remote["shader"]
                    shader.configure(
                        mode=data.get("mode"),
                        mix=data.get("mix"),
                        decay=data.get("decay"),
                        zoom=data.get("zoom"),
                        rotate_degrees=data.get("rotate_degrees", data.get("rotate")),
                        shift_pixels=data.get("shift_pixels", data.get("shift")),
                        transform=data.get("transform"),
                        transform_amount=data.get("transform_amount", data.get("amount")),
                        perspective=data.get("perspective"),
                        saturation=data.get("saturation"),
                        contrast=data.get("contrast"),
                        sharpen=data.get("sharpen"),
                        reset=bool(data.get("reset", False)),
                    )
                    self._send_json(200, {"ok": True, "glsl_effect": shader.get_status()})
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
            print(f"[sd-iptv] remote control server on http://0.0.0.0:{port}")
            print(f"[sd-iptv]   GET  /status    — current state")
            print(f"[sd-iptv]   GET  /prompts   — list all prompts")
            print(f"[sd-iptv]   POST /prompt    — set override prompt")
            print(f"[sd-iptv]   POST /prompts   — replace prompt list")
            print(f"[sd-iptv]   POST /seed      — set fixed seed")
            print(f"[sd-iptv]   POST /steps     — set steps")
            print(f"[sd-iptv]   POST /strength  — set img2img strength (0.1-0.9)")
            print(f"[sd-iptv]   POST /effect    — set GLSL-style feedback effect")
            print(f"[sd-iptv]   POST /next      — jump to next prompt")
            print(f"[sd-iptv]   POST /rotate    — resume auto rotation")
            print(f"[sd-iptv]   POST /interval  — change prompt interval")
            print(f"[sd-iptv]   POST /channel   — switch IPTV source URL")
            while running[0]:
                server.handle_request()
            server.server_close()

        ctrl_thread = threading.Thread(target=start_control_server, args=(args.control_port,), daemon=True)
        ctrl_thread.start()

    # --- Signal handlers ---
    def handle_signal(signum, frame):
        running[0] = False
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # --- Main loop: read frame → stylize → write ---
    print(f"[sd-iptv] starting real-time stream at {args.fps}fps...")

    writer_state = {"writer": writer, "total_written": 0}
    writer_lock = threading.Lock()
    latest_frame_lock = threading.Lock()
    latest_output = {
        "frame": np.zeros((args.height, args.width, 3), dtype=np.uint8),
        "version": 0,
        "updated_at": 0.0,
    }
    cv2.putText(
        latest_output["frame"],
        "SD-TURBO STARTING",
        (10, max(24, args.height // 2)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (220, 220, 220),
        2,
        cv2.LINE_AA,
    )

    frames_since_stats = 0
    output_since_stats = [0]
    repeats_since_stats = [0]
    last_stats_time = time.time()
    frame_interval = 1.0 / args.fps

    def write_udp_frame(frame_bgr):
        try:
            with writer_lock:
                writer_state["writer"].stdin.write(frame_bgr.tobytes())
                writer_state["writer"].stdin.flush()
            writer_state["total_written"] += 1
            return True
        except (BrokenPipeError, IOError):
            print("[sd-iptv] UDP writer pipe broke, restarting...")
            try:
                writer_state["writer"].terminate()
                writer_state["writer"].wait(timeout=2)
            except Exception:
                pass
            writer_state["writer"] = start_udp_writer(args.width, args.height, args.fps, args.udp_url)
            try:
                with writer_lock:
                    writer_state["writer"].stdin.write(frame_bgr.tobytes())
                    writer_state["writer"].stdin.flush()
                writer_state["total_written"] += 1
                return True
            except Exception:
                print("[sd-iptv] failed to restart UDP writer")
                return False

    def paced_writer_thread():
        next_frame_time = time.monotonic()
        last_version = -1
        while running[0]:
            with latest_frame_lock:
                frame_bgr = latest_output["frame"].copy()
                version = latest_output["version"]

            if version == last_version:
                repeats_since_stats[0] += 1
            else:
                last_version = version

            if write_udp_frame(frame_bgr):
                output_since_stats[0] += 1

            next_frame_time += frame_interval
            sleep_time = next_frame_time - time.monotonic()
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                next_frame_time = time.monotonic()

    if args.paced_output:
        threading.Thread(target=paced_writer_thread, daemon=True).start()

    try:
        while running[0]:
            # Read a frame from IPTV
            frame = reader.read_frame(timeout=5.0)
            if frame is None:
                continue

            # Stylize with SD-Turbo
            stylized = stylizer.stylize_frame(frame)
            if stylized is None:
                continue

            output_frame = shader.process(stylized) if shader_enabled else stylized

            if args.paced_output:
                with latest_frame_lock:
                    latest_output["frame"] = output_frame.copy()
                    latest_output["version"] += 1
                    latest_output["updated_at"] = time.time()
            elif write_udp_frame(output_frame):
                output_since_stats[0] += 1

            frames_since_stats += 1

            # Stats every 10 seconds
            now = time.time()
            if now - last_stats_time >= 10.0:
                elapsed = now - last_stats_time
                actual_fps = frames_since_stats / elapsed
                output_fps = output_since_stats[0] / elapsed
                status = stylizer.get_status()
                effect_status = shader.get_status()
                print(f"[sd-iptv] written={writer_state['total_written']} ai_fps={actual_fps:.1f} out_fps={output_fps:.1f} "
                      f"gen={status['last_gen_time']*1000:.0f}ms "
                        f"fx={effect_status['mode']}/{effect_status['transform']}:{effect_status['last_effect_time']*1000:.1f}ms "
                      f"repeats={repeats_since_stats[0]} "
                      f"prompt=#{status['prompt_index']} "
                      f"iptv_frames={reader.total_frames} "
                      f"reconnects={reader.reconnect_count}")
                frames_since_stats = 0
                output_since_stats[0] = 0
                repeats_since_stats[0] = 0
                last_stats_time = now

    except KeyboardInterrupt:
        pass
    finally:
        print("[sd-iptv] shutting down...")
        running[0] = False

        reader.close()
        try:
            writer_state["writer"].stdin.close()
            writer_state["writer"].terminate()
            writer_state["writer"].wait(timeout=3)
        except Exception:
            pass

        print("[sd-iptv] done.")


if __name__ == "__main__":
    main()
