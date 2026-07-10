#version 330
// SHADER: liquid_warp
// Liquid domain warp — flowing organic distortion with edge falloff
in vec2 v_uv;
out vec4 fragColor;
uniform sampler2D u_tex;
uniform float u_time;
uniform vec2 u_resolution;
uniform float u_warp_amount;
uniform float u_chroma_amount;
uniform float u_displace_amount;

float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
float noise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i+vec2(1,0)), f.x), mix(hash(i+vec2(0,1)), hash(i+vec2(1,1)), f.x), f.y);
}
float fbm(vec2 p) {
    float v = 0.0, a = 0.5;
    for (int i = 0; i < 4; i++) { v += a * noise(p); p *= 2.0; a *= 0.5; }
    return v;
}

// Edge falloff — reduces effect strength near borders
float edge_falloff(vec2 uv) {
    vec2 d = abs(uv - 0.5) * 2.0;
    float e = max(d.x, d.y);
    return 1.0 - smoothstep(0.7, 1.0, e);
}

void main() {
    vec2 uv = v_uv;
    float t = u_time * 0.3;
    float edge = edge_falloff(uv);

    // Multi-octave domain warp
    vec2 q = vec2(fbm(uv * 2.0 + t), fbm(uv * 2.0 + t + vec2(5.2, 1.3)));
    vec2 r = vec2(fbm(uv * 3.0 + q + t * 0.5), fbm(uv * 3.0 + q + t * 0.5 + vec2(1.7, 9.2)));
    float strength = u_warp_amount * 0.08 * edge;
    uv += r * strength;

    // Chromatic aberration scaled by warp
    float ca = u_chroma_amount * 0.004 * (1.0 + length(r) * 2.0);
    float cr = texture(u_tex, uv + vec2(ca, 0.0)).r;
    float cg = texture(u_tex, uv).g;
    float cb = texture(u_tex, uv - vec2(ca, 0.0)).b;

    fragColor = vec4(cr, cg, cb, 1.0);
}
