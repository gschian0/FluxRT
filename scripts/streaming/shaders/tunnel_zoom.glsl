#version 330
// SHADER: tunnel_zoom
// Infinite tunnel zoom with twist — feels like flying through the image
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

    // Polar coordinates
    float r = length(uv);
    float a = atan(uv.y, uv.x);

    // Zoom: continuously decreasing r creates tunnel effect
    float zoom = mod(u_time * 0.15, 1.0);
    r = r * (1.0 + u_displace_amount * 0.5) - zoom * 0.3;
    r = max(r, 0.001);

    // Twist
    a += sin(u_time * 0.3) * u_warp_amount * 0.5 + r * 2.0 * u_warp_amount;

    // Back to cartesian
    uv = vec2(cos(a), sin(a)) * r;
    uv.x /= aspect;
    uv += 0.5;

    // Chromatic aberration radial
    float ca = u_chroma_amount * 0.004 * r;
    vec2 ca_dir = normalize(uv - 0.5) * ca;
    float cr = texture(u_tex, uv + ca_dir).r;
    float cg = texture(u_tex, uv).g;
    float cb = texture(u_tex, uv - ca_dir).b;

    fragColor = vec4(cr, cg, cb, 1.0);
}
