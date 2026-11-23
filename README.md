# 多渠道房源挂牌经历与曝光事实湖

授权渠道挂牌导出、覆盖声明、匿名身份及成交限制。本仓库包含原始合成资料、
来源格式文档（docs/source-contract.md）、资料检查程序，以及挂牌经历数据湖：
把广告、房屋身份凭证和交易限制整理为**房屋级挂牌经历**与**渠道曝光**数据集，
按版本固化，可复查历史版本。

## 环境

使用 Python 3.11，依赖精确版本记录在 requirements.txt。DuckDB 用于本地分析存储。

    python3 -m pip install -r requirements.txt

## 数据湖用法

导入授权渠道导出（NDJSON 目录，幂等追加，重复导入不产生重复行）：

    python3 tools/lake.py ingest --db build/lake.duckdb --data data

以当前原始层构建新版本（输入未变化时为 noop，不产生新版本）：

    python3 tools/lake.py build --db build/lake.duckdb --note "初始构建"

查询：

    python3 tools/lake.py versions --db build/lake.duckdb          # 全部数据版本
    python3 tools/lake.py house --db build/lake.duckdb --house H1  # 房屋：挂牌段/未知段/滞留及来源
    python3 tools/lake.py region --db build/lake.duckdb --region west --month 2026-11
    python3 tools/lake.py ad --db build/lake.duckdb --channel B --listing L9 --epoch r7
    python3 tools/lake.py changes --db build/lake.duckdb --version 2  # 相对父版本的变化说明
    python3 tools/lake.py notes --db build/lake.duckdb             # 来源异常注记

`house`/`region`/`ad` 均接受 `--version N` 复查旧版本；旧版本行在构建中从不被修改。

## 语义口径（与 docs/source-contract.md 对齐）

- **下架判断**：只有范围完整（`complete=true` 且 `expected_pages` 全部送达）的批次，
  其范围内广告的缺席才判为下架（ABSENT）；失败页、未收齐页只产生未知（UNKNOWN），
  未知不是零天，也不与前后观察拼接成持续挂牌。
- **经历窗口**：每个广告键 (channel, listing_id, reuse_epoch) 的经历窗口为
  [首次出现, 最后一条证据]；窗口内每天为 LISTED / ABSENT / UNKNOWN 之一，窗口外不作断言。
- **时钟**：观察与批次时间先按 clock_adjustments 校正，再归一到规范时区（Asia/Shanghai）的日历日。
- **身份**：凭证按 valid_from/to（左闭右开）生效；supersedes 整体替换被引用凭证；
  多候选或凭证冲突 => 歧义，房屋级汇总不武断归属，广告级资料始终可查；
  门店房号（house_code）不参与身份合并，reuse_epoch 隔离房号复用。
- **可交易性**：signed/blocked 自生效时刻截断可交易经历；cancelled/released 仅在
  reference 指向同房屋先生效的限制时有据恢复（自其生效时刻起），不回填未观察空白；
  成交/限制后仍被观察到的广告计为渠道滞留曝光（RESIDUAL）。
- **房屋日状态优先级**：有可交易观察 => 可交易挂牌；观察落在限制窗口 => 滞留；
  无观察且有广告未知 => 未知；全部有证据广告确认缺席 => 下架。
- **汇总**：片区×月份的房屋挂牌天数按房屋去重（同一房屋跨渠道同天只计一天，
  互不重计）；渠道曝光天数按 (房屋, 天) 与 (广告, 天) 分别统计，歧义广告曝光单列。
- **版本**：补齐失败页、时钟校正、身份修订等都会改变下架判断、经历边界与片区汇总；
  每次构建把这三类变化连同覆盖、归属、限制变化一起写入 version_change 统一解释。

## 检查与测试

检查资料字节、行数与字段形状（不生成业务事实）：

    python3 tools/inspect_source.py

执行资料检查与数据湖场景测试（真实下架 / 覆盖不足 / 成交后残留 / 身份歧义 /
时钟校正 / 补丁语义 / 版本不可变）：

    python3 -m unittest discover -s tests -v

检查 Python 文件能否编译：

    python3 -m compileall -q tools tests lake

资料格式和来源语义见 docs/source-contract.md；data 内均为本地合成记录。
