#version 330
// SHADER: ripple_wave
// Concentric ripples emanating from moving centers — like water drops
in vec2 v_uv;
out vec4 fragColor;
uniform sampler2D u_tex;
uniform float u_time;
uniform vec2 u_resolution;
uniform float u_warp_amount;
uniform float u_chroma_amount;
uniform float u_displace_amount;

void main() {
    vec2 uv = v_uv;
    float aspect = u_resolution.x / u_resolution.y;
    vec2 p = uv - 0.5;
    p.x *= aspect;

    // Two moving ripple centers
    vec2 c1 = vec2(sin(u_time * 0.3) * 0.3, cos(u_time * 0.2) * 0.2);
    vec2 c2 = vec2(cos(u_time * 0.25) * 0.25, sin(u_time * 0.35) * 0.3);

    float d1 = length(p - c1);
    float d2 = length(p - c2);

    // Ripple displacement
    float amp = u_warp_amount * 0.015;
    float ripple1 = sin(d1 * 30.0 - u_time * 3.0) * amp / (1.0 + d1 * 3.0);
    float ripple2 = sin(d2 * 25.0 - u_time * 2.0) * amp / (1.0 + d2 * 3.0);

    vec2 dir1 = normalize(p - c1 + 0.001);
    vec2 dir2 = normalize(p - c2 + 0.001);

    uv += dir1 * ripple1 + dir2 * ripple2;

    // Chromatic aberration stronger near ripple centers
    float ca = u_chroma_amount * 0.003 * (1.0 + (ripple1 + ripple2) * 50.0);
    float cr = texture(u_tex, uv + vec2(ca, 0.0)).r;
    float cg = texture(u_tex, uv).g;
    float cb = texture(u_tex, uv - vec2(ca, 0.0)).b;

    fragColor = vec4(cr, cg, cb, 1.0);
}
