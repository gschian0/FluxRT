#version 330
// SHADER: flow_field
// Noise-based flow field displacement — particles flowing across the image
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
    for (int i = 0; i < 3; i++) { v += a * noise(p); p *= 2.0; a *= 0.5; }
    return v;
}

void main() {
    vec2 uv = v_uv;
    float t = u_time * 0.2;

    // Flow field: angle from noise, displace along flow direction
    float angle = fbm(uv * 4.0 + t) * 6.28;
    vec2 flow = vec2(cos(angle), sin(angle));
    float strength = u_displace_amount * 0.04;
    uv += flow * strength * sin(u_time * 0.5 + uv.y * 10.0);

    // Secondary warp layer
    float w = u_warp_amount * 0.03;
    uv.x += sin(uv.y * 15.0 + u_time) * w;
    uv.y += cos(uv.x * 15.0 + u_time * 0.7) * w;

    // Chromatic aberration along flow direction
    float ca = u_chroma_amount * 0.003;
    vec2 ca_dir = flow * ca;
    float cr = texture(u_tex, uv + ca_dir).r;
    float cg = texture(u_tex, uv).g;
    float cb = texture(u_tex, uv - ca_dir).b;

    fragColor = vec4(cr, cg, cb, 1.0);
}
