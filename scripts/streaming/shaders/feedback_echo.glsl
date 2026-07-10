#version 330
// SHADER: feedback_echo
// Echo/trail effect — blends current frame with time-offseted self-samples
// Creates motion trails and ghosting that makes the stream feel alive
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

void main() {
    vec2 uv = v_uv;
    float t = u_time;

    // Slow drift
    float drift = u_warp_amount * 0.02;
    uv.x += sin(t * 0.3 + uv.y * 5.0) * drift;
    uv.y += cos(t * 0.2 + uv.x * 5.0) * drift;

    // Sample at multiple offsets to create echo/trail
    float echo = u_displace_amount * 0.03;
    vec2 e1 = vec2(sin(t * 0.8), cos(t * 0.6)) * echo;
    vec2 e2 = vec2(sin(t * 1.2 + 2.0), cos(t * 0.9 + 1.0)) * echo * 1.5;

    vec3 c0 = texture(u_tex, uv).rgb;
    vec3 c1 = texture(u_tex, uv + e1).rgb;
    vec3 c2 = texture(u_tex, uv + e2).rgb;

    // Blend echoes with decreasing weight
    vec3 col = c0 * 0.5 + c1 * 0.3 + c2 * 0.2;

    // Chromatic aberration
    float ca = u_chroma_amount * 0.003;
    col.r = texture(u_tex, uv + vec2(ca, 0.0)).r * 0.5 + texture(u_tex, uv + e1 + vec2(ca, 0.0)).r * 0.3 + texture(u_tex, uv + e2 + vec2(ca, 0.0)).r * 0.2;
    col.b = texture(u_tex, uv - vec2(ca, 0.0)).b * 0.5 + texture(u_tex, uv + e1 - vec2(ca, 0.0)).b * 0.3 + texture(u_tex, uv + e2 - vec2(ca, 0.0)).b * 0.2;

    fragColor = vec4(col, 1.0);
}
