"""查询层：按房屋、按片区×月份、按广告、按版本读取已固化的事实。

所有查询默认读最新版本，可显式指定 version_id 复查旧版本；
旧版本行在构建中从不被修改，复查结果逐字节稳定。
"""

import json
from collections import defaultdict

from .build import latest_version
from .timeutil import parse_ts


def _rows(con, sql, params=()):
    cursor = con.execute(sql, params)
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _resolve_version(con, version_id):
    if version_id is not None:
        return version_id
    latest = latest_version(con)
    if latest is None:
        raise ValueError("湖区还没有任何版本，请先 build")
    return latest["version_id"]


def list_versions(con):
    rows = _rows(con, "SELECT version_id, parent_id, created_at, input_hash, note FROM dim_version ORDER BY version_id")
    for r in rows:
        r["created_at"] = _iso(r["created_at"])
    return rows


def version_changes(con, version_id):
    rows = _rows(
        con, "SELECT category, subject, detail FROM version_change WHERE version_id = ? ORDER BY category, subject",
        [version_id],
    )
    grouped = defaultdict(list)
    for r in rows:
        grouped[r["category"]].append({"subject": r["subject"], "detail": json.loads(r["detail"])})
    return {"version_id": version_id, "changes": dict(grouped)}


def notes(con, version_id=None):
    version_id = _resolve_version(con, version_id)
    rows = _rows(con, "SELECT category, subject, detail FROM fact_note WHERE version_id = ? ORDER BY category, subject", [version_id])
    for r in rows:
        r["detail"] = json.loads(r["detail"])
    return {"version_id": version_id, "notes": rows}


def house_experience(con, house_id, version_id=None):
    """房屋级经历：挂牌段、未知段、渠道滞留及其来源，以及交易限制窗口。"""
    version_id = _resolve_version(con, version_id)
    segments = _rows(
        con,
        "SELECT kind, start_day, end_day, days, sources FROM fact_house_segment "
        "WHERE version_id = ? AND house_id = ? ORDER BY start_day, kind",
        [version_id, house_id],
    )
    for s in segments:
        s["start_day"] = _iso(s["start_day"])
        s["end_day"] = _iso(s["end_day"])
        s["sources"] = json.loads(s["sources"])

    day_rows = _rows(
        con,
        "SELECT day, listed_tradable, residual, unknown, delisted, sources FROM fact_house_day "
        "WHERE version_id = ? AND house_id = ? ORDER BY day",
        [version_id, house_id],
    )
    totals = {"listed_tradable_days": 0, "residual_days": 0, "unknown_days": 0, "delisted_days": 0}
    for d in day_rows:
        for flag in ("listed_tradable", "residual", "unknown", "delisted"):
            if d[flag]:
                totals[f"{flag}_days"] += 1

    restrictions = _rows(
        con,
        "SELECT transaction_id, kind, start_at, end_at, lifted_by FROM fact_house_restriction "
        "WHERE version_id = ? AND house_id = ? ORDER BY start_at",
        [version_id, house_id],
    )
    for r in restrictions:
        r["start_at"] = _iso(r["start_at"])
        r["end_at"] = _iso(r["end_at"])

    linked = _rows(
        con,
        "SELECT DISTINCT channel, listing_id, reuse_epoch FROM fact_listing_attribution "
        "WHERE version_id = ? AND house_id = ? AND status = 'ATTRIBUTED' ORDER BY 1, 2, 3",
        [version_id, house_id],
    )
    ambiguous = _rows(
        con,
        "SELECT DISTINCT channel, listing_id, reuse_epoch, candidates FROM fact_listing_attribution "
        "WHERE version_id = ? AND status = 'AMBIGUOUS' ORDER BY 1, 2, 3",
        [version_id],
    )
    ambiguous = [a for a in ambiguous if house_id in json.loads(a["candidates"])]
    for a in ambiguous:
        a["candidates"] = json.loads(a["candidates"])

    return {
        "version_id": version_id,
        "house_id": house_id,
        "segments": segments,
        "totals": totals,
        "restrictions": restrictions,
        "linked_ads": linked,
        "ambiguous_ads": ambiguous,
    }


def region_month(con, region, month, version_id=None):
    """片区×月份：房屋挂牌天数（跨渠道互不重计）与各渠道曝光天数。"""
    version_id = _resolve_version(con, version_id)
    houses = _rows(
        con,
        "SELECT house_id, listing_days, residual_days, unknown_days, delisted_days "
        "FROM agg_region_month_house WHERE version_id = ? AND region = ? AND month = ? ORDER BY house_id",
        [version_id, region, month],
    )
    channels = _rows(
        con,
        "SELECT channel, house_exposure_days, ad_exposure_days, residual_days, "
        "ambiguous_ad_exposure_days, houses FROM agg_region_month_channel "
        "WHERE version_id = ? AND region = ? AND month = ? ORDER BY channel",
        [version_id, region, month],
    )
    totals = {
        "houses": len(houses),
        "listing_days": sum(h["listing_days"] for h in houses),
        "residual_days": sum(h["residual_days"] for h in houses),
        "unknown_days": sum(h["unknown_days"] for h in houses),
    }
    return {
        "version_id": version_id,
        "region": region,
        "month": month,
        "houses": houses,
        "channels": channels,
        "totals": totals,
    }


def ad_detail(con, channel, listing_id, reuse_epoch, version_id=None, at=None):
    """广告级资料：观察流水、字段状态（区分未提供与清空）、段与归属。

    身份有歧义的广告在此仍然完整可查；at 可指定字段状态的回看时刻。
    """
    version_id = _resolve_version(con, version_id)
    key = [version_id, channel, listing_id, reuse_epoch]

    observations = _rows(
        con,
        "SELECT record_id, batch, page, kind, observed_at_raw, offset_seconds, observed_at_adj, fields "
        "FROM fact_observation WHERE version_id = ? AND channel = ? AND listing_id = ? AND reuse_epoch = ? "
        "ORDER BY observed_at_adj, record_id",
        key,
    )
    for o in observations:
        o["observed_at_raw"] = _iso(o["observed_at_raw"])
        o["observed_at_adj"] = _iso(o["observed_at_adj"])
        o["fields"] = json.loads(o["fields"])

    events = _rows(
        con,
        "SELECT field, op, value, effective_at, record_id FROM fact_ad_field_event "
        "WHERE version_id = ? AND channel = ? AND listing_id = ? AND reuse_epoch = ? "
        "ORDER BY effective_at, record_id",
        key,
    )
    field_state = {}
    at_ts = parse_ts(at) if at is not None else None
    for e in events:
        if at_ts is not None and e["effective_at"] > at_ts:
            continue
        value = json.loads(e["value"]) if e["value"] is not None else None
        field_state[e["field"]] = {"status": "SET" if e["op"] == "SET" else "CLEARED", "value": value}
        e["effective_at"] = _iso(e["effective_at"])
        e["value"] = value

    segments = _rows(
        con,
        "SELECT state, start_day, end_day, days FROM fact_listing_segment "
        "WHERE version_id = ? AND channel = ? AND listing_id = ? AND reuse_epoch = ? ORDER BY start_day",
        key,
    )
    for s in segments:
        s["start_day"] = _iso(s["start_day"])
        s["end_day"] = _iso(s["end_day"])

    attribution = _rows(
        con,
        "SELECT day, status, house_id, candidates FROM fact_listing_attribution "
        "WHERE version_id = ? AND channel = ? AND listing_id = ? AND reuse_epoch = ? ORDER BY day",
        key,
    )
    for a in attribution:
        a["day"] = _iso(a["day"])
        a["candidates"] = json.loads(a["candidates"])

    exposure = _rows(
        con,
        "SELECT day, region, attribution_status, house_id, tradable, residual FROM fact_exposure_day "
        "WHERE version_id = ? AND channel = ? AND listing_id = ? AND reuse_epoch = ? ORDER BY day",
        key,
    )
    for e in exposure:
        e["day"] = _iso(e["day"])

    return {
        "version_id": version_id,
        "channel": channel,
        "listing_id": listing_id,
        "reuse_epoch": reuse_epoch,
        "observations": observations,
        "field_state": field_state,
        "field_events": events,
        "segments": segments,
        "attribution": attribution,
        "exposure_days": exposure,
    }
