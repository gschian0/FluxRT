#version 330
// SHADER: glitch_digital
// Digital glitch — datamosh, scanline tearing, RGB split, random blocks
in vec2 v_uv;
out vec4 fragColor;
uniform sampler2D u_tex;
uniform float u_time;
uniform vec2 u_resolution;
uniform float u_warp_amount;
uniform float u_chroma_amount;
uniform float u_displace_amount;

float rand(vec2 p) { return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }

void main() {
    vec2 uv = v_uv;
    float t = u_time;

    // Scanline tear — offset random horizontal bands
    float line = floor(uv.y * u_resolution.y);
    float tear = step(0.92, rand(vec2(line, floor(t * 8.0))));
    uv.x += tear * u_displace_amount * 0.2 * (rand(vec2(line, t)) - 0.5);

    // Datamosh blocks — small rectangular regions get shifted
    vec2 block = floor(uv * vec2(40.0, 20.0));
    float block_rand = rand(block + vec2(floor(t * 3.0), 0.0));
    if (block_rand > 0.85) {
        uv.x += (block_rand - 0.85) * u_warp_amount * 0.3;
    }

    // Scanline darkening
    float scan = sin(uv.y * u_resolution.y * 3.14159) * 0.04 * u_chroma_amount;
    vec3 col = texture(u_tex, uv).rgb - scan;

    // Strong RGB split
    float ca = u_chroma_amount * 0.008;
    col.r = texture(u_tex, uv + vec2(ca, 0.0)).r - scan;
    col.b = texture(u_tex, uv - vec2(ca, 0.0)).b - scan;

    // Occasional full-frame horizontal shift
    float big_shift = step(0.98, rand(vec2(floor(t * 2.0))));
    if (big_shift > 0.5) {
        col = texture(u_tex, uv + vec2(0.05 * u_displace_amount, 0.0)).rgb;
    }

    fragColor = vec4(col, 1.0);
}
