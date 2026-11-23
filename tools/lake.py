#!/usr/bin/env python3
"""挂牌经历数据湖命令行。

用法示例：
    python3 tools/lake.py ingest --db build/lake.duckdb --data data
    python3 tools/lake.py build  --db build/lake.duckdb --note "初始构建"
    python3 tools/lake.py versions --db build/lake.duckdb
    python3 tools/lake.py house   --db build/lake.duckdb --house H1
    python3 tools/lake.py region  --db build/lake.duckdb --region west --month 2026-11
    python3 tools/lake.py ad      --db build/lake.duckdb --channel B --listing L9 --epoch r7
    python3 tools/lake.py changes --db build/lake.duckdb --version 2
    python3 tools/lake.py notes   --db build/lake.duckdb
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lake import build_version, connect, ingest_dir, init_schema, query  # noqa: E402


def _print(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main(argv=None):
    parser = argparse.ArgumentParser(description="多渠道房源挂牌经历与曝光事实湖")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default="build/lake.duckdb", help="DuckDB 文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", parents=[common], help="导入一个资料目录（NDJSON，幂等追加）")
    p.add_argument("--data", required=True, type=Path)

    p = sub.add_parser("build", parents=[common], help="以当前原始层构建新版本")
    p.add_argument("--note", default=None)

    sub.add_parser("versions", parents=[common], help="列出全部数据版本")

    p = sub.add_parser("house", parents=[common], help="按房屋查询挂牌段、未知段、渠道滞留及来源")
    p.add_argument("--house", required=True)
    p.add_argument("--version", type=int, default=None)

    p = sub.add_parser("region", parents=[common], help="按片区和月份查询挂牌/曝光天数")
    p.add_argument("--region", required=True)
    p.add_argument("--month", required=True)
    p.add_argument("--version", type=int, default=None)

    p = sub.add_parser("ad", parents=[common], help="按广告键查询广告级资料（含歧义身份广告）")
    p.add_argument("--channel", required=True)
    p.add_argument("--listing", required=True)
    p.add_argument("--epoch", required=True)
    p.add_argument("--at", default=None, help="字段状态回看时刻（ISO8601）")
    p.add_argument("--version", type=int, default=None)

    p = sub.add_parser("changes", parents=[common], help="查看某版本相对父版本的变化说明")
    p.add_argument("--version", type=int, required=True)

    p = sub.add_parser("notes", parents=[common], help="查看某版本的来源异常注记")
    p.add_argument("--version", type=int, default=None)

    args = parser.parse_args(argv)
    con = connect(args.db)
    init_schema(con)

    if args.command == "ingest":
        _print(ingest_dir(con, args.data))
    elif args.command == "build":
        _print(build_version(con, note=args.note))
    elif args.command == "versions":
        _print(query.list_versions(con))
    elif args.command == "house":
        _print(query.house_experience(con, args.house, args.version))
    elif args.command == "region":
        _print(query.region_month(con, args.region, args.month, args.version))
    elif args.command == "ad":
        _print(query.ad_detail(con, args.channel, args.listing, args.epoch, args.version, at=args.at))
    elif args.command == "changes":
        _print(query.version_changes(con, args.version))
    elif args.command == "notes":
        _print(query.notes(con, args.version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
