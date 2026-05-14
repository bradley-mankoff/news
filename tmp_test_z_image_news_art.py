# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "huggingface_hub>=0.36.0",
#   "pillow>=10.0.0",
# ]
# ///
"""Temporary test for news-summary image generation.

Recommended Apple Silicon path, using FLUX.2 klein through mflux:

    uv run --with "mflux>=0.16.0" tmp_test_z_image_news_art.py --backend mflux

Z-Image hosted provider comparison:

    HF_TOKEN=... uv run tmp_test_z_image_news_art.py --backend zimage-hosted

Z-Image local diffusers comparison, best on a CUDA GPU with enough VRAM:

    uv run \
      --with torch \
      --with "diffusers @ git+https://github.com/huggingface/diffusers" \
      --with accelerate \
      --with safetensors \
      tmp_test_z_image_news_art.py --backend zimage-local

The script writes:

- a raw generated PNG
- a final PNG with a clean code-rendered text footer
- a Markdown output with the image referenced
- an HTML output with the image embedded as base64
- a JSON stats file with timing/model/seed/settings

That mirrors the integration shape you would want in the news pipeline: take the
final synthesis text, derive a compact visual prompt, generate an image, and
render it into the final output.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import platform
import random
import shutil
import subprocess
import tempfile
import time
from datetime import date
from pathlib import Path
from textwrap import dedent

FLUX_MFLUX_MODEL_ID = "Runpod/FLUX.2-klein-4B-mflux-4bit"
FLUX_MFLUX_BASE_MODEL = "flux2-klein-4b"
Z_IMAGE_MODEL_ID = "Tongyi-MAI/Z-Image-Turbo"

MODEL_LABELS = {
    "mflux": "FLUX.2 klein 4B mflux 4-bit",
    "zimage-hosted": "Z-Image-Turbo hosted provider",
    "zimage-local": "Z-Image-Turbo local diffusers",
}

SAMPLE_FINAL_OUTPUTS = [
    dedent(
        """
        Power grids are becoming the quiet center of the climate transition.
        Utilities in the Midwest and Southwest are racing to add transmission,
        battery storage, and demand-response programs as data centers and
        factories lift electricity demand faster than planners expected.

        The policy fight is no longer only about whether to build renewables.
        It is about who pays for grid upgrades, how quickly permits move, and
        whether local communities see lower bills or just more construction.
        For households, the practical signal is mixed: cleaner power is scaling,
        but reliability and affordability now depend on boring infrastructure
        decisions that are suddenly very political.
        """
    ).strip(),
    dedent(
        """
        A wave of hospital mergers is reshaping regional health care. Executives
        say consolidation gives smaller hospitals the balance sheets needed to
        keep emergency rooms open, upgrade records systems, and recruit
        specialists. Patient advocates see a different pattern: fewer local
        choices, higher negotiated prices, and longer drives for routine care.

        The day-to-day implication is less dramatic than a single headline but
        more durable. Health care access is being redrawn by finance, staffing,
        and geography at the same time, leaving regulators to decide when a
        rescue becomes a monopoly.
        """
    ).strip(),
    dedent(
        """
        Cities are changing how they respond to heat. Several large metro areas
        are treating extreme temperatures less like a seasonal inconvenience and
        more like a public-safety event, opening cooling centers, mapping
        high-risk blocks, and testing reflective pavement and tree-canopy plans.

        The central tension is speed. Emergency measures can reduce deaths this
        summer, but the deeper fixes require housing upgrades, shaded streets,
        utility protections, and budgets that survive after the temperature
        drops. Climate adaptation is moving from planning documents into normal
        city operations.
        """
    ).strip(),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a text-free news photo plus a code-rendered headline footer."
    )
    parser.add_argument(
        "--backend",
        choices=("auto", "mflux", "zimage-hosted", "zimage-local"),
        default="auto",
        help="Use recommended mflux/MLX, Z-Image hosted/local, or auto-detect.",
    )
    parser.add_argument(
        "--provider",
        default=os.environ.get("HF_INFERENCE_PROVIDER", "fal-ai"),
        help="Hosted provider for Z-Image. The model card currently lists fal-ai and wavespeed.",
    )
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument(
        "--steps",
        type=int,
        help="Inference steps. Defaults: mflux=4, Z-Image=9.",
    )
    parser.add_argument("--seed", type=int, default=random.randint(1, 2**31 - 1))
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("output") / "tmp_news_image_art",
        help="Directory for generated test artifacts.",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        help="Optional text file containing a real final synthesis to illustrate.",
    )
    parser.add_argument(
        "--headline",
        help="Optional readable overlay headline. Defaults to a short headline derived from the summary.",
    )
    parser.add_argument(
        "--no-overlay",
        action="store_true",
        help="Write only the raw generated image, with no code-rendered headline overlay.",
    )
    parser.add_argument(
        "--crop-bottom-ratio",
        type=float,
        default=0.12,
        help="Crop this fraction from the bottom before adding the clean footer overlay.",
    )
    return parser.parse_args()


def load_summary(path: Path | None) -> str:
    if path is None:
        return random.choice(SAMPLE_FINAL_OUTPUTS)
    return path.read_text(encoding="utf-8").strip()


def build_image_prompt(summary: str) -> str:
    compact_summary = " ".join(summary.split())
    if len(compact_summary) > 900:
        compact_summary = compact_summary[:900].rsplit(" ", 1)[0] + "..."

    return dedent(
        f"""
        Create one plain text-free documentary photograph that visually suggests this
        news synthesis without depicting an article, poster, flyer, magazine
        spread, web page, screen, infographic, report, newspaper, presentation
        slide, broadcast graphic, captioned image, meme, or social media card:
        {compact_summary}

        Visual target: one coherent real-world scene, photographed as if for a
        wire-service photo archive. For this topic, prefer physical
        infrastructure and environmental context over symbolic graphics.

        Important: the image must contain no typography of any kind. Do not
        create signs, banners, labels, captions, posters, headlines, subtitles,
        watermarks, UI panels, lower thirds, footer panels, paragraph blocks, title cards,
        placards, screens, documents, newspapers, maps, charts, or any shapes
        that resemble letters or writing.

        Composition: natural camera perspective, no graphic design layout, no
        border bands, no poster framing, no text boxes, no blank caption area,
        no dark strip at the bottom, no empty panel. Fill the entire frame with
        the photographed scene only.
        """
    ).strip()


def build_negative_prompt() -> str:
    return (
        "text, letters, words, captions, headline, title, sign, signage, label, "
        "logo, watermark, newspaper front page, poster text, chart text, map labels, "
        "subtitles, pseudo text, gibberish writing, unreadable writing"
    )


def derive_overlay_headline(summary: str) -> str:
    first_sentence = " ".join(summary.split()).split(". ", 1)[0].strip(". ")
    words = first_sentence.split()
    if len(words) > 9:
        first_sentence = " ".join(words[:9])
    return first_sentence or "Daily News Brief"


def resolve_backend(requested_backend: str) -> str:
    if requested_backend != "auto":
        return requested_backend

    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return "mflux"
    if os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACEHUB_API_TOKEN"):
        return "zimage-hosted"
    return "zimage-local"


def resolve_steps(backend: str, requested_steps: int | None) -> int:
    if requested_steps is not None:
        return requested_steps
    if backend == "mflux":
        return 4
    return 9


def generate_with_mflux(
    prompt: str,
    *,
    width: int,
    height: int,
    steps: int,
    seed: int,
) -> object:
    if shutil.which("mflux-generate-flux2") is None:
        raise SystemExit(
            "The recommended local backend requires the mflux CLI, but "
            "`mflux-generate-flux2` is not on PATH.\n"
            "Run: uv run --with \"mflux>=0.16.0\" "
            "tmp_test_z_image_news_art.py --backend mflux"
        )

    try:
        from PIL import Image
    except ImportError as error:
        raise SystemExit("Pillow is required to read the mflux output image.") from error

    with tempfile.TemporaryDirectory(prefix="news-art-mflux-") as temp_dir:
        temp_path = Path(temp_dir)
        prompt_path = temp_path / "prompt.txt"
        output_path = temp_path / "mflux-output.png"
        prompt_path.write_text(prompt + "\n", encoding="utf-8")

        command = [
            "mflux-generate-flux2",
            "--model",
            FLUX_MFLUX_MODEL_ID,
            "--base-model",
            FLUX_MFLUX_BASE_MODEL,
            "--prompt-file",
            str(prompt_path),
            "--seed",
            str(seed),
            "--height",
            str(height),
            "--width",
            str(width),
            "--steps",
            str(steps),
            "--output",
            str(output_path),
        ]
        print("Running:", " ".join(command))
        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError as error:
            raise SystemExit(f"mflux generation failed with exit code {error.returncode}.") from error

        if not output_path.exists():
            raise SystemExit(f"mflux finished but did not create {output_path}.")

        with Image.open(output_path) as image:
            return image.copy()


def generate_with_zimage_hosted(
    prompt: str,
    *,
    provider: str,
    width: int,
    height: int,
    steps: int,
) -> object:
    from huggingface_hub import InferenceClient

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACEHUB_API_TOKEN")
    client_kwargs = {"provider": provider}
    if token:
        client_kwargs["api_key"] = token
    client = InferenceClient(**client_kwargs)

    try:
        return client.text_to_image(
            prompt,
            model=Z_IMAGE_MODEL_ID,
            width=width,
            height=height,
            num_inference_steps=steps,
            guidance_scale=0.0,
            negative_prompt=build_negative_prompt(),
        )
    except TypeError:
        return client.text_to_image(prompt, model=Z_IMAGE_MODEL_ID)


def make_generator(torch: object, device: str, seed: int) -> object:
    try:
        return torch.Generator(device=device).manual_seed(seed)
    except Exception:
        return torch.Generator(device="cpu").manual_seed(seed)


def generate_with_zimage_local_diffusers(
    prompt: str,
    *,
    width: int,
    height: int,
    steps: int,
    seed: int,
) -> object:
    try:
        import torch
        from diffusers import ZImagePipeline
    except ImportError as error:
        raise SystemExit(
            "Local backend requires torch and a recent diffusers with ZImagePipeline.\n"
            "Run the local command in this script's docstring, or use "
            "`--backend zimage-hosted` with HF_TOKEN."
        ) from error

    if torch.cuda.is_available():
        device = "cuda"
        dtype = torch.bfloat16
    elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        device = "mps"
        dtype = torch.float16
    else:
        device = "cpu"
        dtype = torch.float32

    print(f"Loading {Z_IMAGE_MODEL_ID} on {device} with dtype={dtype}...")
    pipe = ZImagePipeline.from_pretrained(
        Z_IMAGE_MODEL_ID,
        torch_dtype=dtype,
        low_cpu_mem_usage=False,
    )
    pipe.to(device)

    image = pipe(
        prompt=prompt,
        negative_prompt=build_negative_prompt(),
        height=height,
        width=width,
        num_inference_steps=steps,
        guidance_scale=0.0,
        generator=make_generator(torch, device, seed),
    ).images[0]

    return image


def load_overlay_font(size: int) -> object:
    from PIL import ImageFont

    font_candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ]
    for font_path in font_candidates:
        if Path(font_path).exists():
            return ImageFont.truetype(font_path, size=size)
    return ImageFont.load_default()


def wrap_text_to_width(draw: object, text: str, font: object, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return []

    lines: list[str] = []
    current_line = words[0]
    for word in words[1:]:
        candidate = f"{current_line} {word}"
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current_line = candidate
        else:
            lines.append(current_line)
            current_line = word
    lines.append(current_line)
    return lines


def add_headline_overlay(image: object, headline: str, *, crop_bottom_ratio: float) -> object:
    from PIL import Image, ImageDraw

    working = image.convert("RGB")
    width, height = working.size
    crop_bottom_ratio = min(max(crop_bottom_ratio, 0.0), 0.35)
    cropped_height = max(1, int(height * (1.0 - crop_bottom_ratio)))
    cropped_image = working.crop((0, 0, width, cropped_height))

    margin = max(28, width // 28)
    font_size = max(34, width // 22)
    footer_padding_x = max(28, width // 30)
    footer_padding_y = max(22, width // 34)
    font = load_overlay_font(font_size)
    scratch = Image.new("RGB", (width, 1))
    draw = ImageDraw.Draw(scratch)
    max_text_width = width - (footer_padding_x * 2)
    lines = wrap_text_to_width(draw, headline, font, max_text_width)
    line_height = int(font_size * 1.22)
    footer_height = (line_height * max(1, len(lines))) + (footer_padding_y * 2)

    final_image = Image.new("RGB", (width, cropped_height + footer_height), (7, 10, 16))
    final_image.paste(cropped_image, (0, 0))
    draw = ImageDraw.Draw(final_image)

    text_y = cropped_height + footer_padding_y
    for line in lines:
        draw.text(
            (footer_padding_x, text_y),
            line,
            font=font,
            fill=(255, 255, 255, 255),
        )
        text_y += line_height

    return final_image


def render_markdown(report_title: str, summary: str, prompt: str, image_filename: str) -> str:
    return dedent(
        f"""
        # {report_title}

        ![Generated editorial illustration]({image_filename})

        ## Final Output

        {summary}

        ## Image Prompt

        ```text
        {prompt}
        ```
        """
    ).strip() + "\n"


def render_html(
    report_title: str,
    summary: str,
    prompt: str,
    image_bytes: bytes,
    *,
    stats: dict,
) -> str:
    encoded_image = base64.b64encode(image_bytes).decode("ascii")
    paragraphs = "".join(
        f"<p>{html.escape(paragraph)}</p>"
        for paragraph in summary.split("\n\n")
        if paragraph.strip()
    )
    escaped_prompt = html.escape(prompt)
    escaped_title = html.escape(report_title)
    stats_html = "".join(
        f"<li><strong>{html.escape(str(key))}:</strong> {html.escape(str(value))}</li>"
        for key, value in stats.items()
    )

    return dedent(
        f"""
        <!doctype html>
        <html lang="en">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>{escaped_title}</title>
        </head>
        <body style="margin:0; background:#f6f7f9; color:#111827; font-family:Arial, sans-serif;">
          <main style="max-width:760px; margin:0 auto; padding:32px 20px;">
            <h1 style="font-size:30px; line-height:1.2; margin:0 0 20px;">{escaped_title}</h1>
            <img
              alt="Generated editorial illustration"
              src="data:image/png;base64,{encoded_image}"
              style="width:100%; height:auto; border-radius:6px; display:block; margin:0 0 28px;"
            >
            <section style="font-size:17px; line-height:1.65;">
              {paragraphs}
            </section>
            <details style="margin-top:28px;">
              <summary>Image prompt</summary>
              <pre style="white-space:pre-wrap; font-size:13px; line-height:1.5;">{escaped_prompt}</pre>
            </details>
            <details style="margin-top:18px;">
              <summary>Generation stats</summary>
              <ul style="font-size:13px; line-height:1.6;">{stats_html}</ul>
            </details>
          </main>
        </body>
        </html>
        """
    ).strip() + "\n"


def main() -> int:
    args = parse_args()
    backend = resolve_backend(args.backend)
    steps = resolve_steps(backend, args.steps)
    summary = load_summary(args.summary)
    prompt = build_image_prompt(summary)
    headline = args.headline or derive_overlay_headline(summary)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    model_label = MODEL_LABELS[backend]
    output_stem = f"news_art_test_{backend.replace('-', '_')}"
    report_title = f"News Image Art Test - {model_label} - {date.today().isoformat()}"
    raw_image_path = args.out_dir / f"{output_stem}_raw.png"
    image_path = args.out_dir / f"{output_stem}_with_text.png"
    markdown_path = args.out_dir / f"{output_stem}.md"
    html_path = args.out_dir / f"{output_stem}.html"
    prompt_path = args.out_dir / f"{output_stem}_prompt.txt"
    stats_path = args.out_dir / f"{output_stem}_stats.json"

    print(f"Backend: {backend} ({model_label})")
    print(f"Model: {model_label}")
    print(f"Seed: {args.seed}")
    print(f"Steps: {steps}")
    print(f"Overlay headline: {headline}")
    print(f"Prompt preview:\n{prompt}\n")

    generation_started = time.perf_counter()
    if backend == "mflux":
        image = generate_with_mflux(
            prompt,
            width=args.width,
            height=args.height,
            steps=steps,
            seed=args.seed,
        )
    elif backend == "zimage-hosted":
        image = generate_with_zimage_hosted(
            prompt,
            provider=args.provider,
            width=args.width,
            height=args.height,
            steps=steps,
        )
    elif backend == "zimage-local":
        image = generate_with_zimage_local_diffusers(
            prompt,
            width=args.width,
            height=args.height,
            steps=steps,
            seed=args.seed,
        )
    else:
        raise SystemExit(f"Unsupported backend: {backend}")
    generation_seconds = time.perf_counter() - generation_started

    image.save(raw_image_path)
    final_image = image if args.no_overlay else add_headline_overlay(
        image,
        headline,
        crop_bottom_ratio=args.crop_bottom_ratio,
    )
    final_image.save(image_path)
    image_bytes = image_path.read_bytes()
    if backend == "mflux":
        model_id = FLUX_MFLUX_MODEL_ID
    else:
        model_id = Z_IMAGE_MODEL_ID
    stats = {
        "backend": backend,
        "model": model_label,
        "model_id": model_id,
        "width": args.width,
        "height": args.height,
        "steps": steps,
        "seed": args.seed,
        "generation_seconds": round(generation_seconds, 2),
        "overlay_enabled": not args.no_overlay,
        "overlay_headline": headline if not args.no_overlay else "",
        "crop_bottom_ratio": 0.0 if args.no_overlay else args.crop_bottom_ratio,
        "raw_image_path": str(raw_image_path),
        "final_image_path": str(image_path),
    }
    prompt_path.write_text(prompt + "\n", encoding="utf-8")
    stats_path.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(
        render_markdown(report_title, summary, prompt, image_path.name),
        encoding="utf-8",
    )
    html_path.write_text(
        render_html(report_title, summary, prompt, image_bytes, stats=stats),
        encoding="utf-8",
    )

    print(f"Generation seconds: {generation_seconds:.2f}")
    print(f"Wrote raw image: {raw_image_path}")
    print(f"Wrote image: {image_path}")
    print(f"Wrote markdown: {markdown_path}")
    print(f"Wrote HTML with embedded image: {html_path}")
    print(f"Wrote stats: {stats_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
