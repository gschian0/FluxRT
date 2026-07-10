#version 330
// SHADER: acid_breathe
// Breathing zoom + color cycling + swirl — psychedelic vibe
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

    // Breathing zoom
    float breathe = sin(u_time * 0.5) * 0.1 * u_displace_amount;
    uv *= 1.0 + breathe;

    // Swirl — rotation amount increases with distance from center
    float r = length(uv);
    float swirl = sin(u_time * 0.3) * u_warp_amount * 2.0;
    float a = atan(uv.y, uv.x) + swirl * r;
    uv = vec2(cos(a), sin(a)) * r;

    // Back to texture coords
    uv.x /= aspect;
    uv += 0.5;

    // Color cycling — hue shift via RGB rotation
    float hue_shift = sin(u_time * 0.4) * u_chroma_amount * 0.3;
    vec3 col = texture(u_tex, uv).rgb;

    // Simple hue rotation matrix
    float c = cos(hue_shift * PI);
    float s = sin(hue_shift * PI);
    vec3 hcol = vec3(
        dot(col, vec3(0.299 + 0.701*c, 0.587 - 0.587*c, 0.114 - 0.114*c)),
        dot(col, vec3(0.299 - 0.299*c, 0.587 + 0.413*c, 0.114 - 0.114*s)),
        dot(col, vec3(0.299 - 0.299*s, 0.587 - 0.588*s, 0.114 + 0.886*c))
    );

    // Chromatic aberration
    float ca = u_chroma_amount * 0.003;
    hcol.r = texture(u_tex, uv + vec2(ca, 0.0)).r;
    hcol.b = texture(u_tex, uv - vec2(ca, 0.0)).b;

    fragColor = vec4(hcol, 1.0);
}
