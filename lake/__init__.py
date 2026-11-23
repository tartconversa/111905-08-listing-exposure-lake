"""多渠道房源挂牌经历与曝光事实湖。

接收授权渠道导出（NDJSON，格式见 docs/source-contract.md），
在 DuckDB 中构建房屋级挂牌经历与渠道曝光数据集。
所有派生数据按版本固化：旧版本只读，新版本附带完整变化说明。
"""

from .store import connect, init_schema
from .ingest import ingest_dir
from .build import build_version
from . import query

__all__ = ["connect", "init_schema", "ingest_dir", "build_version", "query"]
