#!/usr/bin/env python3
"""
TITAN patch/slide 임베딩을 생성해 S3로 업로드하는 스크립트.

주요 동작
- 지정한 WSI(기본: level1 256px 타일)를 타일링하며 백그라운드 필터링.
- CONCH v1.5로 patch feature 추출 → TITAN slide embedding 계산.
- `<slide_id>_titan.pt`에 features/coords/patch_size_level0/slide_embedding/meta 저장.
- 기본 업로드 대상: s3://gc-pathology/gv-titan-level0-embedding-metadata/

예시
  python run_titan_level0.py \\
    --input-dir /workspace/data/raw \\
    --output-dir /workspace/PoC/v2/output_titan \\
    --s3-prefix s3://gc-pathology/gv-titan-level0-embedding-metadata/

사전 준비
- Hugging Face 로그인(`huggingface_hub.login()`)으로 TITAN/CONCH 접근 권한 확보.
- AWS 자격 증명은 환경변수(AWS_*), 프로파일(--aws-profile) 또는 EC2 메타데이터 등으로 제공.
"""

import argparse
import glob
import logging
import os
import sys
import time
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

import numpy as np
import openslide
import torch
import torch.nn.functional as F
from PIL import Image
from transformers import AutoModel


DEFAULT_S3_PREFIX = "s3://gc-pathology/gv-titan-level0-embedding-metadata/"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "output_titan"
SUPPORTED_EXTS = (".svs", ".tif", ".tiff", ".ndpi")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract TITAN embeddings and upload to S3.")
    input_group = parser.add_mutually_exclusive_group(required=False)
    input_group.add_argument(
        "--slides",
        nargs="+",
        help="Slide paths or glob patterns (space separated).",
    )
    input_group.add_argument(
        "--input-dir",
        type=Path,
        help="Directory containing slides; all supported extensions are processed.",
    )
    input_group.add_argument(
        "--list-file",
        type=Path,
        help="Text file with one slide path per line.",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Local output directory.")
    parser.add_argument(
        "--s3-prefix",
        default=DEFAULT_S3_PREFIX,
        help="S3 prefix to upload .pt files (e.g., s3://bucket/prefix/).",
    )
    parser.add_argument("--level", type=int, default=1, help="OpenSlide level to read tiles from.")
    parser.add_argument("--patch-size", type=int, default=256, help="Tile size (pixels) at the chosen level.")
    parser.add_argument("--batch-size", type=int, default=32, help="Batch size for CONCH inference.")
    parser.add_argument(
        "--bg-white-ratio",
        type=float,
        default=0.80,
        help="Skip tiles whose mean white pixel ratio is above this threshold.",
    )
    parser.add_argument("--max-tiles", type=int, help="Optional cap on number of kept tiles per slide.")
    parser.add_argument("--skip-upload", action="store_true", help="Do not upload to S3.")
    parser.add_argument("--aws-profile", help="AWS profile name for boto3 session.")
    parser.add_argument("--aws-region", help="AWS region for boto3 session.")
    parser.add_argument("--aws-endpoint-url", help="Custom endpoint URL (e.g., for MinIO).")
    parser.add_argument("--log-every", type=int, default=200, help="Log progress every N kept tiles.")
    parser.add_argument(
        "--save-h5",
        action="store_true",
        help="Also write <slide_id>_features.h5 alongside the .pt payload.",
    )
    return parser.parse_args()


def resolve_slides(args: argparse.Namespace) -> List[Path]:
    candidates: List[Path] = []
    if args.slides:
        for item in args.slides:
            if any(ch in item for ch in "*?[]"):
                matches = [Path(p) for p in glob.glob(os.path.expanduser(item))]
                if not matches:
                    logging.warning("Pattern matched no files: %s", item)
                candidates.extend(matches)
            else:
                candidates.append(Path(item))
    if args.input_dir:
        for ext in SUPPORTED_EXTS:
            candidates.extend(sorted(Path(args.input_dir).glob(f"*{ext}")))
    if args.list_file:
        for line in args.list_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            candidates.append(Path(line))

    slides = []
    seen = set()
    for p in candidates:
        p = p.expanduser()
        if not p.suffix.lower() in SUPPORTED_EXTS:
            continue
        try:
            resolved = p.resolve()
        except FileNotFoundError:
            resolved = p
        if resolved in seen:
            continue
        slides.append(resolved)
        seen.add(resolved)
    return slides


def is_background(pil_img: Image.Image, white_ratio: float) -> bool:
    arr = np.asarray(pil_img).astype(np.uint8)
    if arr.ndim == 3 and arr.shape[2] == 4:
        arr = arr[:, :, :3]
    white = (arr > 220).mean()
    return float(white) > white_ratio


def load_models(device: torch.device):
    logging.info("Loading TITAN + CONCH v1.5 from Hugging Face...")
    titan = AutoModel.from_pretrained("MahmoodLab/TITAN", trust_remote_code=True)
    conch, eval_transform = titan.return_conch()
    titan = titan.to(device).eval()
    conch = conch.to(device).eval()
    logging.info("Models ready on %s", device)
    return titan, conch, eval_transform


def compute_grid(slide: openslide.OpenSlide, level: int, patch_size_lv: int) -> Tuple[List[Tuple[int, int]], int, int]:
    if level >= slide.level_count:
        raise ValueError(f"Slide has only {slide.level_count} levels; requested level={level}")
    level0_w, level0_h = slide.dimensions
    downsample = slide.level_downsamples[level]
    stride_lv0 = int(round(patch_size_lv * float(downsample)))
    xs = range(0, level0_w - stride_lv0 + 1, stride_lv0)
    ys = range(0, level0_h - stride_lv0 + 1, stride_lv0)
    coords_lv0 = [(x, y) for y in ys for x in xs]
    patch_size_lv0 = stride_lv0
    return coords_lv0, patch_size_lv0, level0_w * level0_h


def conch_batch(
    conch_model,
    eval_transform,
    images: Sequence[Image.Image],
    device: torch.device,
) -> torch.Tensor:
    tensors = [eval_transform(img) for img in images]
    batch = torch.stack(tensors).to(device, non_blocking=True)
    with torch.no_grad(), torch.autocast(device.type, torch.float16):
        feats = conch_model(batch)
    if isinstance(feats, (list, tuple)):
        feats = feats[0]
    return feats.detach().cpu()


def save_h5(h5_path: Path, features: torch.Tensor, coords: torch.Tensor, patch_size_lv0: int) -> None:
    import h5py

    with h5py.File(h5_path, "w") as f:
        f.create_dataset("features", data=features.numpy(), compression="gzip")
        f.create_dataset("coords", data=coords.numpy(), compression="gzip")
        f["coords"].attrs["patch_size_level0"] = patch_size_lv0


def process_slide(
    slide_path: Path,
    args: argparse.Namespace,
    titan,
    conch,
    eval_transform,
    device: torch.device,
) -> Tuple[Path, Path | None]:
    start = time.time()
    slide = openslide.OpenSlide(str(slide_path))
    try:
        coords_lv0, patch_size_lv0, area_lv0 = compute_grid(slide, args.level, args.patch_size)
        logging.info(
            "Processing %s | level%d tiles=%d patch_size_lv0=%d area=%.2f MP",
            slide_path.name,
            args.level,
            len(coords_lv0),
            patch_size_lv0,
            area_lv0 / 1e6,
        )

        kept_coords: List[Tuple[int, int]] = []
        feats_list: List[torch.Tensor] = []
        batch_imgs: List[Image.Image] = []
        batch_coords: List[Tuple[int, int]] = []

        for (x0, y0) in coords_lv0:
            if args.max_tiles is not None and len(kept_coords) >= args.max_tiles:
                break
            tile = slide.read_region((x0, y0), args.level, (args.patch_size, args.patch_size)).convert("RGB")
            if is_background(tile, args.bg_white_ratio):
                continue
            batch_imgs.append(tile)
            batch_coords.append((x0, y0))
            if len(batch_imgs) == args.batch_size:
                feats_list.append(conch_batch(conch, eval_transform, batch_imgs, device))
                kept_coords.extend(batch_coords)
                batch_imgs, batch_coords = [], []
                if len(kept_coords) % args.log_every == 0:
                    logging.info("  kept %d tiles", len(kept_coords))

        if batch_imgs and (args.max_tiles is None or len(kept_coords) < args.max_tiles):
            feats_list.append(conch_batch(conch, eval_transform, batch_imgs, device))
            kept_coords.extend(batch_coords)

        if not kept_coords:
            raise RuntimeError(f"No tiles kept after background filtering: {slide_path}")

        features = torch.cat(feats_list, dim=0)
        if args.max_tiles is not None and len(kept_coords) > args.max_tiles:
            kept_coords = kept_coords[: args.max_tiles]
            features = features[: args.max_tiles]
        coords_tensor = torch.tensor(kept_coords, dtype=torch.int32)
        logging.info("  final tiles: %d | feature dim: %d", features.shape[0], features.shape[1])

        with torch.no_grad(), torch.autocast(device.type, torch.float16):
            slide_emb = titan.encode_slide_from_patch_features(
                features.to(device), coords_tensor.to(device), patch_size_lv0
            ).cpu()

        meta = {
            "slide": slide_path.name,
            "level": args.level,
            "patch_size_level": args.patch_size,
            "patch_size_level0": patch_size_lv0,
            "num_tiles": int(features.shape[0]),
            "bg_white_ratio": args.bg_white_ratio,
            "max_tiles": args.max_tiles,
            "created_at": time.time(),
        }

        args.output_dir.mkdir(parents=True, exist_ok=True)
        slide_id = slide_path.stem
        pt_path = args.output_dir / f"{slide_id}_titan.pt"
        torch.save(
            {
                "features": features,
                "coords": coords_tensor,
                "patch_size_level0": patch_size_lv0,
                "slide_embedding": slide_emb,
                "meta": meta,
            },
            pt_path,
        )

        h5_path = None
        if args.save_h5:
            h5_path = args.output_dir / f"{slide_id}_features.h5"
            save_h5(h5_path, features, coords_tensor, patch_size_lv0)

        elapsed = time.time() - start
        logging.info(
            "  saved %s (%.1f MB) in %.1f min", pt_path.name, pt_path.stat().st_size / (1024 * 1024), elapsed / 60
        )
        return pt_path, h5_path
    finally:
        slide.close()


def parse_s3_uri(uri: str) -> Tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"S3 prefix must start with s3://, got: {uri}")
    no_scheme = uri[5:]
    if "/" in no_scheme:
        bucket, key_prefix = no_scheme.split("/", 1)
    else:
        bucket, key_prefix = no_scheme, ""
    key_prefix = key_prefix.rstrip("/")
    if key_prefix:
        key_prefix += "/"
    return bucket, key_prefix


def upload_to_s3(
    files: Iterable[Path],
    s3_prefix: str,
    profile: str | None,
    region: str | None,
    endpoint_url: str | None,
):
    try:
        import boto3
    except ImportError as exc:
        logging.error("boto3 is required for S3 upload but not installed: %s", exc)
        return

    bucket, key_prefix = parse_s3_uri(s3_prefix)
    session_kwargs = {}
    if profile:
        session_kwargs["profile_name"] = profile
    if region:
        session_kwargs["region_name"] = region
    session = boto3.Session(**session_kwargs)
    s3 = session.resource("s3", endpoint_url=endpoint_url)

    for path in files:
        key = f"{key_prefix}{path.name}"
        logging.info("Uploading %s -> s3://%s/%s", path.name, bucket, key)
        obj = s3.Object(bucket, key)
        obj.upload_file(str(path))


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Image.MAX_IMAGE_PIXELS = None

    slides = resolve_slides(args)
    if not slides:
        logging.error("No slides found. Provide --slides / --input-dir / --list-file.")
        sys.exit(1)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if device.type == "cuda":
        torch.backends.cudnn.benchmark = True
    titan, conch, eval_transform = load_models(device)

    created: List[Path] = []
    for slide_path in slides:
        try:
            pt_path, h5_path = process_slide(slide_path, args, titan, conch, eval_transform, device)
            created.append(pt_path)
            if h5_path:
                created.append(h5_path)
        except Exception as exc:  # pragma: no cover - runtime guardrail
            logging.exception("Failed on %s: %s", slide_path, exc)

    if created and not args.skip_upload:
        upload_to_s3(created, args.s3_prefix, args.aws_profile, args.aws_region, args.aws_endpoint_url)
    elif not args.skip_upload:
        logging.warning("No files to upload.")

    logging.info("Done. Created %d files under %s", len(created), args.output_dir)


if __name__ == "__main__":
    main()
