#version 330
// SHADER: kaleidoscope
// Kaleidoscopic mirror with rotation and zoom
in vec2 v_uv;
out vec4 fragColor;
uniform sampler2D u_tex;
uniform float u_time;
uniform vec2 u_resolution;
uniform float u_warp_amount;
uniform float u_chroma_amount;
uniform float u_displace_amount;

#define PI 3.14159265

void main() {
    vec2 uv = v_uv - 0.5;
    float aspect = u_resolution.x / u_resolution.y;
    uv.x *= aspect;

    // Rotate slowly
    float angle = u_time * 0.15;
    float s = sin(angle), c = cos(angle);
    uv = mat2(c, -s, s, c) * uv;

    // Kaleidoscope segments
    float segments = 6.0 + floor(u_warp_amount * 6.0);
    float r = length(uv);
    float a = atan(uv.y, uv.x);
    a = mod(a, 2.0 * PI / segments);
    a = abs(a - PI / segments);
    uv = vec2(cos(a), sin(a)) * r;

    // Zoom pulse
    float zoom = 1.0 + sin(u_time * 0.4) * 0.1 * u_displace_amount;
    uv /= zoom;

    // Back to texture coords
    uv.x /= aspect;
    uv += 0.5;

    // Chromatic aberration
    float ca = u_chroma_amount * 0.003;
    float cr = texture(u_tex, uv + vec2(ca, 0.0)).r;
    float cg = texture(u_tex, uv).g;
    float cb = texture(u_tex, uv - vec2(ca, 0.0)).b;

    fragColor = vec4(cr, cg, cb, 1.0);
}
