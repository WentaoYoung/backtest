"""Safe local data reads (detect Git LFS pointer stubs on deploy)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from data.parquet_io import assert_not_lfs_pointer


def safe_read_csv(path: str, **kwargs: Any) -> pd.DataFrame:
    assert_not_lfs_pointer(path)
    return pd.read_csv(path, **kwargs)
