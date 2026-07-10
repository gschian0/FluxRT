#!/usr/bin/env python3
"""
GLSL shader post-processor for Flux video output.

Three-pass pipeline:
  Pass 1 — Temporal blend: crossfades current Flux frame with previous output,
           creating smooth motion between diffusion generations.
  Pass 2 — Camera motion: adds continuous cinematic camera movement (pan, zoom,
           rotation, parallax) to make static frames feel like real video.
  Pass 3 — Effect shader: applies the active Shadertoy-style displacement/warp.

Features:
  - Hot-reload: watches shader files for changes, recompiles live
  - Multi-shader rotation: cycles through all .glsl files in a directory
  - Remote control: HTTP endpoint to switch shaders, adjust params
  - Temporal blending with configurable blend strength
  - Camera motion always applied (makes it look like video, not a filter)

Usage:
    from glsl_shader_processor import ShaderProcessor
    sp = ShaderProcessor(width=1280, height=720, shader_dir="shaders")
    sp.start()
    processed = sp.process(frame_rgb)  # numpy array (H, W, 3) uint8
"""

import os
import time
import glob
import numpy as np


# Must be set BEFORE importing moderngl
os.environ.setdefault("__EGL_VENDOR_LIBRARY_FILENAMES",
                      "/etc/glvnd/egl_vendor.d/10_nvidia.json")


def _create_egl_context():
    """Create a ModernGL context using EGL (headless NVIDIA)."""
    import glcontext
    glcontext.default_backend = lambda: glcontext.get_backend_by_name('egl')
    import moderngl
    ctx = moderngl.create_standalone_context()
    return ctx


# Fullscreen triangle vertex shader (shared by all passes)
VERTEX_SHADER = """
#version 330

in vec2 in_vert;
in vec2 in_uv;
out vec2 v_uv;

void main() {
    v_uv = in_uv;
    gl_Position = vec4(in_vert, 0.0, 1.0);
}
"""

# Pass 1: Temporal blend — crossfade current frame with previous output
BLEND_SHADER = """
#version 330

in vec2 v_uv;
out vec4 fragColor;

uniform sampler2D u_current;    // new Flux frame
uniform sampler2D u_previous;   // previous output frame
uniform float u_blend;          // 0.0 = show previous, 1.0 = show current
uniform float u_motion_blur;    // 0.0 = none, 1.0 = heavy trails
uniform vec2 u_resolution;

float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
}

void main() {
    vec3 cur = texture(u_current, v_uv).rgb;
    vec3 prev = texture(u_previous, v_uv).rgb;

    // Temporal crossfade
    float b = mix(0.2, 1.0, u_blend);

    // Motion blur: retain some of the previous frame for trail effect
    float mb = u_motion_blur * 0.5;
    vec3 col = mix(cur, prev, mb);
    col = mix(col, cur, b);

    // Dithering to prevent banding
    float dither = (hash(v_uv * u_resolution + u_blend) - 0.5) * 2.0 / 255.0;
    col += dither;

    fragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
"""

# Pass 2: Camera motion — always applied, adds cinematic movement
CAMERA_MOTION_SHADER = """
#version 330

in vec2 v_uv;
out vec4 fragColor;

uniform sampler2D u_tex;
uniform float u_time;
uniform vec2 u_resolution;
uniform float u_motion_intensity;  // 0.0 = static, 1.0 = normal, 2.0 = extreme

float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
}

float noise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    float a = hash(i);
    float b = hash(i + vec2(1.0, 0.0));
    float c = hash(i + vec2(0.0, 1.0));
    float d = hash(i + vec2(1.0, 1.0));
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}

float fbm(vec2 p) {
    float v = 0.0;
    float a = 0.5;
    for (int i = 0; i < 4; i++) {
        v += a * noise(p);
        p *= 2.0;
        a *= 0.5;
    }
    return v;
}

float edge_falloff(vec2 uv, float strength) {
    float d = min(min(uv.x, uv.y), min(1.0 - uv.x, 1.0 - uv.y));
    return smoothstep(0.0, strength, d);
}

void main() {
    vec2 uv = v_uv;
    float t = u_time;
    float mi = u_motion_intensity;

    // === Slow pan: drift using sine waves at different frequencies ===
    float pan_speed = 0.03 * mi;
    vec2 pan = vec2(
        sin(t * 0.13) * pan_speed,
        cos(t * 0.17) * pan_speed
    );

    // === Slow zoom: breathing in and out ===
    float zoom = 1.0 + sin(t * 0.08) * 0.05 * mi;
    vec2 centered = uv - 0.5;
    vec2 zoomed = centered / zoom + 0.5;

    // === Subtle rotation ===
    float angle = sin(t * 0.05) * 0.02 * mi;
    float ca = cos(angle);
    float sa = sin(angle);
    vec2 rotated = vec2(
        dot(zoomed - 0.5, vec2(ca, -sa)),
        dot(zoomed - 0.5, vec2(sa, ca))
    ) + 0.5;

    // Apply pan
    vec2 motion_uv = rotated + pan;

    // === Parallax: depth-based offset using noise ===
    float depth = fbm(uv * 3.0 + t * 0.02);
    vec2 parallax = vec2(
        sin(t * 0.1) * depth * 0.02 * mi,
        cos(t * 0.12) * depth * 0.02 * mi
    );
    motion_uv += parallax;

    // Sample with motion
    vec3 col = texture(u_tex, motion_uv).rgb;

    // === Vignette ===
    float falloff = edge_falloff(uv, 0.08);
    float vig = 1.0 - (1.0 - falloff) * 0.25;
    col *= vig;

    // === Film grain ===
    float grain = (hash(uv * u_resolution + t * 60.0) - 0.5) * 0.025;
    col += grain;

    // === Subtle color shift (changing lighting) ===
    float color_shift = sin(t * 0.03) * 0.015;
    col.r += color_shift;
    col.b -= color_shift;

    fragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
"""


class ShaderProcessor:
    """GLSL shader post-processor with temporal blending, camera motion, and effects."""

    def __init__(self, width: int = 1280, height: int = 720,
                 shader_dir: str = None,
                 shader_file: str = None,
                 warp_amount: float = 0.5,
                 chroma_amount: float = 0.5,
                 displace_amount: float = 0.5,
                 shader_change_interval: int = 600,
                 hot_reload: bool = True,
                 temporal_blend: float = 0.6,
                 motion_blur: float = 0.3,
                 motion_intensity: float = 0.7):
        """
        Args:
            shader_dir: Directory of .glsl files to rotate through.
            shader_file: Single shader file (overrides shader_dir if set).
            warp_amount: Warp effect strength (0.0-2.0).
            chroma_amount: Chromatic aberration strength (0.0-2.0).
            displace_amount: Displacement strength (0.0-2.0).
            shader_change_interval: Seconds between shader rotation (0=never).
            hot_reload: If True, recompile shader when file changes on disk.
            temporal_blend: How quickly new frames replace old (0.0=sticky, 1.0=instant).
            motion_blur: Trail/motion-blur amount (0.0=none, 1.0=heavy).
            motion_intensity: Camera motion strength (0.0=static, 1.0=normal, 2.0=extreme).
        """
        self.width = width
        self.height = height
        self.warp_amount = warp_amount
        self.chroma_amount = chroma_amount
        self.displace_amount = displace_amount
        self.shader_change_interval = shader_change_interval
        self.hot_reload = hot_reload
        self.temporal_blend = temporal_blend
        self.motion_blur = motion_blur
        self.motion_intensity = motion_intensity

        # Resolve shader directory
        if shader_file:
            self.shader_dir = None
            self.shader_files = [shader_file]
        else:
            self.shader_dir = shader_dir or os.path.join(
                os.path.dirname(__file__), "shaders")
            self.shader_files = sorted(glob.glob(
                os.path.join(self.shader_dir, "*.glsl")))

        self.current_shader_index = 0
        self.current_shader_path = None
        self.current_shader_name = "none"
        self.current_shader_mtime = 0
        self.shader_start_time = 0

        self.ctx = None
        self.prog = None          # effect shader (pass 3)
        self.blend_prog = None    # temporal blend (pass 1)
        self.camera_prog = None   # camera motion (pass 2)
        self.tex = None           # input: current Flux frame
        self.prev_tex = None      # previous blended frame (for temporal)
        self.blend_tex = None     # pass 1 output: blended
        self.camera_tex = None    # pass 2 output: camera-motioned
        self.out_tex = None       # pass 3 output: final
        self.fbo_blend = None
        self.fbo_camera = None
        self.fbo = None
        self.vao = None
        self.vao_blend = None
        self.vao_camera = None
        self.start_time = None
        self._initialized = False

        # Stats
        self._frame_count = 0
        self._last_stats_time = 0

    def _load_shader_source(self, path: str) -> str:
        with open(path, 'r') as f:
            return f.read()

    def _compile_shader(self, source: str):
        try:
            prog = self.ctx.program(
                vertex_shader=VERTEX_SHADER,
                fragment_shader=source,
            )
            return prog
        except Exception as e:
            print(f"[shader] COMPILE ERROR: {e}")
            return None

    def _make_vao(self, prog):
        """Create a fullscreen triangle VAO for a given program."""
        vertices = np.array([
            -1.0, -1.0,  0.0,  0.0,
             3.0, -1.0,  2.0,  0.0,
            -1.0,  3.0,  0.0,  2.0,
        ], dtype=np.float32)
        vbo = self.ctx.buffer(vertices.tobytes())
        return self.ctx.vertex_array(prog, vbo, 'in_vert', 'in_uv')

    def _switch_shader(self, index: int):
        if not self.shader_files:
            return

        index = index % len(self.shader_files)
        path = self.shader_files[index]
        source = self._load_shader_source(path)

        new_prog = self._compile_shader(source)
        if new_prog is None:
            print(f"[shader] failed to compile {os.path.basename(path)}, keeping previous")
            return

        if self.prog is not None:
            self.prog.release()
        if self.vao is not None:
            self.vao.release()

        self.prog = new_prog
        self.current_shader_index = index
        self.current_shader_path = path
        self.current_shader_name = os.path.basename(path).replace('.glsl', '')
        self.current_shader_mtime = os.path.getmtime(path)
        self.shader_start_time = time.time()
        self.vao = self._make_vao(self.prog)

        print(f"[shader] switched to: {self.current_shader_name} "
              f"(#{index+1}/{len(self.shader_files)})")

    def _check_hot_reload(self):
        if not self.hot_reload or self.current_shader_path is None:
            return
        try:
            mtime = os.path.getmtime(self.current_shader_path)
        except OSError:
            return
        if mtime != self.current_shader_mtime:
            print(f"[shader] hot-reload detected: {self.current_shader_name}")
            source = self._load_shader_source(self.current_shader_path)
            new_prog = self._compile_shader(source)
            if new_prog is not None:
                if self.prog is not None:
                    self.prog.release()
                if self.vao is not None:
                    self.vao.release()
                self.prog = new_prog
                self.current_shader_mtime = mtime
                self.vao = self._make_vao(self.prog)
                print(f"[shader] hot-reload OK: {self.current_shader_name}")
            else:
                print(f"[shader] hot-reload FAILED, keeping old version")

    def _check_rotation(self):
        if self.shader_change_interval <= 0 or len(self.shader_files) <= 1:
            return
        elapsed = time.time() - self.shader_start_time
        if elapsed >= self.shader_change_interval:
            self._switch_shader(self.current_shader_index + 1)

    def start(self):
        """Initialize the OpenGL context, textures, and shaders."""
        self.ctx = _create_egl_context()
        ctx = self.ctx

        # --- Textures ---
        self.tex = ctx.texture((self.width, self.height), 3)
        self.tex.filter = (ctx.LINEAR, ctx.LINEAR)

        self.prev_tex = ctx.texture((self.width, self.height), 3)
        self.prev_tex.filter = (ctx.LINEAR, ctx.LINEAR)

        self.blend_tex = ctx.texture((self.width, self.height), 3)
        self.blend_tex.filter = (ctx.LINEAR, ctx.LINEAR)

        self.camera_tex = ctx.texture((self.width, self.height), 3)
        self.camera_tex.filter = (ctx.LINEAR, ctx.LINEAR)

        self.out_tex = ctx.texture((self.width, self.height), 3)
        self.out_tex.filter = (ctx.LINEAR, ctx.LINEAR)

        # --- Framebuffers ---
        self.fbo_blend = ctx.framebuffer(color_attachments=[self.blend_tex])
        self.fbo_camera = ctx.framebuffer(color_attachments=[self.camera_tex])
        self.fbo = ctx.framebuffer(color_attachments=[self.out_tex])

        # --- Pass 1: Temporal blend ---
        self.blend_prog = self._compile_shader(BLEND_SHADER)
        self.vao_blend = self._make_vao(self.blend_prog)

        # --- Pass 2: Camera motion (always on) ---
        self.camera_prog = self._compile_shader(CAMERA_MOTION_SHADER)
        self.vao_camera = self._make_vao(self.camera_prog)

        # --- Pass 3: Effect shader ---
        if self.shader_files:
            self._switch_shader(0)
        else:
            print("[shader] WARNING: no shader files found, passthrough mode")
            return

        self.start_time = time.time()
        self._last_stats_time = time.time()
        self._initialized = True
        print(f"[shader] initialized: {self.width}x{self.height}, "
              f"{len(self.shader_files)} shaders, "
              f"blend={self.temporal_blend} blur={self.motion_blur} "
              f"motion={self.motion_intensity}, "
              f"rotate every {self.shader_change_interval}s, "
              f"hot_reload={self.hot_reload}")

    def process(self, frame: np.ndarray) -> np.ndarray:
        """Apply temporal blend + camera motion + effect shader to a frame."""
        if not self._initialized or self.prog is None:
            return frame

        h, w = frame.shape[:2]
        assert w == self.width and h == self.height, \
            f"Frame {w}x{h} doesn't match shader {self.width}x{self.height}"

        self._check_hot_reload()
        self._check_rotation()

        # Upload current Flux frame to input texture
        self.tex.write(frame.tobytes())

        elapsed = time.time() - self.start_time

        # === PASS 1: Temporal blend ===
        self.tex.use(0)         # u_current
        self.prev_tex.use(1)    # u_previous

        if 'u_current' in self.blend_prog:
            self.blend_prog['u_current'].value = 0
        if 'u_previous' in self.blend_prog:
            self.blend_prog['u_previous'].value = 1
        if 'u_blend' in self.blend_prog:
            self.blend_prog['u_blend'].value = self.temporal_blend
        if 'u_motion_blur' in self.blend_prog:
            self.blend_prog['u_motion_blur'].value = self.motion_blur
        if 'u_resolution' in self.blend_prog:
            self.blend_prog['u_resolution'].value = (float(self.width), float(self.height))

        self.fbo_blend.use()
        self.ctx.viewport = (0, 0, self.width, self.height)
        self.vao_blend.render(mode=self.ctx.TRIANGLES)

        # === PASS 2: Camera motion ===
        self.blend_tex.use(0)
        if 'u_tex' in self.camera_prog:
            self.camera_prog['u_tex'].value = 0
        if 'u_time' in self.camera_prog:
            self.camera_prog['u_time'].value = elapsed
        if 'u_resolution' in self.camera_prog:
            self.camera_prog['u_resolution'].value = (float(self.width), float(self.height))
        if 'u_motion_intensity' in self.camera_prog:
            self.camera_prog['u_motion_intensity'].value = self.motion_intensity

        self.fbo_camera.use()
        self.ctx.viewport = (0, 0, self.width, self.height)
        self.vao_camera.render(mode=self.ctx.TRIANGLES)

        # === PASS 3: Effect shader ===
        self.camera_tex.use(0)
        if 'u_tex' in self.prog:
            self.prog['u_tex'].value = 0

        self.fbo.use()
        self.ctx.viewport = (0, 0, self.width, self.height)

        uniforms = {
            "u_time": elapsed,
            "u_resolution": (float(self.width), float(self.height)),
            "u_warp_amount": self.warp_amount,
            "u_chroma_amount": self.chroma_amount,
            "u_displace_amount": self.displace_amount,
        }
        for name, value in uniforms.items():
            if name in self.prog:
                self.prog[name].value = value

        self.vao.render(mode=self.ctx.TRIANGLES)

        # Read back the result
        raw = self.fbo.read(components=3, alignment=1)
        result = np.frombuffer(raw, dtype=np.uint8).reshape(self.height, self.width, 3)

        # Update prev_tex with the blended frame for next frame's temporal blend
        self.prev_tex.write(self.blend_tex.read(alignment=1))

        # Stats every 10 seconds
        self._frame_count += 1
        now = time.time()
        if now - self._last_stats_time >= 10.0:
            fps = self._frame_count / (now - self._last_stats_time)
            print(f"[shader] {self.current_shader_name} fps={fps:.1f} "
                  f"warp={self.warp_amount:.2f} chroma={self.chroma_amount:.2f} "
                  f"displace={self.displace_amount:.2f} "
                  f"blend={self.temporal_blend:.2f} blur={self.motion_blur:.2f} "
                  f"motion={self.motion_intensity:.2f}")
            self._frame_count = 0
            self._last_stats_time = now

        return np.ascontiguousarray(result)

    def next_shader(self):
        self._switch_shader(self.current_shader_index + 1)

    def prev_shader(self):
        self._switch_shader(self.current_shader_index - 1)

    def set_shader(self, name: str):
        for i, path in enumerate(self.shader_files):
            if os.path.basename(path).replace('.glsl', '') == name:
                self._switch_shader(i)
                return True
        return False

    def set_warp(self, amount: float):
        self.warp_amount = max(0.0, min(2.0, amount))

    def set_chroma(self, amount: float):
        self.chroma_amount = max(0.0, min(2.0, amount))

    def set_displace(self, amount: float):
        self.displace_amount = max(0.0, min(2.0, amount))

    def set_temporal_blend(self, amount: float):
        self.temporal_blend = max(0.0, min(1.0, amount))

    def set_motion_blur(self, amount: float):
        self.motion_blur = max(0.0, min(1.0, amount))

    def set_motion_intensity(self, amount: float):
        self.motion_intensity = max(0.0, min(2.0, amount))

    def get_status(self) -> dict:
        return {
            "current_shader": self.current_shader_name,
            "shader_index": self.current_shader_index,
            "shader_count": len(self.shader_files),
            "shader_files": [os.path.basename(f).replace('.glsl', '')
                             for f in self.shader_files],
            "warp_amount": self.warp_amount,
            "chroma_amount": self.chroma_amount,
            "displace_amount": self.displace_amount,
            "temporal_blend": self.temporal_blend,
            "motion_blur": self.motion_blur,
            "motion_intensity": self.motion_intensity,
            "shader_change_interval": self.shader_change_interval,
            "hot_reload": self.hot_reload,
        }

    def stop(self):
        for vao in [self.vao, self.vao_blend, self.vao_camera]:
            if vao is not None:
                vao.release()
        for prog in [self.prog, self.blend_prog, self.camera_prog]:
            if prog is not None:
                prog.release()
        if self.ctx:
            self.ctx.release()
            self.ctx = None
        self._initialized = False
        self.tex = None
        self.prev_tex = None
        self.blend_tex = None
        self.camera_tex = None
        self.out_tex = None
        self.fbo = None
        self.fbo_blend = None
        self.fbo_camera = None


if __name__ == "__main__":
    sp = ShaderProcessor(1280, 720, shader_dir=os.path.join(
        os.path.dirname(__file__), "shaders"))
    sp.start()

    # Test with a changing pattern to verify all 3 passes
    t0 = time.time()
    for i in range(100):
        test = np.zeros((720, 1280, 3), dtype=np.uint8)
        test[:, :, 0] = np.linspace(0, 255, 1280, dtype=np.uint8)
        test[:, :, 1] = np.linspace(0, 255, 720, dtype=np.uint8)[:, None]
        test[:, :, 2] = (i * 2) % 256
        result = sp.process(test)
    elapsed = time.time() - t0
    print(f"[test] 100 frames in {elapsed:.3f}s = {100/elapsed:.1f} fps")
    print(f"[test] output shape: {result.shape}, dtype: {result.dtype}")
    print(f"[test] output range: {result.min()}-{result.max()}")
    print(f"[test] status: {sp.get_status()}")
    sp.stop()
