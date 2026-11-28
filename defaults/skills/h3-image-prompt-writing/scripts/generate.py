#!/usr/bin/env python3
"""Render a MiniMax-H3-Image prompt through the MiniMax API.

    export MINIMAX_API_KEY=<API key>
    export MINIMAX_API_BASE=https://api.minimax.io   # Global (default); China: https://api.minimax.cn

    # Text-to-image: the prompt is rendered as written
    python3 generate.py --prompt-file prompt.txt --aspect-ratio 3:4 --out poster

    # Editing: images in <Picture N> order, as local files or http(s) URLs
    python3 generate.py --prompt-file edit.txt --image room.jpg --image sofa.png --aspect-ratio 4:3 --out room_edit

Saves <out>_<i>.png and, when the API expanded the prompt, <out>_<i>.revised.txt.
Uses only the Python standard library.
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def image_type(data: bytes):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx"):
            return "image/heic"
        if brand in (b"mif1", b"msf1"):
            return "image/heif"
    return None


def image_ref(source: str) -> dict:
    if source.startswith(("http://", "https://", "data:")):
        return {"image_url": source}
    path = Path(source)
    if not path.is_file():
        sys.exit(f"Input image not found: {source}")
    data = path.read_bytes()
    mime = image_type(data)
    if mime is None:
        sys.exit(f"Unsupported image type: {source} (use JPG, PNG, WebP, HEIC or HEIF)")
    return {"image_url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    text = parser.add_mutually_exclusive_group(required=True)
    text.add_argument("--prompt", help="prompt text")
    text.add_argument("--prompt-file", type=Path, help="file containing the prompt")
    parser.add_argument("--image", action="append", default=[],
                        help="input image for editing, in <Picture N> order; repeat for several")
    parser.add_argument("--aspect-ratio", help="width:height between 1:3 and 3:1, e.g. 16:9")
    parser.add_argument("--area", default="4K", choices=["1K", "2K", "4K"], help="output area (default 4K)")
    parser.add_argument("--n", type=int, default=1, choices=range(1, 11), metavar="N",
                        help="number of images, 1-10 (default 1)")
    parser.add_argument("--enhance", action="store_true",
                        help="text-to-image only: let the API expand the prompt")
    parser.add_argument("--out", default="h3_image", help="output path prefix (default h3_image)")
    args = parser.parse_args()

    key = os.environ.get("MINIMAX_API_KEY")
    if not key:
        sys.exit("Set MINIMAX_API_KEY to your MiniMax API key.")
    base = os.environ.get("MINIMAX_API_BASE", "https://api.minimax.io").rstrip("/")
    if args.prompt_file is not None and not args.prompt_file.is_file():
        sys.exit(f"Prompt file not found: {args.prompt_file}")
    prompt = args.prompt if args.prompt is not None else args.prompt_file.read_text(encoding="utf-8").strip()

    body = {"model": "MiniMax-H3-Image", "prompt": prompt, "n": args.n, "area": args.area,
            "response_format": "url", "output_format": "png"}
    if args.aspect_ratio:
        body["aspect_ratio"] = args.aspect_ratio
    if args.image:
        path = "/v1/images/edits"
        body["images"] = [image_ref(s) for s in args.image]
    else:
        path = "/v1/images/generations"
        if not args.enhance:
            if not args.aspect_ratio:
                sys.exit("Give --aspect-ratio, or pass --enhance to let the API choose it.")
            body["disable_prompt_enhanced"] = True

    request = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {key}",
                                              "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        sys.exit(f"HTTP {error.code}: {error.read().decode(errors='replace')}")
    except (urllib.error.URLError, TimeoutError) as error:
        sys.exit(f"Request to {base} failed: {error}")
    except json.JSONDecodeError:
        sys.exit("The API returned a response that is not JSON.")

    print(f"request_id {result.get('request_id')}", file=sys.stderr)
    status = result.get("base_resp") or {}
    if status.get("status_code"):
        sys.exit(f"API error {status.get('status_code')}: {status.get('status_msg')}")
    items = result.get("data") or []
    if not items:
        sys.exit(f"No image returned: {json.dumps(result)[:500]}")

    out = Path(args.out)
    if out.suffix.lower() == ".png":
        out = out.with_suffix("")
    out.parent.mkdir(parents=True, exist_ok=True)
    saved = 0
    for i, item in enumerate(items, 1):
        if item.get("error") or not item.get("url"):
            print(f"image {i} failed: {item.get('error') or json.dumps(item)[:300]}", file=sys.stderr)
            continue
        image_path = out.parent / f"{out.name}_{i}.png"
        try:
            with urllib.request.urlopen(item["url"], timeout=300) as download:
                image_path.write_bytes(download.read())
        except (urllib.error.URLError, TimeoutError) as error:
            print(f"image {i} download failed: {error}", file=sys.stderr)
            continue
        line = f"{image_path} ({item.get('size')})"
        revised_path = image_path.with_suffix(".revised.txt")
        if item.get("revised_prompt") and item["revised_prompt"].strip() != prompt:
            revised_path.write_text(item["revised_prompt"], encoding="utf-8")
            line += f", expanded prompt in {revised_path}"
        elif revised_path.exists():
            revised_path.unlink()
        print(line)
        saved += 1
    if not saved:
        sys.exit(1)


if __name__ == "__main__":
    main()
