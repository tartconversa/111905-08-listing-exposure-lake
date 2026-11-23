"""版本化构建：原始层 -> 派生层快照 + 变化说明。

每次构建把当前原始层整体重算为一个新版本（确定性全量计算）：
- 输入指纹（全部原始行哈希）与上一版本一致时不产生新版本；
- 派生行只以新 version_id 插入，旧版本行永不修改，已引用旧版可稳定复查；
- 与父版本的差异（覆盖、下架判断、归属、经历段、片区汇总）统一写入
  version_change，一个版本的变化一起解释。
"""

import hashlib
import json
from datetime import datetime, timezone

from .derive import compute_derived
from .ingest import DATASETS
from .store import init_schema

# 派生表 -> 存为 JSON 字符串的列
_JSON_COLUMNS = {
    "fact_batch_coverage": {"expected_pages", "received_pages", "missing_pages"},
    "fact_observation": {"fields"},
    "fact_listing_attribution": {"candidates"},
    "fact_ad_field_event": {"value"},
    "fact_house_day": {"sources"},
    "fact_house_segment": {"sources"},
    "fact_note": {"detail"},
}

DERIVED_TABLES = [
    "fact_batch_coverage", "fact_observation", "fact_listing_day",
    "fact_listing_segment", "fact_listing_attribution", "fact_ad_field_event",
    "fact_house_restriction", "fact_house_day", "fact_house_segment",
    "fact_exposure_day", "agg_region_month_house", "agg_region_month_channel",
    "fact_note",
]


def _jsonable(value):
    from datetime import date, datetime
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def load_raw(con):
    """读取原始层，解码 JSON 字段，返回 {dataset: [行]}（仅契约字段）。"""
    raw = {}
    json_fields = {"expected_pages", "fields", "batches", "candidates"}
    for dataset, (table, fields, _) in DATASETS.items():
        cols = sorted(fields)
        rows = con.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        decoded = []
        for row in rows:
            item = dict(zip(cols, row))
            for field in json_fields & fields:
                item[field] = json.loads(item[field])
            decoded.append(item)
        raw[dataset] = decoded
    return raw


def input_hash(con):
    hashes = set()
    for table, _, _ in DATASETS.values():
        for (ph,) in con.execute(f"SELECT payload_hash FROM {table}").fetchall():
            hashes.add(ph)
    digest = hashlib.sha256()
    for ph in sorted(hashes):
        digest.update(ph.encode("ascii"))
    return digest.hexdigest()


def latest_version(con):
    row = con.execute(
        "SELECT version_id, parent_id, input_hash, note FROM dim_version ORDER BY version_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return {"version_id": row[0], "parent_id": row[1], "input_hash": row[2], "note": row[3]}


def _insert_rows(con, table, version_id, rows):
    if not rows:
        return
    json_cols = _JSON_COLUMNS.get(table, set())
    columns = ["version_id"] + [c for c in rows[0] if c != "version_id"]

    def cell(row, col):
        if col == "version_id":
            return version_id
        value = row[col]
        if col in json_cols and value is not None:
            return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True)
        return value

    values = [[cell(row, c) for c in columns] for row in rows]
    con.executemany(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
        values,
    )


def _load_version_rows(con, table, version_id):
    cursor = con.execute(f"SELECT * FROM {table} WHERE version_id = ?", [version_id])
    columns = [d[0] for d in cursor.description]
    json_cols = _JSON_COLUMNS.get(table, set()) | {"detail"}
    out = []
    for row in cursor.fetchall():
        item = dict(zip(columns, row))
        for col in json_cols & item.keys():
            if item[col] is not None:
                item[col] = json.loads(item[col])
        out.append(item)
    return out


# 差异类别 -> (派生表, 键函数, 值函数, 主题函数)
def _seg_value(r):
    return {
        "kind": r["kind"],
        "start_day": r["start_day"].isoformat(),
        "end_day": r["end_day"].isoformat(),
        "days": r["days"],
        "sources": r["sources"],
    }


DIFF_SPECS = {
    "coverage": (
        "fact_batch_coverage",
        lambda r: (r["channel"], r["batch"]),
        lambda r: {
            "scope_complete": r["scope_complete"],
            "observed_at_adj": r["observed_at_adj"].isoformat(),
            "coverage_date": r["coverage_date"].isoformat(),
            "missing_pages": r["missing_pages"],
        },
        lambda k: f"{k[0]}/{k[1]}",
    ),
    "delisting": (
        "fact_listing_day",
        lambda r: (r["channel"], r["listing_id"], r["reuse_epoch"], r["day"].isoformat()),
        lambda r: r["state"],
        lambda k: f"{k[0]}/{k[1]}/{k[2]}@{k[3]}",
    ),
    "attribution": (
        "fact_listing_attribution",
        lambda r: (r["channel"], r["listing_id"], r["reuse_epoch"], r["day"].isoformat()),
        lambda r: {"status": r["status"], "house_id": r["house_id"], "candidates": r["candidates"]},
        lambda k: f"{k[0]}/{k[1]}/{k[2]}@{k[3]}",
    ),
    "house_segments": (
        "fact_house_segment",
        lambda r: r["house_id"],
        _seg_value,
        lambda k: k,
    ),
    "restrictions": (
        "fact_house_restriction",
        lambda r: (r["house_id"], r["transaction_id"]),
        lambda r: {
            "kind": r["kind"],
            "start_at": r["start_at"].isoformat(),
            "end_at": r["end_at"].isoformat() if r["end_at"] else None,
            "lifted_by": r["lifted_by"],
        },
        lambda k: f"{k[0]}/{k[1]}",
    ),
    "region_month_house": (
        "agg_region_month_house",
        lambda r: (r["region"], r["month"], r["house_id"]),
        lambda r: {
            "listing_days": r["listing_days"],
            "residual_days": r["residual_days"],
            "unknown_days": r["unknown_days"],
            "delisted_days": r["delisted_days"],
        },
        lambda k: f"{k[0]}/{k[1]}/{k[2]}",
    ),
    "region_month_channel": (
        "agg_region_month_channel",
        lambda r: (r["region"], r["month"], r["channel"]),
        lambda r: {
            "house_exposure_days": r["house_exposure_days"],
            "ad_exposure_days": r["ad_exposure_days"],
            "residual_days": r["residual_days"],
            "ambiguous_ad_exposure_days": r["ambiguous_ad_exposure_days"],
            "houses": r["houses"],
        },
        lambda k: f"{k[0]}/{k[1]}/{k[2]}",
    ),
}

# 多值类别（同一键多条记录，整体比较）
_MULTI_VALUE = {"house_segments"}


def _diff(con, parent_id, new_tables):
    changes = []
    for category, (table, key_fn, value_fn, subject_fn) in DIFF_SPECS.items():
        before_rows = _load_version_rows(con, table, parent_id)
        after_rows = new_tables[table]
        if category in _MULTI_VALUE:
            before_map = {}
            for r in before_rows:
                before_map.setdefault(key_fn(r), []).append(value_fn(r))
            after_map = {}
            for r in after_rows:
                after_map.setdefault(key_fn(r), []).append(value_fn(r))
            for m in (before_map, after_map):
                for k in m:
                    m[k] = sorted(m[k], key=lambda v: json.dumps(_jsonable(v), ensure_ascii=False, sort_keys=True))
        else:
            before_map = {key_fn(r): value_fn(r) for r in before_rows}
            after_map = {key_fn(r): value_fn(r) for r in after_rows}
        for key in sorted(before_map.keys() | after_map.keys(), key=str):
            before, after = before_map.get(key), after_map.get(key)
            if before != after:
                changes.append({
                    "category": category,
                    "subject": subject_fn(key),
                    "detail": {"before": _jsonable(before), "after": _jsonable(after)},
                })
    return changes


def build_version(con, note=None):
    """构建新版本；输入未变化时返回现有版本（noop）。"""
    init_schema(con)
    current_hash = input_hash(con)
    latest = latest_version(con)
    if latest and latest["input_hash"] == current_hash:
        return {"version_id": latest["version_id"], "status": "noop", "changes": 0}

    version_id = (latest["version_id"] + 1) if latest else 1
    parent_id = latest["version_id"] if latest else None
    raw = load_raw(con)
    tables = compute_derived(raw)

    con.execute(
        "INSERT INTO dim_version (version_id, parent_id, created_at, input_hash, note) VALUES (?, ?, ?, ?, ?)",
        [version_id, parent_id, datetime.now(timezone.utc), current_hash, note],
    )
    for table in DERIVED_TABLES:
        _insert_rows(con, table, version_id, tables[table])

    if parent_id is None:
        changes = [{
            "category": "initial_build",
            "subject": "lake",
            "detail": {"counts": {t: len(rows) for t, rows in tables.items()}},
        }]
    else:
        changes = _diff(con, parent_id, tables)
    for change in changes:
        con.execute(
            "INSERT INTO version_change (version_id, category, subject, detail) VALUES (?, ?, ?, ?)",
            [version_id, change["category"], change["subject"],
             json.dumps(_jsonable(change["detail"]), ensure_ascii=False, sort_keys=True)],
        )
    return {
        "version_id": version_id,
        "parent_id": parent_id,
        "status": "built",
        "counts": {t: len(rows) for t, rows in tables.items()},
        "changes": len(changes),
    }
