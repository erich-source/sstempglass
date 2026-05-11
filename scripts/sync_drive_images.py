#!/usr/bin/env python3
"""Sync public Google Drive folder images into the static website.

Workflow:
1. Download images from a public Google Drive folder using gdown.
2. Normalize, resize and convert images to WebP.
3. Save generated images into img/factory/.
4. Patch index.html so website image references use the local generated images.

This script is designed for GitHub Actions but can also run locally:

    python scripts/sync_drive_images.py \
      --folder-url "https://drive.google.com/drive/folders/1b95SP0zB1wr6jEZd7CiCCNUnEYGhH4Xe" \
      --limit 10
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageOps

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
DEFAULT_FOLDER_URL = "https://drive.google.com/drive/folders/1b95SP0zB1wr6jEZd7CiCCNUnEYGhH4Xe"


@dataclass(frozen=True)
class GeneratedImage:
    index: int
    source_name: str
    output_path: Path
    width: int
    height: int


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def ensure_clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def download_public_drive_folder(folder_url: str, target_dir: Path) -> None:
    """Download a public Google Drive folder with gdown.

    The folder must be shared as: Anyone with the link can view.
    """
    ensure_clean_dir(target_dir)
    cmd = [
        sys.executable,
        "-m",
        "gdown",
        "--folder",
        folder_url,
        "--output",
        str(target_dir),
        "--remaining-ok",
        "--fuzzy",
    ]
    run(cmd)


def iter_image_files(folder: Path) -> Iterable[Path]:
    for path in sorted(folder.rglob("*"), key=lambda p: (p.stat().st_mtime, p.name), reverse=True):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            yield path


def convert_to_webp(source: Path, output: Path, max_width: int, quality: int) -> tuple[int, int]:
    with Image.open(source) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")

        width, height = img.size
        if width > max_width:
            ratio = max_width / width
            new_size = (max_width, max(1, int(height * ratio)))
            img = img.resize(new_size, Image.Resampling.LANCZOS)

        output.parent.mkdir(parents=True, exist_ok=True)
        img.save(output, "WEBP", quality=quality, method=6)
        return img.size


def generate_images(raw_dir: Path, output_dir: Path, limit: int, max_width: int, quality: int) -> list[GeneratedImage]:
    output_dir.mkdir(parents=True, exist_ok=True)

    # Remove old generated factory images only; keep other site images untouched.
    for old in output_dir.glob("factory-*.webp"):
        old.unlink()

    images = list(iter_image_files(raw_dir))[:limit]
    if not images:
        raise RuntimeError(f"No image files found in downloaded Drive folder: {raw_dir}")

    generated: list[GeneratedImage] = []
    for idx, source in enumerate(images, start=1):
        output = output_dir / f"factory-{idx:02d}.webp"
        width, height = convert_to_webp(source, output, max_width=max_width, quality=quality)
        generated.append(GeneratedImage(idx, source.name, output, width, height))
        print(f"generated {output} from {source.name} ({width}x{height})")

    manifest = {
        "source": "Google Drive",
        "count": len(generated),
        "images": [
            {
                "index": item.index,
                "source_name": item.source_name,
                "path": str(item.output_path).replace("\\", "/"),
                "width": item.width,
                "height": item.height,
            }
            for item in generated
        ],
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return generated


def web_path(path: Path) -> str:
    return str(path).replace("\\", "/")


def patch_index_html(index_path: Path, generated: list[GeneratedImage]) -> None:
    html = index_path.read_text(encoding="utf-8")
    image_paths = [web_path(item.output_path) for item in generated]
    if not image_paths:
        raise RuntimeError("No generated images to patch into index.html")

    hero = image_paths[0]
    html = re.sub(
        r"url\(['\"]img/equipment_01\.jpg['\"]\)",
        f"url('{hero}')",
        html,
    )

    # Replace all non-logo local img/* references in order, keeping logo untouched.
    # This is intentionally conservative: it only replaces local img/ references,
    # not external tracking pixels or icons.
    image_cycle = image_paths[:]
    cursor = 0

    def next_image() -> str:
        nonlocal cursor
        value = image_cycle[cursor % len(image_cycle)]
        cursor += 1
        return value

    def replace_src(match: re.Match[str]) -> str:
        quote = match.group("quote")
        src = match.group("src")
        if "logo" in src.lower():
            return match.group(0)
        return f"src={quote}{next_image()}{quote}"

    html = re.sub(
        r"src=(?P<quote>['\"])(?P<src>img/(?!logo)[^'\"]+)(?P=quote)",
        replace_src,
        html,
    )

    # Add a small generated marker for maintenance.
    marker = "<!-- Factory images synced automatically from Google Drive. -->"
    if marker not in html:
        html = html.replace("</body>", f"{marker}\n</body>")

    index_path.write_text(html, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder-url", default=os.environ.get("DRIVE_FOLDER_URL", DEFAULT_FOLDER_URL))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("DRIVE_IMAGE_LIMIT", "10")))
    parser.add_argument("--max-width", type=int, default=int(os.environ.get("IMAGE_MAX_WIDTH", "1800")))
    parser.add_argument("--quality", type=int, default=int(os.environ.get("WEBP_QUALITY", "82")))
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    index_path = repo_root / "index.html"
    output_dir = repo_root / "img" / "factory"

    if not index_path.exists():
        raise FileNotFoundError(f"Cannot find {index_path}")

    with tempfile.TemporaryDirectory(prefix="drive-images-") as temp:
        raw_dir = Path(temp) / "raw"
        download_public_drive_folder(args.folder_url, raw_dir)
        generated = generate_images(
            raw_dir=raw_dir,
            output_dir=output_dir,
            limit=args.limit,
            max_width=args.max_width,
            quality=args.quality,
        )
        patch_index_html(index_path, generated)

    print(f"Synced {len(generated)} images into {output_dir}")


if __name__ == "__main__":
    main()
