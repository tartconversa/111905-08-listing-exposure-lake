# 来源资料约定

授权渠道挂牌导出、覆盖声明、匿名身份及成交限制。

渠道键为channel/listing_id，门店房号只在reuse_epoch内有效。时间为带时区ISO8601；日期区间左闭右开。

exports声明channel、region、batch和expected_pages，page_receipts表示已收到页面，complete=true且所有预期页到达才证明对应范围完整。局部抓取的缺席不构成下架。

observations.kind为full或patch；patch中没有的键表示未提供，显式null表示清空该字段。observed_at是来源时钟时间，clock_adjustments在所列批次范围加offset_seconds才是校正后的时间。

identity凭证只对valid_from/to内的广告和房屋关系有效，candidates非唯一时不能默认选择第一项；supersedes只替代对应凭证。transactions对house_id的signed/cancelled/blocked/released限定可交易期，不能替代渠道观察。

## 数据载体

每类资料位于 data 中同名的 NDJSON 文件，一行一个原始对象；字段和基本类型见 data/schema.json。data/manifest.json 固定资料行数与字节哈希。资料中的缺失、歧义、未完整交付及冲突声明属于来源事实，不由资料检查程序推断业务结论。

### exports

清单范围声明。字段：channel (string)、region (string)、batch (string)、expected_pages (array)、complete (boolean)、observed_at (string)。

### page_receipts

页面交付覆盖。字段：channel (string)、batch (string)、page (integer)、received (string)。

### observations

原始广告观察与补丁。字段：record_id (string)、channel (string)、listing_id (string)、reuse_epoch (string)、batch (string)、page (integer)、kind (string)、observed_at (string)、fields (object)。

### clock_adjustments

来源时钟校正。字段：adjustment_id (string)、channel (string)、batches (array)、offset_seconds (integer)、received (string)。

### identity

房屋关联凭证。字段：proof_id (string)、channel (string)、listing_id (string)、reuse_epoch (string)、candidates (array)、valid_from (string)、valid_to (any)、received (string)、supersedes (any)。

### transactions

成交及交易限制。字段：transaction_id (string)、house_id (string)、kind (string)、effective (string)、received (string)、reference (any)。

