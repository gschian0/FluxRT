#version 330

/*
  Camera motion shader — adds continuous cinematic camera movement
  to make static Flux frames feel like real video.

  Effects (all time-driven, slow and smooth):
    - Slow pan (translate UVs over time)
    - Slow zoom in/out (breathing)
    - Subtle rotation
    - Parallax layers (depth-based offset)
    - Vignette
    - Film grain

  Uniforms:
    u_tex           - input texture
    u_time          - elapsed time (seconds)
    u_resolution    - (width, height)
    u_warp_amount   - motion intensity (0.0=none, 1.0=normal, 2.0=extreme)
    u_chroma_amount - chromatic aberration at edges
    u_displace_amount - parallax depth strength
*/

in vec2 v_uv;
out vec4 fragColor;

uniform sampler2D u_tex;
uniform float u_time;
uniform vec2 u_resolution;
uniform float u_warp_amount;
uniform float u_chroma_amount;
uniform float u_displace_amount;

// --- Noise utilities ---
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

// Edge falloff — reduce effect strength near borders
float edge_falloff(vec2 uv, float strength) {
    float d = min(min(uv.x, uv.y), min(1.0 - uv.x, 1.0 - uv.y));
    return smoothstep(0.0, strength, d);
}

void main() {
    vec2 uv = v_uv;
    float t = u_time;
    float aspect = u_resolution.x / u_resolution.y;

    // === Camera pan: slow drift using sine waves ===
    float pan_speed = 0.02 * u_warp_amount;
    vec2 pan = vec2(
        sin(t * 0.13) * pan_speed,
        cos(t * 0.17) * pan_speed
    );

    // === Camera zoom: slow breathing in and out ===
    float zoom = 1.0 + sin(t * 0.08) * 0.04 * u_warp_amount;
    vec2 centered = uv - 0.5;
    vec2 zoomed = centered / zoom + 0.5;

    // === Camera rotation: very subtle slow roll ===
    float angle = sin(t * 0.05) * 0.015 * u_warp_amount;
    float ca = cos(angle);
    float sa = sin(angle);
    vec2 rotated = vec2(
        dot(zoomed - 0.5, vec2(ca, -sa)),
        dot(zoomed - 0.5, vec2(sa, ca))
    ) + 0.5;

    // Apply pan
    vec2 motion_uv = rotated + pan;

    // === Parallax: sample texture at slightly different offsets per "layer" ===
    // Use noise to create a depth map, then offset UVs based on depth
    float depth = fbm(uv * 3.0 + t * 0.02);
    vec2 parallax = vec2(
        sin(t * 0.1) * depth * 0.015 * u_displace_amount,
        cos(t * 0.12) * depth * 0.015 * u_displace_amount
    );
    motion_uv += parallax;

    // Clamp to valid range with edge falloff
    float falloff = edge_falloff(uv, 0.05);

    // === Chromatic aberration at edges ===
    float ca_strength = u_chroma_amount * 0.003 * (1.0 - falloff) * 2.0;
    float r = texture(u_tex, motion_uv + vec2(ca_strength, 0.0)).r;
    float g = texture(u_tex, motion_uv).g;
    float b = texture(u_tex, motion_uv - vec2(ca_strength, 0.0)).b;
    vec3 col = vec3(r, g, b);

    // === Vignette: darken edges slightly ===
    float vig = 1.0 - (1.0 - falloff) * 0.3;
    col *= vig;

    // === Film grain: subtle noise overlay ===
    float grain = (hash(uv * u_resolution + t * 60.0) - 0.5) * 0.03;
    col += grain;

    // === Subtle color shift over time (like changing lighting) ===
    float color_shift = sin(t * 0.03) * 0.02;
    col.r += color_shift;
    col.b -= color_shift;

    fragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
