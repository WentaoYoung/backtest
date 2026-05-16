"""
Parquet helpers for factors_all: single file or GitHub-safe shards (factors_all_partNNN.parquet).
"""

from __future__ import annotations

import glob
import os
import re
from typing import List, Optional, Tuple

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

_SHARD_RE = re.compile(r"^factors_all_part(\d+)\.parquet$", re.IGNORECASE)
_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"
# Stay under GitHub's 100 MB blob limit (use 95 MB target when splitting).
DEFAULT_MAX_SHARD_BYTES = 95 * 1024 * 1024

_LFS_DEPLOY_HINT = (
    "Git LFS pointer file detected (not real data). "
    "On Railway, ensure nixpacks.toml runs `git lfs pull` during build, "
    "or commit this file as a regular git blob (not LFS)."
)


def is_git_lfs_pointer(path: str) -> bool:
    """True if path is a Git LFS stub instead of the actual binary."""
    try:
        with open(path, "rb") as f:
            head = f.read(128)
    except OSError:
        return False
    return head.startswith(_LFS_POINTER_PREFIX)


def assert_not_lfs_pointer(path: str) -> None:
    if is_git_lfs_pointer(path):
        raise RuntimeError(f"{_LFS_DEPLOY_HINT}\nFile: {path}")


def factors_all_single_path(parquet_dir: str) -> str:
    return os.path.join(parquet_dir, "factors_all.parquet")


def list_factors_all_shards(parquet_dir: str) -> List[str]:
    """Sorted shard paths: factors_all_part001.parquet, ..."""
    pattern = os.path.join(parquet_dir, "factors_all_part*.parquet")
    paths = [p for p in glob.glob(pattern) if _SHARD_RE.match(os.path.basename(p))]
    return sorted(paths, key=lambda p: int(_SHARD_RE.match(os.path.basename(p)).group(1)))


def has_factors_all_parquet(parquet_dir: str) -> bool:
    if os.path.isfile(factors_all_single_path(parquet_dir)):
        return True
    return bool(list_factors_all_shards(parquet_dir))


def resolve_factors_all_paths(parquet_dir: str) -> Optional[List[str]]:
    """
    Resolve readable factors_all source(s).
    Prefer shards over legacy single file when both exist.
    """
    shards = list_factors_all_shards(parquet_dir)
    if shards:
        return shards
    single = factors_all_single_path(parquet_dir)
    if os.path.isfile(single):
        return [single]
    return None


def shard_cache_mtime(paths: List[str]) -> float:
    mt = -1.0
    for p in paths:
        try:
            mt = max(mt, os.path.getmtime(p))
        except OSError:
            pass
    return mt


def read_factors_all_table(paths: List[str]) -> pa.Table:
    for p in paths:
        assert_not_lfs_pointer(p)
    if len(paths) == 1:
        return pq.read_table(paths[0])
    return pa.concat_tables([pq.read_table(p) for p in paths], promote_options="default")


def read_factors_all_dataframe(paths: List[str]) -> pd.DataFrame:
    return read_factors_all_table(paths).to_pandas()


def split_factors_all_parquet(
    src_path: str,
    out_dir: str,
    *,
    max_shard_bytes: int = DEFAULT_MAX_SHARD_BYTES,
    remove_source: bool = False,
) -> List[str]:
    """
    Split a large factors_all.parquet into factors_all_partNNN.parquet shards.
    Uses row-group boundaries to keep each shard under max_shard_bytes.
    """
    os.makedirs(out_dir, exist_ok=True)
    for old in list_factors_all_shards(out_dir):
        os.remove(old)

    written: List[str] = []
    with pq.ParquetFile(src_path) as pf:
        n_rg = pf.metadata.num_row_groups
        part_idx = 0
        batch_rgs: List[int] = []
        batch_bytes = 0

        def flush_batch(rg_indices: List[int]) -> None:
            nonlocal part_idx, written
            if not rg_indices:
                return
            part_idx += 1
            out_path = os.path.join(out_dir, f"factors_all_part{part_idx:03d}.parquet")
            tables = [pf.read_row_group(i) for i in rg_indices]
            combined = pa.concat_tables(tables, promote_options="default")
            pq.write_table(combined, out_path, compression="snappy")
            written.append(out_path)
            size_mb = os.path.getsize(out_path) / (1024 * 1024)
            print(
                f"[parquet_io] wrote {os.path.basename(out_path)} "
                f"({size_mb:.1f} MB, {combined.num_rows} rows)"
            )

        for i in range(n_rg):
            rg = pf.metadata.row_group(i)
            rg_bytes = rg.total_byte_size or 0
            if batch_rgs and batch_bytes + rg_bytes > max_shard_bytes:
                flush_batch(batch_rgs)
                batch_rgs = []
                batch_bytes = 0
            batch_rgs.append(i)
            batch_bytes += rg_bytes

        flush_batch(batch_rgs)

    if remove_source and written:
        os.remove(src_path)
        print(f"[parquet_io] removed source: {src_path}")

    return written


def ensure_factors_all_sharded(parquet_dir: str, *, max_shard_bytes: int = DEFAULT_MAX_SHARD_BYTES) -> bool:
    """If only legacy single file exists and is large, split into shards."""
    single = factors_all_single_path(parquet_dir)
    if not os.path.isfile(single):
        return bool(list_factors_all_shards(parquet_dir))
    if list_factors_all_shards(parquet_dir):
        return True
    if os.path.getsize(single) <= max_shard_bytes:
        return True
    split_factors_all_parquet(single, parquet_dir, max_shard_bytes=max_shard_bytes, remove_source=True)
    return bool(list_factors_all_shards(parquet_dir))


if __name__ == "__main__":
    import argparse
    import sys

    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)

    parser = argparse.ArgumentParser(description="Split factors_all.parquet into GitHub-safe shards.")
    parser.add_argument(
        "src",
        nargs="?",
        default=os.path.join(_root, "data", "parquet_loaded", "factors_all.parquet"),
    )
    parser.add_argument(
        "--out-dir",
        default=os.path.join(_root, "data", "parquet_loaded"),
    )
    parser.add_argument("--max-mb", type=float, default=95.0)
    parser.add_argument("--keep-source", action="store_true")
    args = parser.parse_args()
    if not os.path.isfile(args.src):
        print(f"Source not found: {args.src}")
        sys.exit(1)
    split_factors_all_parquet(
        args.src,
        args.out_dir,
        max_shard_bytes=int(args.max_mb * 1024 * 1024),
        remove_source=not args.keep_source,
    )
