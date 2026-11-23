"""DuckDB 存储层：原始表（只增不改）+ 按版本固化的派生表。

原始表以 payload_hash 去重，重复导入同一文件不产生新行；
派生表全部带 version_id，构建只 INSERT 新版本行，绝不更新/删除旧版本行。
"""

from pathlib import Path

import duckdb

SCHEMA = """
-- ============ 原始层（append-only） ============
CREATE TABLE IF NOT EXISTS raw_exports (
    payload_hash VARCHAR PRIMARY KEY,
    ingest_id VARCHAR,
    channel VARCHAR, region VARCHAR, batch VARCHAR,
    expected_pages VARCHAR, complete BOOLEAN, observed_at VARCHAR,
    ingested_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS raw_page_receipts (
    payload_hash VARCHAR PRIMARY KEY,
    ingest_id VARCHAR,
    channel VARCHAR, batch VARCHAR, page INTEGER, received VARCHAR,
    ingested_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS raw_observations (
    payload_hash VARCHAR PRIMARY KEY,
    ingest_id VARCHAR,
    record_id VARCHAR, channel VARCHAR, listing_id VARCHAR, reuse_epoch VARCHAR,
    batch VARCHAR, page INTEGER, kind VARCHAR, observed_at VARCHAR, fields VARCHAR,
    ingested_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS raw_clock_adjustments (
    payload_hash VARCHAR PRIMARY KEY,
    ingest_id VARCHAR,
    adjustment_id VARCHAR, channel VARCHAR, batches VARCHAR,
    offset_seconds INTEGER, received VARCHAR,
    ingested_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS raw_identity (
    payload_hash VARCHAR PRIMARY KEY,
    ingest_id VARCHAR,
    proof_id VARCHAR, channel VARCHAR, listing_id VARCHAR, reuse_epoch VARCHAR,
    candidates VARCHAR, valid_from VARCHAR, valid_to VARCHAR,
    received VARCHAR, supersedes VARCHAR,
    ingested_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS raw_transactions (
    payload_hash VARCHAR PRIMARY KEY,
    ingest_id VARCHAR,
    transaction_id VARCHAR, house_id VARCHAR, kind VARCHAR,
    effective VARCHAR, received VARCHAR, reference VARCHAR,
    ingested_at TIMESTAMPTZ
);

-- ============ 版本登记 ============
CREATE TABLE IF NOT EXISTS dim_version (
    version_id INTEGER PRIMARY KEY,
    parent_id INTEGER,
    created_at TIMESTAMPTZ,
    input_hash VARCHAR,
    note VARCHAR
);

-- ============ 派生层（全部按 version_id 固化） ============
CREATE TABLE IF NOT EXISTS fact_batch_coverage (
    version_id INTEGER, channel VARCHAR, region VARCHAR, batch VARCHAR,
    observed_at_raw TIMESTAMPTZ, offset_seconds INTEGER, observed_at_adj TIMESTAMPTZ,
    coverage_date DATE, expected_pages VARCHAR, received_pages VARCHAR,
    missing_pages VARCHAR, scope_complete BOOLEAN
);
CREATE TABLE IF NOT EXISTS fact_observation (
    version_id INTEGER, record_id VARCHAR, channel VARCHAR, listing_id VARCHAR,
    reuse_epoch VARCHAR, region VARCHAR, batch VARCHAR, page INTEGER, kind VARCHAR,
    observed_at_raw TIMESTAMPTZ, offset_seconds INTEGER, observed_at_adj TIMESTAMPTZ,
    obs_date DATE, fields VARCHAR
);
CREATE TABLE IF NOT EXISTS fact_listing_day (
    version_id INTEGER, channel VARCHAR, listing_id VARCHAR, reuse_epoch VARCHAR,
    region VARCHAR, day DATE, state VARCHAR  -- LISTED / ABSENT / UNKNOWN
);
CREATE TABLE IF NOT EXISTS fact_listing_segment (
    version_id INTEGER, channel VARCHAR, listing_id VARCHAR, reuse_epoch VARCHAR,
    region VARCHAR, state VARCHAR, start_day DATE, end_day DATE, days INTEGER
);
CREATE TABLE IF NOT EXISTS fact_listing_attribution (
    version_id INTEGER, channel VARCHAR, listing_id VARCHAR, reuse_epoch VARCHAR,
    day DATE, status VARCHAR,  -- ATTRIBUTED / AMBIGUOUS / UNATTRIBUTED
    house_id VARCHAR, candidates VARCHAR
);
CREATE TABLE IF NOT EXISTS fact_ad_field_event (
    version_id INTEGER, channel VARCHAR, listing_id VARCHAR, reuse_epoch VARCHAR,
    field VARCHAR, op VARCHAR,  -- SET / CLEAR
    value VARCHAR, effective_at TIMESTAMPTZ, record_id VARCHAR
);
CREATE TABLE IF NOT EXISTS fact_house_restriction (
    version_id INTEGER, house_id VARCHAR, transaction_id VARCHAR, kind VARCHAR,
    start_at TIMESTAMPTZ, end_at TIMESTAMPTZ, lifted_by VARCHAR
);
CREATE TABLE IF NOT EXISTS fact_house_day (
    version_id INTEGER, house_id VARCHAR, day DATE,
    listed_tradable BOOLEAN, residual BOOLEAN, unknown BOOLEAN, delisted BOOLEAN,
    sources VARCHAR  -- JSON: {flag: [{channel, listing_id, reuse_epoch, region}]}
);
CREATE TABLE IF NOT EXISTS fact_house_segment (
    version_id INTEGER, house_id VARCHAR, kind VARCHAR,
    -- LISTED_TRADABLE / RESIDUAL / UNKNOWN / DELISTED
    start_day DATE, end_day DATE, days INTEGER, sources VARCHAR
);
CREATE TABLE IF NOT EXISTS fact_exposure_day (
    version_id INTEGER, channel VARCHAR, listing_id VARCHAR, reuse_epoch VARCHAR,
    region VARCHAR, day DATE, attribution_status VARCHAR, house_id VARCHAR,
    tradable BOOLEAN, residual BOOLEAN
);
CREATE TABLE IF NOT EXISTS agg_region_month_house (
    version_id INTEGER, region VARCHAR, month VARCHAR, house_id VARCHAR,
    listing_days INTEGER, residual_days INTEGER, unknown_days INTEGER,
    delisted_days INTEGER
);
CREATE TABLE IF NOT EXISTS agg_region_month_channel (
    version_id INTEGER, region VARCHAR, month VARCHAR, channel VARCHAR,
    house_exposure_days INTEGER, ad_exposure_days INTEGER,
    residual_days INTEGER, ambiguous_ad_exposure_days INTEGER, houses INTEGER
);
CREATE TABLE IF NOT EXISTS fact_note (
    version_id INTEGER, category VARCHAR, subject VARCHAR, detail VARCHAR
);
CREATE TABLE IF NOT EXISTS version_change (
    version_id INTEGER, category VARCHAR, subject VARCHAR, detail VARCHAR
);
"""


def connect(path=":memory:"):
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


def init_schema(con):
    for statement in SCHEMA.split(";"):
        stmt = statement.strip()
        if stmt:
            con.execute(stmt)
    return con
