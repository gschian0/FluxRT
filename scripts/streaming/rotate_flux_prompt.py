#!/usr/bin/env python3
import argparse
import random
import time

from gradio_client import Client


PROMPTS = [
    "8k ultra high resolution claymation character video frame, expressive handmade alien hosts, visible fingerprints in clay, miniature broadcast studio, saturated practical lights, crisp macro lens detail, cinematic depth of field",
    "8k high resolution 3D render, computer graphics alien variety show, glossy toy-like characters, ray-traced reflections, global illumination, sharp bevels, vivid color grading, polished cinematic frame",
    "8k high resolution carpet world, fuzzy characters made of fur and woven yarn, shag textile mountains, tufted furniture, colorful fiber landscape, macro texture detail, bright studio lighting, crisp broadcast image",
    "8k high resolution oil painting video frame, alien host in a glowing studio, thick impasto texture, luminous pigments, sharp facial detail, dramatic rim light, vivid museum-grade composition",
    "8k high resolution two-bit dither computer graphics world, alien musician characters, chunky pixel dithering, limited-color poster palette, razor-sharp edges, retro digital broadcast, vivid high contrast lighting",
    "8k high resolution claymation music channel, alien band characters, handmade instruments, tactile fingerprints, saturated gels, miniature cables, cinematic close-up, clean sharp video frame",
    "8k high resolution fur puppet talk show, dense colorful fibers, expressive eyes, tiny embroidered props, shallow depth of field, warm key light, crisp cinematic camera framing",
    "8k high resolution velvet and felt stop-motion universe, alien news desk, handmade seams, bright practical gels, miniature set dressing, sharp close-up, polished broadcast frame",
    "8k high resolution ceramic clay alien talk show, glazed porcelain faces, hand-painted patterns, glossy reflections, dramatic black stage, vivid color contrast, sharp studio photography",
    "8k high resolution glass sculpture broadcast world, translucent alien host, neon refractions, prism highlights, clean black background, cinematic macro detail, crystalline sharpness",
    "8k high resolution retro computer animation, alien host rendered in early CGI style, chrome gradients, wireframe props, dithered shadows, vivid neon grid floor, crisp synthetic camera angle",
    "8k high resolution embroidered textile cartoon world, alien musicians on a tiny stage, thread texture, stitched outlines, bright fabric props, crisp macro detail, vivid color design",
    "8k high resolution black and white ink wash broadcast, expressive alien host, sharp brush lines, high contrast studio lighting, subtle film grain, elegant cinematic framing",
    "8k high resolution 1980s airbrush sci-fi poster come alive, chrome alien presenter, glowing grid floor, vivid magenta and teal lights, crisp edges, glossy broadcast quality",
    "8k high resolution miniature diorama livestream studio, alien characters made from paper clay and velvet, practical LEDs, tiny cables, detailed set dressing, cinematic depth of field",
    "8k high resolution comic book paint style, alien host in a crowded surreal studio, bold clean outlines, vivid halftone texture, dramatic camera angle, sharp broadcast-ready frame",
    "8k high resolution surreal botanical studio set, alien presenter made of lacquered petals and moss-like fibers, vivid greens and coral lights, macro detail, crisp cinematic image",
    "8k high resolution hyperreal textile planet, carpet mountains and fuzzy buildings, fur-covered characters hosting a music show, bright pop colors, sharp macro lens, polished frame",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Rotate FluxRT prompts through the Gradio API")
    parser.add_argument("--gradio-url", default="http://127.0.0.1:7862")
    parser.add_argument("--interval", type=float, default=90.0)
    parser.add_argument("--jitter", type=float, default=20.0)
    parser.add_argument("--shuffle", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    client = Client(args.gradio_url)
    prompts = list(PROMPTS)
    index = 0
    print(f"[prompt-rotator] connected to {args.gradio_url}", flush=True)
    while True:
        if args.shuffle:
            prompt = random.choice(prompts)
        else:
            prompt = prompts[index % len(prompts)]
            index += 1
        try:
            client.predict(prompt, api_name="/set_prompt")
            print(f"[prompt-rotator] set prompt: {prompt}", flush=True)
        except Exception as exc:
            print(f"[prompt-rotator] failed: {type(exc).__name__}: {exc}", flush=True)
        delay = max(10.0, float(args.interval) + random.uniform(-args.jitter, args.jitter))
        time.sleep(delay)


if __name__ == "__main__":
    main()