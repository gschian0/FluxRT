#version 330
// SHADER: pixel_sort
// Pixel sorting effect — shifts rows/columns based on luminance thresholds
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
    float t = u_time;

    // Sample base color for luminance
    vec3 base = texture(u_tex, uv).rgb;
    float lum = dot(base, vec3(0.299, 0.587, 0.114));

    // Horizontal pixel shift based on luminance and time
    float shift = u_displace_amount * 0.08;
    float wave = sin(floor(uv.y * u_resolution.y * 0.5) * 1.0 + t * 2.0);
    uv.x += wave * shift * (0.5 + lum);

    // Vertical pixel shift
    float vshift = u_warp_amount * 0.05;
    float vwave = sin(floor(uv.x * u_resolution.x * 0.3) * 1.0 + t * 1.5);
    uv.y += vwave * vshift * (0.5 + lum);

    // Glitch slices — random horizontal bands shift more
    float band = step(0.97, fract(sin(floor(uv.y * 80.0) * 12.9898 + t) * 43758.5453));
    uv.x += band * u_chroma_amount * 0.15 * sin(t * 10.0);

    // RGB channel split
    float ca = u_chroma_amount * 0.005;
    float cr = texture(u_tex, uv + vec2(ca, 0.0)).r;
    float cg = texture(u_tex, uv).g;
    float cb = texture(u_tex, uv - vec2(ca, 0.0)).b;

    fragColor = vec4(cr, cg, cb, 1.0);
}
