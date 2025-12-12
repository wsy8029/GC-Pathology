"""
Virchow2 embedding script for mammary adenoma/adenocarcinoma slides.

Features:
  - Loads slide list from a parquet with label column.
  - Filters labels to mammary_adenoma / mammary_adenocarcinoma.
  - Extracts tile embeddings with Virchow2 at chosen level (default 1) using GPU.
  - Saves torch file with embeddings + coords + slide metadata needed for MIL heatmaps.
  - Saves a slide thumbnail (same level as embeddings) for overlay.
  - Uploads both PT and PNG to S3 (requires AWS credentials in env).

S3 target prefix: s3://gc-pathology/gv-virchow2-level0-embedding-metadata/

[2024-12 FIX] Coordinate bug fixed:
  - read_region(location, level, size)의 location은 항상 level 0 좌표여야 함
  - 이전 버전은 level 좌표를 넘겨서 슬라이드 일부만 타일링됨
  - 수정: level 0 좌표계에서 iteration하여 전체 슬라이드 커버
"""

from __future__ import annotations

import argparse
import importlib
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path
from contextlib import nullcontext
from typing import Optional

import boto3
import numpy as np
import pandas as pd
import torch
import timm
from PIL import Image
from botocore.exceptions import BotoCoreError, ClientError
from huggingface_hub import login
from timm.data import resolve_data_config
from timm.data.transforms_factory import create_transform
from timm.layers import SwiGLUPacked
from skimage import color, filters


def ensure_openslide():
    """
    Ensure openslide + system libs are present. Attempts apt install if missing.
    """
    try:
        import openslide  # type: ignore

        return openslide
    except ImportError as original_err:
        if not shutil.which("apt-get"):
            raise ImportError(
                "openslide is not installed and apt-get is unavailable; install openslide-python and system libs manually."
            ) from original_err

        print("[info] Installing openslide via apt-get (requires sudo/root)...", file=sys.stderr)
        apt_commands = [
            ["sudo", "apt-get", "update"],
            ["sudo", "apt-get", "install", "-y", "openslide-tools", "libopenslide0", "python3-openslide"],
        ]
        for cmd in apt_commands:
            result = subprocess.run(cmd, check=False)
            if result.returncode != 0:
                raise ImportError(
                    f"Command {' '.join(cmd)} failed with code {result.returncode}; install openslide manually."
                ) from original_err

        try:
            return importlib.import_module("openslide")
        except Exception as retry_err:  # pragma: no cover - import failure after install
            raise ImportError("openslide import failed even after apt install.") from retry_err


openslide = ensure_openslide()


def parse_s3_prefix(s3_prefix: str) -> tuple[str, str]:
    if not s3_prefix.startswith("s3://"):
        raise ValueError("s3_prefix must start with s3://")
    _, remainder = s3_prefix.split("s3://", 1)
    bucket, *prefix_parts = remainder.split("/", 1)
    key_prefix = prefix_parts[0] if prefix_parts else ""
    return bucket, key_prefix.strip("/")


def list_completed_stems(s3_client, s3_prefix: str) -> set[str]:
    bucket, key_prefix = parse_s3_prefix(s3_prefix)
    kwargs = {"Bucket": bucket}
    if key_prefix:
        kwargs["Prefix"] = key_prefix + "/"
    stems = set()
    while True:
        resp = s3_client.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            fname = key.rsplit("/", 1)[-1]
            if fname.endswith("_virchow2.pt"):
                stems.add(fname.replace("_virchow2.pt", ""))
        if resp.get("IsTruncated"):
            kwargs["ContinuationToken"] = resp["NextContinuationToken"]
        else:
            break
    return stems


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Virchow2 embedding + thumbnail + S3 upload",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--parquet",
        type=Path,
        required=True,
        help="Parquet with columns FOLDER, FILE_NAME, label (mammary_adenoma / mammary_adenocarcinoma)",
    )
    p.add_argument(
        "--volume-root",
        type=Path,
        default=Path("/Volumes/Expansion"),
        help="Mounted volume root that contains the slide directory tree (e.g., external drive mount point)",
    )
    p.add_argument(
        "--slide-root",
        type=Path,
        required=True,
        help="Path containing slide folders. If relative, it is resolved under --volume-root; if absolute, used as-is.",
    )
    p.add_argument("--output-s3", type=str, required=True, help="S3 prefix, e.g., s3://gc-pathology/gv-virchow2-level0-embedding-metadata/")
    p.add_argument("--level", type=int, default=1, help="OpenSlide level to read tiles")
    p.add_argument("--tile-size", type=int, default=224, help="Tile size at chosen level")
    p.add_argument("--batch-size", type=int, default=16, help="Batch size for embedding")
    p.add_argument("--min-tissue-ratio", type=float, default=0.3, help="Tissue filter threshold")
    p.add_argument("--max-slides", type=int, default=None, help="Limit number of slides for a dry-run")
    p.add_argument("--seed", type=int, default=42, help="Shuffle seed for slide order")
    p.add_argument("--hf-token", type=str, default=None, help="Hugging Face token (overrides env/HF config)")
    return p.parse_args()


def load_hf_token(cli_token: Optional[str]) -> Optional[str]:
    if cli_token:
        return cli_token
    for key in ("HF_TOKEN", "HUGGINGFACE_TOKEN", "HUGGINGFACE_HUB_TOKEN"):
        val = os.environ.get(key)
        if val:
            return val
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if not line or line.strip().startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() in ("HF_TOKEN", "HUGGINGFACE_TOKEN", "HUGGINGFACE_HUB_TOKEN"):
                return v.strip()
    return None


def hf_login(cli_token: Optional[str]):
    token = load_hf_token(cli_token)
    if token:
        try:
            login(token=token)
        except Exception as e:  # pragma: no cover - best effort
            print(f"[warn] HF login failed: {e}", file=sys.stderr)


def prepare_model(device: torch.device):
    model = timm.create_model(
        "hf-hub:paige-ai/Virchow2",
        pretrained=True,
        mlp_layer=SwiGLUPacked,
        act_layer=torch.nn.SiLU,
    )
    model.eval().to(device)
    config = resolve_data_config(model.pretrained_cfg, model=model)
    transform = create_transform(**config, is_training=False)
    return model, transform


def tissue_ratio(np_rgb: np.ndarray) -> float:
    hsv = color.rgb2hsv(np_rgb)
    sat = hsv[:, :, 1]
    thresh = filters.threshold_otsu(sat)
    return float((sat > thresh).mean())


def slide_paths_from_parquet(parquet: Path, slide_root: Path, max_slides: Optional[int], seed: int) -> list[dict]:
    df = pd.read_parquet(parquet)
    label_map = {"mammary_adenoma": 0, "mammary_adenocarcinoma": 1}
    df = df.assign(label_norm=df["label"].astype(str).str.lower().str.strip())
    df = df[df["label_norm"].isin(label_map.keys())].copy()
    df["label_id"] = df["label_norm"].map(label_map)
    records = df.to_dict("records")
    random.Random(seed).shuffle(records)
    slides = []
    for rec in records:
        folder = str(rec.get("FOLDER", "")).strip()
        file_names = [fn.strip() for fn in str(rec.get("FILE_NAME", "")).split("|") if fn.strip()]
        if not file_names:
            continue
        for fn in file_names:
            fn = fn if fn.lower().endswith(".svs") else f"{fn}.svs"
            svs_path = slide_root / folder / fn if folder else slide_root / fn
            if svs_path.exists():
                slides.append(
                    {
                        "path": svs_path,
                        "label": rec["label_norm"],
                        "label_id": rec["label_id"],
                        "slide_id": rec.get("INSP_RQST_NO", rec.get("INSP_RQST_NUM", svs_path.stem)),
                    }
                )
                break
        if max_slides and len(slides) >= max_slides:
            break
    return slides


def create_thumbnail(slide: openslide.OpenSlide, level: int, max_edge: int = 2048) -> Image.Image:
    w, h = slide.level_dimensions[level]
    img = slide.read_region((0, 0), level, (w, h)).convert("RGB")
    scale = min(1.0, max_edge / max(w, h)) if max(w, h) > 0 else 1.0
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), resample=Image.BILINEAR)
    return img


def embed_slide(
    svs_path: Path,
    model,
    transform,
    device: torch.device,
    level: int,
    tile_size: int,
    min_tissue_ratio: float,
    batch_size: int,
) -> dict:
    """
    슬라이드에서 타일을 추출하고 Virchow2 임베딩을 생성합니다.
    
    [CRITICAL] OpenSlide read_region API:
        read_region(location, level, size)
        - location: 항상 level 0 좌표 (픽셀 단위)
        - level: 읽을 피라미드 레벨
        - size: 해당 level에서의 타일 크기
    
    따라서 level 0 좌표계에서 iteration해야 전체 슬라이드를 커버합니다.
    """
    slide = openslide.OpenSlide(str(svs_path))
    level0_dim = slide.level_dimensions[0]
    level_dim = slide.level_dimensions[level]
    downsample = slide.level_downsamples[level]

    # level 0 좌표계에서의 타일 step 크기
    # tile_size는 level에서의 픽셀 수, 이를 level 0 기준으로 변환
    tile_size_level0 = int(round(tile_size * downsample))

    coords = []
    feats = []
    
    # level 0 좌표계에서 iteration - 전체 슬라이드 커버
    coords_pool = [
        (x, y) 
        for y in range(0, level0_dim[1], tile_size_level0) 
        for x in range(0, level0_dim[0], tile_size_level0)
    ]
    total_tiles = len(coords_pool)
    print(
        f"[slide] {svs_path.name}: level={level} tile_size={tile_size} downsample={downsample:.3f} "
        f"tile_size_level0={tile_size_level0} total_tiles={total_tiles} "
        f"min_tissue={min_tissue_ratio} batch={batch_size}"
    )
    random.shuffle(coords_pool)

    autocast_ctx = torch.autocast(device_type="cuda", dtype=torch.float16) if device.type == "cuda" else nullcontext()
    batch_imgs = []
    batch_xy = []
    processed_tiles = 0
    kept_tiles = 0
    
    with torch.inference_mode(), autocast_ctx:
        for (x, y) in coords_pool:
            # (x, y)는 level 0 좌표 - read_region에 바로 사용
            region = slide.read_region((x, y), level, (tile_size, tile_size)).convert("RGB")
            arr = np.asarray(region)
            if arr.shape[0] < tile_size or arr.shape[1] < tile_size:
                continue
            if tissue_ratio(arr) < min_tissue_ratio:
                continue
            batch_imgs.append(transform(region))
            batch_xy.append((x, y))  # level 0 좌표 그대로 저장
            kept_tiles += 1
            
            if len(batch_imgs) == batch_size:
                batch = torch.stack(batch_imgs).to(device, non_blocking=True)
                out = model(batch)
                if isinstance(out, (list, tuple)):
                    out = out[0]
                class_token = out[:, 0]
                patch_tokens = out[:, 5:]
                embedding = torch.cat([class_token, patch_tokens.mean(1)], dim=-1)
                feats.append(embedding.detach().cpu())
                # 좌표는 이미 level 0 기준이므로 변환 불필요
                coords.extend([(int(bx), int(by)) for (bx, by) in batch_xy])
                batch_imgs.clear()
                batch_xy.clear()
                processed_tiles += batch_size
                pct = min(100.0, processed_tiles / max(1, total_tiles) * 100.0)
                print(f"[tile] {svs_path.name}: processed~{processed_tiles}/{total_tiles} ({pct:.1f}%) kept={kept_tiles}")
        
        # 남은 배치 처리
        if batch_imgs:
            batch = torch.stack(batch_imgs).to(device, non_blocking=True)
            out = model(batch)
            if isinstance(out, (list, tuple)):
                out = out[0]
            class_token = out[:, 0]
            patch_tokens = out[:, 5:]
            embedding = torch.cat([class_token, patch_tokens.mean(1)], dim=-1)
            feats.append(embedding.detach().cpu())
            coords.extend([(int(bx), int(by)) for (bx, by) in batch_xy])
            processed_tiles += len(batch_imgs)
            pct = min(100.0, processed_tiles / max(1, total_tiles) * 100.0)
            print(f"[tile] {svs_path.name}: processed~{processed_tiles}/{total_tiles} ({pct:.1f}%) kept={kept_tiles}")

    if not coords:
        slide.close()
        raise RuntimeError("No tissue tiles passed the filter")

    feat_tensor = torch.cat(feats, dim=0)
    coord_tensor = torch.tensor(coords, dtype=torch.int32)
    slide.close()
    
    return {
        "embedding": feat_tensor,
        "coords": coord_tensor,
        "meta": {
            "slide": svs_path.name,
            "tile_size": tile_size,
            "tile_size_scaled": tile_size_level0,  # level 0에서의 타일 크기 (히트맵용)
            "level": level,
            "level_downsample": float(downsample),
            "coords_level": 0,  # 좌표가 level 0 기준임을 명시
            "dims_level": {"width": level_dim[0], "height": level_dim[1]},
            "dims_level0": {"width": level0_dim[0], "height": level0_dim[1]},
            "min_tissue_ratio": min_tissue_ratio,
            "batch_size": batch_size,
            "model": "paige-ai/Virchow2",
        },
    }


def upload_s3(local_path: Path, s3_prefix: str, s3_client) -> str:
    # s3_prefix expected like s3://bucket/prefix/
    if not s3_prefix.startswith("s3://"):
        raise ValueError("s3_prefix must start with s3://")
    _, remainder = s3_prefix.split("s3://", 1)
    bucket, *prefix_parts = remainder.split("/", 1)
    key_prefix = prefix_parts[0] if prefix_parts else ""
    key_prefix = key_prefix.strip("/")
    key = f"{key_prefix}/{local_path.name}" if key_prefix else local_path.name
    s3_client.upload_file(str(local_path), bucket, key)
    return f"s3://{bucket}/{key}"


def main():
    args = parse_args()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if device.type != "cuda":
        print("[warn] CUDA not available; this will be slow on CPU", file=sys.stderr)

    hf_login(args.hf_token)
    slide_root_path = args.slide_root if args.slide_root.is_absolute() else args.volume_root / args.slide_root
    print(f"[info] Using slide root: {slide_root_path}", file=sys.stderr)
    print("[step] Preparing model and transforms...", file=sys.stderr)
    model, transform = prepare_model(device)
    print("[step] Loading slide list from parquet...", file=sys.stderr)
    slides = slide_paths_from_parquet(args.parquet, slide_root_path, args.max_slides, args.seed)
    if not slides:
        print("no slides found matching labels", file=sys.stderr)
        sys.exit(1)

    s3 = boto3.client("s3")
    print("[step] Checking already completed slides in S3 for resume...", file=sys.stderr)
    completed_stems = list_completed_stems(s3, args.output_s3)
    print(f"[info] Found {len(completed_stems)} completed items in S3; will skip matching slides.", file=sys.stderr)

    out_dir = Path("virchow2_tmp")
    out_dir.mkdir(exist_ok=True)
    total_slides = len(slides)
    processed = 0
    for idx, rec in enumerate(slides, start=1):
        svs_path: Path = rec["path"]
        stem = svs_path.stem
        if stem in completed_stems:
            processed += 1
            pct = processed / total_slides * 100.0
            print(f"[skip] {idx}/{total_slides} ({pct:.1f}%) {stem} already in S3, skipping.", file=sys.stderr)
            continue

        pct = processed / total_slides * 100.0
        print(
            f"[step] {idx}/{total_slides} ({pct:.1f}%) Starting {stem} | level={args.level} tile={args.tile_size} "
            f"batch={args.batch_size} min_tissue={args.min_tissue_ratio} device={device.type}",
            file=sys.stderr,
        )
        try:
            slide_obj = openslide.OpenSlide(str(svs_path))
            thumb = create_thumbnail(slide_obj, args.level)
            slide_obj.close()
            print(f"[thumb] Created thumbnail for {stem}", file=sys.stderr)
        except Exception as e:
            print(f"[warn] thumbnail failed for {svs_path}: {e}", file=sys.stderr)
            thumb = None

        try:
            print(f"[step] Embedding tiles for {stem}...", file=sys.stderr)
            result = embed_slide(
                svs_path,
                model,
                transform,
                device,
                level=args.level,
                tile_size=args.tile_size,
                min_tissue_ratio=args.min_tissue_ratio,
                batch_size=args.batch_size,
            )
        except Exception as e:
            print(f"[error] embedding failed for {svs_path}: {e}", file=sys.stderr)
            continue

        pt_path = out_dir / f"{stem}_virchow2.pt"
        torch.save(result, pt_path)
        if thumb is not None:
            png_path = out_dir / f"{stem}_thumb.png"
            thumb.save(png_path, format="PNG")
        else:
            png_path = None

        try:
            print(f"[step] Uploading outputs for {stem} to S3...", file=sys.stderr)
            pt_uri = upload_s3(pt_path, args.output_s3, s3)
            print(f"uploaded {pt_uri}")
            if png_path:
                png_uri = upload_s3(png_path, args.output_s3, s3)
                print(f"uploaded {png_uri}")
        except (BotoCoreError, ClientError) as e:
            print(f"[error] S3 upload failed for {svs_path}: {e}", file=sys.stderr)

        # cleanup local temp
        try:
            pt_path.unlink(missing_ok=True)
            if png_path:
                png_path.unlink(missing_ok=True)
        except Exception:
            pass
        processed += 1
        pct_done = processed / total_slides * 100.0
        print(f"[done] {processed}/{total_slides} ({pct_done:.1f}%) completed {stem}.", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    main()
