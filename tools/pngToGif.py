#!/usr/bin/env python3
import argparse
import re
from pathlib import Path
from typing import List, Tuple, Optional

from PIL import Image

def natural_key(s: str):
    # Split into digit/non-digit chunks for natural sorting
    return [int(t) if t.isdigit() else t.lower() for t in re.findall(r'\d+|\D+', s)]

def parse_size(s: str) -> Tuple[int, int]:
    try:
        w, h = s.lower().replace("x", " ").split()
        return int(w), int(h)
    except Exception:
        raise argparse.ArgumentTypeError("Size must look like 800x600")

def collect_pngs(folder: Path, recursive: bool) -> List[Path]:
    pattern = "**/*.png" if recursive else "*.png"
    return sorted(folder.glob(pattern), key=lambda p: natural_key(p.name))

def ensure_mode(img: Image.Image, allow_transparency: bool) -> Image.Image:
    if allow_transparency:
        # Keep RGBA so GIF can use a single transparent index later
        if img.mode != "RGBA":
            return img.convert("RGBA")
        return img
    else:
        # Flatten against white background
        if img.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])  # alpha
            return bg
        return img.convert("RGB")

def resize_or_fit(
    img: Image.Image,
    resize_to: Optional[Tuple[int, int]],
    fit_canvas: Optional[Tuple[int, int]],
    resample: int
) -> Image.Image:
    if resize_to:
        return img.resize(resize_to, resample=resample)

    if fit_canvas:
        # Letterbox/pad to target canvas size while preserving aspect
        target_w, target_h = fit_canvas
        img_aspect = img.width / img.height
        target_aspect = target_w / target_h

        if img_aspect > target_aspect:
            # width-limited
            new_w = target_w
            new_h = int(round(new_w / img_aspect))
        else:
            # height-limited
            new_h = target_h
            new_w = int(round(new_h * img_aspect))

        resized = img.resize((new_w, new_h), resample=resample)

        # Create transparent canvas
        canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
        offset = ((target_w - new_w) // 2, (target_h - new_h) // 2)
        canvas.paste(resized, offset)
        return canvas

    return img

def unify_to_common_size(frames: List[Image.Image]) -> List[Image.Image]:
    # If sizes differ and no explicit size provided, pad to max width/height
    max_w = max(img.width for img in frames)
    max_h = max(img.height for img in frames)
    out = []
    for img in frames:
        if img.size == (max_w, max_h):
            out.append(img)
        else:
            canvas = Image.new("RGBA", (max_w, max_h), (0, 0, 0, 0))
            offset = ((max_w - img.width) // 2, (max_h - img.height) // 2)
            canvas.paste(img, offset)
            out.append(canvas)
    return out

def main():
    parser = argparse.ArgumentParser(description="Convert a folder of PNGs into an animated GIF.")
    parser.add_argument("input_folder", type=Path, help="Folder containing PNGs")
    parser.add_argument("-o", "--output", type=Path, default=Path("output.gif"), help="Output GIF path")
    parser.add_argument("-r", "--recursive", action="store_true", help="Search subfolders")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--fps", type=float, help="Frames per second (e.g., 10). Sets per-frame duration = 1000/fps ms")
    group.add_argument("--duration", type=float, help="Per-frame duration in milliseconds (e.g., 100)")
    parser.add_argument("--loop", type=int, default=0, help="Loop count (0=infinite)")
    parser.add_argument("--reverse", action="store_true", help="Also play frames in reverse (ping-pong without duplicating endpoints)")
    parser.add_argument("--resize", type=parse_size, help="Force all frames to this size, e.g. 800x600")
    parser.add_argument("--fit", type=parse_size, help="Letterbox/pad frames onto this canvas size, e.g. 800x600")
    parser.add_argument("--no-alpha", action="store_true", help="Flatten transparency onto white background")
    parser.add_argument("--resample", choices=["nearest", "bilinear", "bicubic", "lanczos"], default="lanczos",
                        help="Resampling filter for resizing/fitting")
    parser.add_argument("--optimize", action="store_true", help="Ask Pillow to optimize the GIF palette")
    parser.add_argument("--dither", choices=["none", "floyd-steinberg"], default="floyd-steinberg",
                        help="Dithering for palette conversion")
    args = parser.parse_args()

    if not args.input_folder.exists():
        raise SystemExit(f"Input folder not found: {args.input_folder}")

    pngs = collect_pngs(args.input_folder, args.recursive)
    if not pngs:
        raise SystemExit("No PNG files found.")

    # Compute duration per frame
    if args.fps:
        if args.fps <= 0:
            raise SystemExit("FPS must be > 0")
        duration_ms = int(round(1000.0 / args.fps))
    else:
        duration_ms = int(round(args.duration if args.duration else 100))  # default 100 ms

    resample_map = {
        "nearest": Image.NEAREST,
        "bilinear": Image.BILINEAR,
        "bicubic": Image.BICUBIC,
        "lanczos": Image.LANCZOS,
    }
    resample = resample_map[args.resample]

    # Load, convert, and size-normalize frames
    frames: List[Image.Image] = []
    allow_transparency = not args.no_alpha

    for p in pngs:
        img = Image.open(p)
        img = ensure_mode(img, allow_transparency=allow_transparency)
        img = resize_or_fit(img, args.resize, args.fit, resample=resample)
        frames.append(img)

    # If no explicit size was given and sizes differ, pad to max
    if not args.resize and not args.fit:
        sizes = {im.size for im in frames}
        if len(sizes) > 1:
            frames = unify_to_common_size(frames)

    # Optional ping-pong effect
    if args.reverse and len(frames) > 1:
        tail = frames[-2:0:-1]  # reverse without duplicating endpoints
        frames = frames + tail

    # CHANGES FOR TRANSPARENCY START ------------------------------------------
    # We'll quantize to 255 colors (leaving index 255 free), then force transparent
    # pixels to palette index 255 and mark that index as transparent.
    dither_flag = Image.FLOYDSTEINBERG if args.dither == "floyd-steinberg" else Image.NONE
    TRANS_IDX = 255  # reserved transparent palette index (0-255)

    def to_palette_with_transparency(img: Image.Image) -> Tuple[Image.Image, bool]:
        """Return (paletted_image, had_any_transparent_pixels)."""
        if img.mode == "RGBA":
            alpha = img.getchannel("A")
            # Threshold: treat alpha <= 128 as transparent (tweak if you want)
            # Create a mask where 255 = transparent pixel, 0 = opaque
            mask = alpha.point(lambda a: 255 if a <= 128 else 0)
            # Quantize to 255 colors so index 255 is available for transparency
            pal = img.convert("RGBA").convert(
                "P", palette=Image.ADAPTIVE, colors=255, dither=dither_flag
            )
            # Force transparent areas to use index 255
            pal.paste(TRANS_IDX, mask)
            # Tell Pillow which palette index is transparent
            pal.info["transparency"] = TRANS_IDX
            # If there were any transparent pixels at all:
            any_transparent = mask.getbbox() is not None
            return pal, any_transparent
        else:
            # No alpha in source; just palettize normally
            pal = img.convert("P", palette=Image.ADAPTIVE, dither=dither_flag)
            return pal, False
    # CHANGES FOR TRANSPARENCY END --------------------------------------------

    pal_frames: List[Image.Image] = []
    any_frame_transparent = False
    for im in frames:
        p, had = to_palette_with_transparency(im) if allow_transparency else (
            im.convert("P", palette=Image.ADAPTIVE, dither=dither_flag), False
        )
        pal_frames.append(p)
        any_frame_transparent = any_frame_transparent or had

    # Pillow expects first frame + append_images for the rest
    first, *rest = pal_frames

    args.output.parent.mkdir(parents=True, exist_ok=True)

    save_kwargs = dict(
        save_all=True,
        append_images=rest,
        duration=duration_ms,
        loop=args.loop,
        disposal=2,            # restore to background between frames (important for transparency)
        optimize=args.optimize
    )
    # Only set the transparency kwarg if we actually used it; this avoids odd palettes for opaque GIFs
    if allow_transparency and any_frame_transparent:
        save_kwargs["transparency"] = TRANS_IDX

    first.save(args.output, **save_kwargs)
    print(f"GIF saved to: {args.output.resolve()}")

if __name__ == "__main__":
    main()
