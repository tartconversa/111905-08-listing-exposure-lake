"""导入层：把授权渠道导出（NDJSON 目录）追加进原始表。

只做契约级校验（字段集合、基本类型、时间格式、枚举值），不推断业务结论；
完全相同的行按 payload_hash 去重，重复导入同一目录是幂等的。
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .timeutil import parse_date_or_ts, parse_ts

# 数据集 -> (原始表, 字段集合, 行级校验函数)
_KINDS_OBS = {"full", "patch"}
_KINDS_TX = {"signed", "cancelled", "blocked", "released"}


def _check_ts(*fields):
    def check(row):
        for f in fields:
            if row.get(f) is not None:
                parse_ts(row[f])
    return check


def _check_exports(row):
    parse_ts(row["observed_at"])
    if not all(isinstance(p, int) and not isinstance(p, bool) for p in row["expected_pages"]):
        raise ValueError("expected_pages 必须是整数数组")


def _check_observations(row):
    parse_ts(row["observed_at"])
    if row["kind"] not in _KINDS_OBS:
        raise ValueError(f"observations.kind 非法：{row['kind']!r}")


def _check_identity(row):
    parse_date_or_ts(row["valid_from"])
    if row["valid_to"] is not None:
        parse_date_or_ts(row["valid_to"])
    parse_date_or_ts(row["received"])
    if not row["candidates"]:
        raise ValueError("identity.candidates 不能为空数组")


def _check_transactions(row):
    parse_ts(row["effective"])
    parse_date_or_ts(row["received"])
    if row["kind"] not in _KINDS_TX:
        raise ValueError(f"transactions.kind 非法：{row['kind']!r}")


DATASETS = {
    "exports": ("raw_exports", {"channel", "region", "batch", "expected_pages", "complete", "observed_at"}, _check_exports),
    "page_receipts": ("raw_page_receipts", {"channel", "batch", "page", "received"}, _check_ts("received")),
    "observations": ("raw_observations", {"record_id", "channel", "listing_id", "reuse_epoch", "batch", "page", "kind", "observed_at", "fields"}, _check_observations),
    "clock_adjustments": ("raw_clock_adjustments", {"adjustment_id", "channel", "batches", "offset_seconds", "received"}, _check_ts("received")),
    "identity": ("raw_identity", {"proof_id", "channel", "listing_id", "reuse_epoch", "candidates", "valid_from", "valid_to", "received", "supersedes"}, _check_identity),
    "transactions": ("raw_transactions", {"transaction_id", "house_id", "kind", "effective", "received", "reference"}, _check_transactions),
}

# 原始表中存为 JSON 字符串的字段
_JSON_FIELDS = {"expected_pages", "fields", "batches", "candidates"}


def payload_hash(row):
    canonical = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate(dataset, row, where):
    table, fields, check = DATASETS[dataset]
    if set(row) != fields:
        raise ValueError(f"{where}: 字段不符，期望 {sorted(fields)}，实际 {sorted(row)}")
    try:
        check(row)
    except ValueError as exc:
        raise ValueError(f"{where}: {exc}") from exc


def ingest_rows(con, dataset, rows, ingest_id=None, ingested_at=None):
    """把一类资料的行追加进对应原始表，返回 (插入数, 跳过数)。"""
    table, _, _ = DATASETS[dataset]
    ingest_id = ingest_id or uuid.uuid4().hex[:12]
    ingested_at = ingested_at or datetime.now(timezone.utc)
    inserted = skipped = 0
    columns = None
    for i, row in enumerate(rows, 1):
        _validate(dataset, row, f"{dataset} 第{i}行")
        ph = payload_hash(row)
        record = {"payload_hash": ph, "ingest_id": ingest_id, "ingested_at": ingested_at}
        for key, value in row.items():
            record[key] = json.dumps(value, ensure_ascii=False, sort_keys=True) if key in _JSON_FIELDS else value
        if columns is None:
            columns = list(record)
        exists = con.execute(
            f"SELECT 1 FROM {table} WHERE payload_hash = ?", [ph]
        ).fetchone()
        if exists:
            skipped += 1
            continue
        con.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            [record[c] for c in columns],
        )
        inserted += 1
    return inserted, skipped


def ingest_dir(con, directory):
    """导入一个资料目录中全部 NDJSON（按数据集文件名识别）。"""
    directory = Path(directory)
    ingest_id = uuid.uuid4().hex[:12]
    ingested_at = datetime.now(timezone.utc)
    summary = {"ingest_id": ingest_id, "datasets": {}}
    for dataset in DATASETS:
        path = directory / f"{dataset}.ndjson"
        if not path.exists():
            summary["datasets"][dataset] = {"inserted": 0, "skipped": 0, "file": None}
            continue
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        inserted, skipped = ingest_rows(con, dataset, rows, ingest_id, ingested_at)
        summary["datasets"][dataset] = {"inserted": inserted, "skipped": skipped, "file": path.name}
    return summary
