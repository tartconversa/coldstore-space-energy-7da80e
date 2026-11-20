"""区间与时间轴通用工具。所有区间左闭右开，时刻为带时区 datetime。"""
from __future__ import annotations

from datetime import datetime, timedelta

HOURS = timedelta(hours=1)


def overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime):
    """返回两区间交集 (start, end)，无交集返回 None。"""
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    if start < end:
        return start, end
    return None


def clip(interval, window):
    """把 (start, end) 裁剪到 window 内。"""
    start, end = interval
    w_start, w_end = window
    out = overlap(start, end, w_start, w_end)
    return out


def hours_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600.0


def merge_timeline(*point_lists) -> list[datetime]:
    """合并若干时点序列，去重并排序，得到时间轴。"""
    points = set()
    for lst in point_lists:
        points.update(lst)
    return sorted(points)


def fraction_split(value: float, seg_start: datetime, seg_end: datetime,
                   cut_start: datetime, cut_end: datetime) -> float:
    """按小时比例把区间总量拆分到子区间；仅在无法直接证明时使用，调用方须标记 estimated。"""
    total_h = hours_between(seg_start, seg_end)
    part_h = hours_between(cut_start, cut_end)
    if total_h <= 0:
        return 0.0
    return value * part_h / total_h
