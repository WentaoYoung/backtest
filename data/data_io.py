"""Safe local data reads (detect Git LFS pointer stubs on deploy)."""

from __future__ import annotations

import gzip
import os
from typing import Any, Optional

import pandas as pd

from data.parquet_io import assert_not_lfs_pointer, is_git_lfs_pointer


def resolve_data_csv_path(data_dir: str, basename: str) -> Optional[str]:
    """
    Resolve CSV under data_dir, preferring uncompressed then .gz.
    Returns None if neither exists.
    """
    plain = os.path.join(data_dir, basename)
    if os.path.isfile(plain) and not is_git_lfs_pointer(plain):
        return plain
    gz = plain + ".gz"
    if os.path.isfile(gz) and not is_git_lfs_pointer(gz):
        return gz
    if os.path.isfile(plain):
        return plain
    if os.path.isfile(gz):
        return gz
    return None


def safe_read_csv(path: str, **kwargs: Any) -> pd.DataFrame:
    assert_not_lfs_pointer(path)
    if path.lower().endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            return pd.read_csv(f, **kwargs)
    return pd.read_csv(path, **kwargs)


def safe_read_data_csv(data_dir: str, basename: str, **kwargs: Any) -> pd.DataFrame:
    path = resolve_data_csv_path(data_dir, basename)
    if not path:
        raise FileNotFoundError(f"Missing data file: {basename} (or {basename}.gz) under {data_dir}")
    return safe_read_csv(path, **kwargs)
