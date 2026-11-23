"""派生计算：把原始资料编译为经历与曝光事实。

纯函数实现（不触碰数据库），输入为六类原始行，输出为各派生表的行。
核心语义（与 docs/source-contract.md 对齐）：

- 缺席只有在“范围完整”（complete=true 且 expected_pages 全部送达）时才判下架；
  失败页与未覆盖区域只产生 UNKNOWN，绝不当零天。
- 观察/批次时间先按 clock_adjustments 校正再归日到规范时区。
- 广告键 = (channel, listing_id, reuse_epoch)，门店房号复用不会跨 epoch 合并。
- 身份凭证按 valid_from/to（左闭右开）生效；supersedes 整体替换被引用凭证；
  多候选或凭证冲突 => AMBIGUOUS，绝不默认取第一项。
- signed/blocked 关闭可交易期；cancelled/released 仅在引用有据（reference 指向
  同房屋先生效的限制）时从生效时刻恢复，不回填未观察到的空白。
- 成交/限制后仍被观察到的广告 => 渠道滞留曝光（RESIDUAL），供运营跟进。
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .timeutil import (
    collapse_segments,
    day_of,
    iter_days,
    month_of,
    parse_date_or_ts,
    parse_ts,
)

_FAR = datetime(9999, 12, 31, tzinfo=timezone.utc)

LISTED, ABSENT, UNKNOWN = "LISTED", "ABSENT", "UNKNOWN"
ATTRIBUTED, AMBIGUOUS, UNATTRIBUTED = "ATTRIBUTED", "AMBIGUOUS", "UNATTRIBUTED"
RESTRICTION_KINDS = {"signed", "blocked"}
LIFT_KINDS = {"cancelled", "released"}


def _as_of_sort_key(value):
    """received 可能是日期或时间戳，统一为可比较的时间戳。"""
    parsed = parse_date_or_ts(value)
    if hasattr(parsed, "tzinfo"):
        return parsed
    return parse_ts(f"{parsed.isoformat()}T00:00:00+00:00")


# ---------------------------------------------------------------- 时钟校正
def _clock_offsets(raw, notes):
    """(channel, batch) -> (offset_seconds, adjustment_id)。同键后者（按 received）覆盖。"""
    offsets = {}
    adjustments = sorted(
        raw["clock_adjustments"],
        key=lambda a: (_as_of_sort_key(a["received"]), a["adjustment_id"]),
    )
    for adj in adjustments:
        for batch in adj["batches"]:
            key = (adj["channel"], batch)
            if key in offsets:
                notes.append({
                    "category": "clock_overlap",
                    "subject": f"{adj['channel']}/{batch}",
                    "detail": {"kept": adj["adjustment_id"], "dropped": offsets[key][1]},
                })
            offsets[key] = (adj["offset_seconds"], adj["adjustment_id"])
    return offsets


def _adjust(offsets, channel, batch, ts):
    entry = offsets.get((channel, batch))
    if entry is None:
        return ts, 0
    return ts + timedelta(seconds=entry[0]), entry[0]


# ---------------------------------------------------------------- 覆盖
def _coverage(raw, offsets, notes):
    receipts = defaultdict(set)
    for r in raw["page_receipts"]:
        receipts[(r["channel"], r["batch"])].add(r["page"])

    coverage = {}
    duplicates = defaultdict(list)
    for e in raw["exports"]:
        duplicates[(e["channel"], e["batch"])].append(e)
    for key, rows in duplicates.items():
        if len(rows) > 1:
            notes.append({
                "category": "duplicate_export",
                "subject": f"{key[0]}/{key[1]}",
                "detail": {"declarations": len(rows)},
            })
        e = sorted(rows, key=lambda x: parse_ts(x["observed_at"]))[-1]
        raw_ts = parse_ts(e["observed_at"])
        adj_ts, offset = _adjust(offsets, e["channel"], e["batch"], raw_ts)
        expected = sorted(set(e["expected_pages"]))
        received = sorted(receipts.get(key, set()))
        missing = sorted(set(expected) - set(received))
        extra = sorted(set(received) - set(expected))
        if extra:
            notes.append({
                "category": "unexpected_pages",
                "subject": f"{e['channel']}/{e['batch']}",
                "detail": {"pages": extra},
            })
        coverage[key] = {
            "channel": e["channel"],
            "region": e["region"],
            "batch": e["batch"],
            "observed_at_raw": raw_ts,
            "offset_seconds": offset,
            "observed_at_adj": adj_ts,
            "coverage_date": day_of(adj_ts),
            "expected_pages": expected,
            "received_pages": received,
            "missing_pages": missing,
            # 范围完整 = 渠道声明完整 且 所有预期页送达；局部抓取的缺席不构成下架
            "scope_complete": bool(e["complete"]) and not missing,
        }
    return coverage


# ---------------------------------------------------------------- 观察
def _observations(raw, coverage, offsets, notes):
    observations = []
    for o in raw["observations"]:
        exp = coverage.get((o["channel"], o["batch"]))
        if exp is None:
            notes.append({
                "category": "observation_without_export",
                "subject": f"{o['channel']}/{o['batch']}/{o['record_id']}",
                "detail": {"listing_id": o["listing_id"]},
            })
        raw_ts = parse_ts(o["observed_at"])
        adj_ts, offset = _adjust(offsets, o["channel"], o["batch"], raw_ts)
        observations.append({
            "record_id": o["record_id"],
            "channel": o["channel"],
            "listing_id": o["listing_id"],
            "reuse_epoch": o["reuse_epoch"],
            "key": (o["channel"], o["listing_id"], o["reuse_epoch"]),
            "region": exp["region"] if exp else None,
            "batch": o["batch"],
            "page": o["page"],
            "kind": o["kind"],
            "observed_at_raw": raw_ts,
            "offset_seconds": offset,
            "observed_at_adj": adj_ts,
            "obs_date": day_of(adj_ts),
            "fields": o["fields"],
        })
    observations.sort(key=lambda o: (o["observed_at_adj"], o["record_id"]))
    return observations


# ---------------------------------------------------------------- 挂牌日状态
def _listing_states(observations, coverage):
    """每个广告键的日状态（LISTED/ABSENT/UNKNOWN）与段。

    缺席证据只来自范围完整的批次；范围不完整的批次产生 UNKNOWN 证据日。
    经历窗口为 [首次出现, 最后一条证据（在场/缺席/未知）]，窗口内无证据的天
    记 UNKNOWN，窗口外不作任何断言——缺失不当作零天。
    """
    listings = {}
    for o in observations:
        entry = listings.setdefault(o["key"], {
            "key": o["key"],
            "channel": o["channel"],
            "listing_id": o["listing_id"],
            "reuse_epoch": o["reuse_epoch"],
            "regions": [],
            "observations": [],
            "presence_days": set(),
            "first_ts": None,
        })
        entry["observations"].append(o)
        entry["presence_days"].add(o["obs_date"])
        if o["region"] and o["region"] not in entry["regions"]:
            entry["regions"].append(o["region"])
        if entry["first_ts"] is None or o["observed_at_adj"] < entry["first_ts"]:
            entry["first_ts"] = o["observed_at_adj"]

    # 范围内的批次 -> 已知广告的缺席/未知证据。
    # 范围完整且未出现 => ABSENT（确认下架）；范围不完整（失败页等）=> UNKNOWN，
    # 两者都会把经历窗口延伸到该批次的观察日，使“覆盖不足”与“确认下架”可区分。
    scopes = defaultdict(list)  # (channel, region) -> [coverage,...]
    for cov in coverage.values():
        scopes[(cov["channel"], cov["region"])].append(cov)
    obs_by_batch = defaultdict(set)
    for o in observations:
        obs_by_batch[(o["channel"], o["batch"])].add(o["key"])

    absence_days = defaultdict(set)
    unknown_days = defaultdict(set)
    for (channel, region), batches in scopes.items():
        ordered = sorted(batches, key=lambda b: b["observed_at_adj"])
        members = [l for l in listings.values() if channel == l["channel"] and region in l["regions"]]
        for cov in ordered:
            present = obs_by_batch.get((channel, cov["batch"]), set())
            for listing in members:
                if listing["first_ts"] < cov["observed_at_adj"] and listing["key"] not in present:
                    if cov["scope_complete"]:
                        absence_days[listing["key"]].add(cov["coverage_date"])
                    else:
                        unknown_days[listing["key"]].add(cov["coverage_date"])

    for listing in listings.values():
        pres = listing["presence_days"]
        absc = absence_days.get(listing["key"], set())
        unk = unknown_days.get(listing["key"], set())
        first = min(pres)
        last = max(pres | absc | unk)
        day_states = []
        for day in iter_days(first, last):
            if day in pres:
                state = LISTED
            elif day in absc:
                state = ABSENT
            else:
                state = UNKNOWN
            day_states.append((day, state, None))
        listing["day_states"] = day_states
        listing["absence_days"] = absc
        listing["region"] = listing["regions"][0] if listing["regions"] else None
        listing["segments"] = collapse_segments(day_states)
    return listings


# ---------------------------------------------------------------- 身份归属
def _attribution(raw, listings, notes):
    superseded = {p["supersedes"] for p in raw["identity"] if p["supersedes"]}
    proofs = defaultdict(list)
    for p in raw["identity"]:
        if p["proof_id"] in superseded:
            continue  # supersedes 整体替换被引用凭证
        valid_from = parse_date_or_ts(p["valid_from"])
        valid_to = parse_date_or_ts(p["valid_to"]) if p["valid_to"] is not None else None
        proofs[(p["channel"], p["listing_id"], p["reuse_epoch"])].append({
            "proof_id": p["proof_id"],
            "candidates": sorted(p["candidates"]),
            # 适用期间按日历日解释（左闭右开），时间戳形式归一到规范时区的日
            "valid_from": day_of(valid_from) if isinstance(valid_from, datetime) else valid_from,
            "valid_to": (day_of(valid_to) if isinstance(valid_to, datetime) else valid_to),
        })

    attribution = {}  # (key, day) -> {status, house_id, candidates}
    for key, listing in listings.items():
        candidates_proofs = proofs.get(key, [])
        for day, _, _ in listing["day_states"]:
            covering = [
                p for p in candidates_proofs
                if p["valid_from"] <= day and (p["valid_to"] is None or day < p["valid_to"])
            ]
            if not covering:
                attribution[(key, day)] = {"status": UNATTRIBUTED, "house_id": None, "candidates": []}
                continue
            union = sorted({c for p in covering for c in p["candidates"]})
            if len(covering) > 1:
                notes.append({
                    "category": "overlapping_proofs",
                    "subject": "/".join(key),
                    "detail": {"day": day.isoformat(), "proofs": [p["proof_id"] for p in covering]},
                })
            if len(union) == 1:
                attribution[(key, day)] = {"status": ATTRIBUTED, "house_id": union[0], "candidates": union}
            else:
                # 候选非唯一：不得默认取第一项，房屋级汇总保持歧义
                attribution[(key, day)] = {"status": AMBIGUOUS, "house_id": None, "candidates": union}
    return attribution, proofs


# ---------------------------------------------------------------- 交易限制
def _restrictions(raw, notes):
    by_house = defaultdict(list)
    for tx in raw["transactions"]:
        by_house[tx["house_id"]].append(tx)

    restrictions = {}  # house_id -> [ {transaction_id, kind, start_at, end_at, lifted_by} ]
    for house_id, txs in by_house.items():
        txs = sorted(txs, key=lambda t: (parse_ts(t["effective"]), _as_of_sort_key(t["received"]), t["transaction_id"]))
        open_by_id = {}
        closed = []
        for tx in txs:
            effective = parse_ts(tx["effective"])
            if tx["kind"] in RESTRICTION_KINDS:
                if tx["transaction_id"] in open_by_id or any(r["transaction_id"] == tx["transaction_id"] for r in closed):
                    notes.append({
                        "category": "duplicate_transaction",
                        "subject": tx["transaction_id"],
                        "detail": {"house_id": house_id},
                    })
                open_by_id[tx["transaction_id"]] = {
                    "transaction_id": tx["transaction_id"],
                    "kind": tx["kind"],
                    "start_at": effective,
                    "end_at": None,
                    "lifted_by": None,
                }
            elif tx["kind"] in LIFT_KINDS:
                target = open_by_id.get(tx["reference"])
                if target is not None and target["start_at"] <= effective:
                    # 有根据的撤销：仅从自身生效时刻恢复可交易性
                    target["end_at"] = effective
                    target["lifted_by"] = tx["transaction_id"]
                    closed.append(target)
                    del open_by_id[tx["reference"]]
                else:
                    notes.append({
                        "category": "ungrounded_lift",
                        "subject": tx["transaction_id"],
                        "detail": {"house_id": house_id, "reference": tx["reference"], "kind": tx["kind"]},
                    })
        restrictions[house_id] = closed + list(open_by_id.values())
    return restrictions


def _non_tradable_intervals(restriction_rows):
    """合并 [start, end) 限制区间；end_at=None 表示开放。"""
    intervals = sorted(
        ((r["start_at"], r["end_at"]) for r in restriction_rows),
        key=lambda x: x[0],
    )
    merged = []
    for start, end in intervals:
        if merged and start <= (merged[-1][1] or _FAR):
            prev_start, prev_end = merged[-1]
            merged[-1] = (prev_start, None if (prev_end is None or end is None) else max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _is_tradable(intervals, ts):
    for start, end in intervals:
        if start <= ts and (end is None or ts < end):
            return False
    return True


# ---------------------------------------------------------------- 字段事件
def _field_events(listings):
    """full 提供全量字段（之后缺失的键视为隐式清空）；patch 缺键=未提供，null=清空。"""
    events = []
    for listing in listings.values():
        known = set()
        for o in listing["observations"]:
            if o["kind"] == "full":
                for field in sorted(known - set(o["fields"])):
                    events.append({**_event_base(listing, o), "field": field, "op": "CLEAR", "value": None})
                for field, value in sorted(o["fields"].items()):
                    events.append({**_event_base(listing, o), "field": field, "op": "SET", "value": value})
                known = set(o["fields"])
            else:  # patch
                for field, value in sorted(o["fields"].items()):
                    if value is None:
                        events.append({**_event_base(listing, o), "field": field, "op": "CLEAR", "value": None})
                        known.discard(field)
                    else:
                        events.append({**_event_base(listing, o), "field": field, "op": "SET", "value": value})
                        known.add(field)
    return events


def _event_base(listing, obs):
    return {
        "channel": listing["channel"],
        "listing_id": listing["listing_id"],
        "reuse_epoch": listing["reuse_epoch"],
        "effective_at": obs["observed_at_adj"],
        "record_id": obs["record_id"],
    }


# ---------------------------------------------------------------- 房屋经历
def _house_facts(listings, attribution, restrictions):
    """房屋级日事实：可交易挂牌 / 滞留 / 未知 / 确认下架，含来源。"""
    house_days = defaultdict(dict)  # house -> day -> buckets
    for key, listing in listings.items():
        for day, state, _ in listing["day_states"]:
            att = attribution.get((key, day))
            if att is None or att["status"] != ATTRIBUTED:
                continue  # 歧义/无凭证：广告级可查，房屋级不武断归属
            house_id = att["house_id"]
            bucket = house_days[house_id].setdefault(day, {"obs": [], "unknown": [], "absent": []})
            if state == LISTED:
                bucket["obs"].extend(o for o in listing["observations"] if o["obs_date"] == day)
            elif state == UNKNOWN:
                bucket["unknown"].append(listing)
            else:
                bucket["absent"].append(listing)

    tradability = {h: _non_tradable_intervals(rows) for h, rows in restrictions.items()}
    rows, segments = [], []
    for house_id, days in sorted(house_days.items()):
        intervals = tradability.get(house_id, [])
        first, last = min(days), max(days)
        flag_rows = {k: [] for k in ("listed_tradable", "residual", "unknown", "delisted")}
        for day in iter_days(first, last):
            bucket = days.get(day)
            flags = {"listed_tradable": False, "residual": False, "unknown": False, "delisted": False}
            sources = defaultdict(list)

            def src(listing):
                return {
                    "channel": listing["channel"],
                    "listing_id": listing["listing_id"],
                    "reuse_epoch": listing["reuse_epoch"],
                    "region": listing["region"],
                }

            if bucket and bucket["obs"]:
                tradable_obs = [o for o in bucket["obs"] if _is_tradable(intervals, o["observed_at_adj"])]
                residual_obs = [o for o in bucket["obs"] if not _is_tradable(intervals, o["observed_at_adj"])]
                if tradable_obs:
                    flags["listed_tradable"] = True
                    for o in tradable_obs:
                        sources["listed_tradable"].append(src(listings[o["key"]]))
                if residual_obs:
                    # 成交/限制后仍在的广告 => 渠道滞留曝光
                    flags["residual"] = True
                    for o in residual_obs:
                        sources["residual"].append(src(listings[o["key"]]))
            elif bucket and bucket["unknown"]:
                flags["unknown"] = True
                for l in bucket["unknown"]:
                    sources["unknown"].append(src(l))
            elif bucket and bucket["absent"]:
                flags["delisted"] = True
                for l in bucket["absent"]:
                    sources["delisted"].append(src(l))
            else:
                flags["unknown"] = True  # 窗口间隙：无证据，不当作零天

            rows.append({
                "house_id": house_id,
                "day": day,
                **flags,
                "sources": {k: _dedup(v) for k, v in sources.items()},
            })
            for flag, on in flags.items():
                if on:
                    flag_rows[flag].append((day, flag, tuple(_dedup(sources[flag]))))

        kind_map = {
            "listed_tradable": "LISTED_TRADABLE",
            "residual": "RESIDUAL",
            "unknown": "UNKNOWN",
            "delisted": "DELISTED",
        }
        for flag, day_states in flag_rows.items():
            for seg in collapse_segments(day_states):
                seg_sources = _dedup([s for payload in seg["payloads"] for s in payload])
                segments.append({
                    "house_id": house_id,
                    "kind": kind_map[flag],
                    "start_day": seg["start_day"],
                    "end_day": seg["end_day"],
                    "days": seg["days"],
                    "sources": seg_sources,
                })
    return rows, segments


def _dedup(dicts):
    seen, out = set(), []
    for d in dicts:
        key = tuple(sorted(d.items()))
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


# ---------------------------------------------------------------- 曝光与汇总
def _exposure_days(listings, attribution, restrictions):
    tradability = {h: _non_tradable_intervals(rows) for h, rows in restrictions.items()}
    by_day = {}
    for key, listing in listings.items():
        for o in listing["observations"]:
            day_key = (key, o["obs_date"], o["region"])
            entry = by_day.setdefault(day_key, {"obs": []})
            entry["obs"].append(o)
    rows = []
    for (key, day, region), entry in sorted(by_day.items(), key=lambda x: (x[0][1], x[0][0])):
        att = attribution.get((key, day), {"status": UNATTRIBUTED, "house_id": None, "candidates": []})
        house_id = att["house_id"] if att["status"] == ATTRIBUTED else None
        tradable = residual = None
        if house_id is not None:
            intervals = tradability.get(house_id, [])
            tradable = any(_is_tradable(intervals, o["observed_at_adj"]) for o in entry["obs"])
            residual = any(not _is_tradable(intervals, o["observed_at_adj"]) for o in entry["obs"])
        listing = listings[key]
        rows.append({
            "channel": listing["channel"],
            "listing_id": listing["listing_id"],
            "reuse_epoch": listing["reuse_epoch"],
            "region": region,
            "day": day,
            "attribution_status": att["status"],
            "house_id": house_id,
            "tradable": tradable,
            "residual": residual,
        })
    return rows


def _aggregates(house_day_rows, exposure_rows):
    """片区×月份：房屋挂牌天数（跨渠道互不重计）与各渠道曝光天数。"""
    agg_house = defaultdict(lambda: {"listing": set(), "residual": set(), "unknown": set(), "delisted": set()})
    for row in house_day_rows:
        month = month_of(row["day"])
        flag_map = {
            "listing": "listed_tradable",
            "residual": "residual",
            "unknown": "unknown",
            "delisted": "delisted",
        }
        for metric, flag in flag_map.items():
            if not row[flag]:
                continue
            regions = {s["region"] for s in row["sources"].get(flag, []) if s["region"]}
            for region in regions:
                agg_house[(region, month, row["house_id"])][metric].add(row["day"])

    house_rows = [
        {
            "region": region, "month": month, "house_id": house_id,
            "listing_days": len(metrics["listing"]),
            "residual_days": len(metrics["residual"]),
            "unknown_days": len(metrics["unknown"]),
            "delisted_days": len(metrics["delisted"]),
        }
        for (region, month, house_id), metrics in sorted(agg_house.items())
    ]

    agg_channel = defaultdict(lambda: {
        "house_days": set(), "ad_days": set(), "residual_days": set(),
        "ambiguous_ad_days": set(), "houses": set(),
    })
    for row in exposure_rows:
        if row["region"] is None:
            continue
        month = month_of(row["day"])
        key = (row["region"], month, row["channel"])
        metrics = agg_channel[key]
        ad = (row["channel"], row["listing_id"], row["reuse_epoch"])
        metrics["ad_days"].add((ad, row["day"]))
        if row["attribution_status"] == ATTRIBUTED:
            metrics["house_days"].add((row["house_id"], row["day"]))
            metrics["houses"].add(row["house_id"])
            if row["residual"]:
                metrics["residual_days"].add((row["house_id"], row["day"]))
        else:
            # 歧义/无归属的曝光单列，不并入房屋级数字
            metrics["ambiguous_ad_days"].add((ad, row["day"]))

    channel_rows = [
        {
            "region": region, "month": month, "channel": channel,
            "house_exposure_days": len(m["house_days"]),
            "ad_exposure_days": len(m["ad_days"]),
            "residual_days": len(m["residual_days"]),
            "ambiguous_ad_exposure_days": len(m["ambiguous_ad_days"]),
            "houses": len(m["houses"]),
        }
        for (region, month, channel), m in sorted(agg_channel.items())
    ]
    return house_rows, channel_rows


# ---------------------------------------------------------------- 主入口
def compute_derived(raw):
    """raw: {dataset: [行]}（JSON 字段已解码）。返回 {表名: [行]} 与 notes。"""
    notes = []
    offsets = _clock_offsets(raw, notes)
    coverage = _coverage(raw, offsets, notes)
    observations = _observations(raw, coverage, offsets, notes)
    listings = _listing_states(observations, coverage)
    attribution, _ = _attribution(raw, listings, notes)
    restrictions = _restrictions(raw, notes)
    field_events = _field_events(listings)
    house_day_rows, house_segment_rows = _house_facts(listings, attribution, restrictions)
    exposure_rows = _exposure_days(listings, attribution, restrictions)
    agg_house_rows, agg_channel_rows = _aggregates(house_day_rows, exposure_rows)

    restriction_rows = [
        {"house_id": house_id, **r} for house_id, rows in sorted(restrictions.items()) for r in rows
    ]

    tables = {
        "fact_batch_coverage": list(coverage.values()),
        "fact_observation": [{k: v for k, v in o.items() if k != "key"} for o in observations],
        "fact_listing_day": [
            {
                "channel": l["channel"], "listing_id": l["listing_id"], "reuse_epoch": l["reuse_epoch"],
                "region": l["region"], "day": day, "state": state,
            }
            for l in listings.values() for day, state, _ in l["day_states"]
        ],
        "fact_listing_segment": [
            {
                "channel": l["channel"], "listing_id": l["listing_id"], "reuse_epoch": l["reuse_epoch"],
                "region": l["region"], "state": seg["state"],
                "start_day": seg["start_day"], "end_day": seg["end_day"], "days": seg["days"],
            }
            for l in listings.values() for seg in l["segments"]
        ],
        "fact_listing_attribution": [
            {
                "channel": key[0], "listing_id": key[1], "reuse_epoch": key[2],
                "day": day, "status": att["status"], "house_id": att["house_id"],
                "candidates": att["candidates"],
            }
            for (key, day), att in sorted(attribution.items(), key=lambda x: (x[0][0], x[0][1]))
        ],
        "fact_ad_field_event": field_events,
        "fact_house_restriction": restriction_rows,
        "fact_house_day": house_day_rows,
        "fact_house_segment": house_segment_rows,
        "fact_exposure_day": exposure_rows,
        "agg_region_month_house": agg_house_rows,
        "agg_region_month_channel": agg_channel_rows,
        "fact_note": notes,
    }
    return tables
