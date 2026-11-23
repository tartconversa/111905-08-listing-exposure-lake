"""时间工具：来源时间为带时区 ISO8601，日期区间左闭右开。

“天”的口径固定为湖区规范时区（默认 Asia/Shanghai，与来源 +08:00 一致），
所有按天计数（挂牌天数、曝光天数）都先归一到该时区的日历日。
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

CANONICAL_TZ = ZoneInfo("Asia/Shanghai")


def parse_ts(value):
    """解析带时区的 ISO8601 时间戳；缺时区视为违反契约。"""
    if not isinstance(value, str):
        raise ValueError(f"时间字段必须是字符串：{value!r}")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"时间缺少时区：{value!r}")
    return dt


def parse_date(value):
    if not isinstance(value, str):
        raise ValueError(f"日期字段必须是字符串：{value!r}")
    return date.fromisoformat(value)


def parse_date_or_ts(value):
    """identity.received 等字段可能是日期或带时区时间戳。"""
    try:
        return parse_date(value)
    except ValueError:
        return parse_ts(value)


def day_of(dt):
    """时间戳归一到规范时区的日历日。"""
    return dt.astimezone(CANONICAL_TZ).date()


def month_of(day):
    return f"{day.year:04d}-{day.month:02d}"


def iter_days(start, end_inclusive):
    d = start
    while d <= end_inclusive:
        yield d
        d += timedelta(days=1)


def collapse_segments(day_states):
    """把 [(day, state, payload)] 按连续相同 state 折叠为左闭右开段。

    day_states 必须按天排序。只有“状态相同且日期紧接上一段末尾”才并入当前段，
    不连续的同状态天会另起新段（未知段不能把两段观察强行拼成持续挂牌）。
    返回 [{"state":..., "start_day":date, "end_day":date(开), "days":int, "payloads":[...]}]，
    payloads 为该段内每天的 payload（用于汇总来源）。
    """
    segments = []
    for day, state, payload in day_states:
        if segments and segments[-1]["state"] == state and segments[-1]["end_day"] == day:
            seg = segments[-1]
            seg["end_day"] = day + timedelta(days=1)
            seg["days"] += 1
            seg["payloads"].append(payload)
        else:
            segments.append({
                "state": state,
                "start_day": day,
                "end_day": day + timedelta(days=1),
                "days": 1,
                "payloads": [payload],
            })
    return segments
