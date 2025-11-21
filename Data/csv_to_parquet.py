"""
CSV 파일을 선택적으로 Parquet 포맷으로 변환하는 스크립트.

사용 예시:
    python csv_to_parquet.py Data/조직검사_결과.csv
    python csv_to_parquet.py --encoding euc-kr Data/sample.csv

기본적으로 UTF-8 인코딩을 사용하며, parquet 파일 경로를 지정하지 않으면
`.csv` 확장자를 `.parquet`로 교체하여 저장합니다.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def csv_to_parquet(csv_path: str, parquet_path: str | None = None, *, encoding: str = "utf-8") -> Path:
    """CSV 파일을 Parquet 포맷으로 변환한다.

    Args:
        csv_path: 변환할 CSV 파일 경로.
        parquet_path: 출력할 Parquet 경로. 지정하지 않으면 입력 파일명에서
            확장자를 `.parquet`로 교체한다.
        encoding: CSV 파일을 읽을 때 사용할 인코딩.

    Returns:
        생성된 Parquet 파일의 Path 객체.
    """

    csv_file = Path(csv_path)
    if parquet_path is None:
        parquet_file = csv_file.with_suffix(".parquet")
    else:
        parquet_file = Path(parquet_path)

    print(f"Reading {csv_file} (encoding={encoding})...")
    df = pd.read_csv(csv_file, encoding=encoding)

    print(f"Converting to {parquet_file}...")
    df.to_parquet(parquet_file, compression="snappy", index=False)

    csv_size_mb = csv_file.stat().st_size / (1024**2)
    pq_size_mb = parquet_file.stat().st_size / (1024**2)

    saved_mb = csv_size_mb - pq_size_mb
    saved_pct = (1 - pq_size_mb / csv_size_mb) * 100 if csv_size_mb else 0

    print("✓ Done!")
    print(f"  CSV: {csv_size_mb:.1f} MB")
    print(f"  Parquet: {pq_size_mb:.1f} MB")
    print(f"  Saved: {saved_mb:.1f} MB ({saved_pct:.1f}%)")

    return parquet_file


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert a CSV file to Parquet format.")
    parser.add_argument("csv_path", type=str, help="Path to the input CSV file")
    parser.add_argument(
        "parquet_path",
        nargs="?",
        default=None,
        help="Optional output path for the Parquet file (defaults to replacing .csv with .parquet)",
    )
    parser.add_argument(
        "--encoding",
        default="utf-8",
        help="Encoding used to read the CSV file (default: utf-8)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    csv_to_parquet(args.csv_path, args.parquet_path, encoding=args.encoding)
