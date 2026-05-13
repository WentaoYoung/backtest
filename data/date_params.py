"""日期参数规范化，供因子仓储与 Web 层共用。"""

from __future__ import annotations

from typing import Optional


def coerce_yyyy_mm_dd(value: Optional[str]) -> Optional[str]:
    """
    将输入规范为 YYYY-MM-DD；空串或无效则返回 None。
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("/", "-")
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def both_date_bounds_blank(start_date: Optional[str], end_date: Optional[str]) -> bool:
    """两端日期均为空时返回 True（表示不裁剪日期区间）。"""
    return not (str(start_date or "").strip() or str(end_date or "").strip())
