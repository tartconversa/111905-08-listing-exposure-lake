"""挂牌经历数据湖的场景测试。

覆盖契约要求的关键区分：
- 真实下架（范围完整后缺席）vs 覆盖不足（失败页 => 未知，不是零天也不是下架）
- 成交/限制截断可交易经历，成交后仍在的广告 => 渠道滞留曝光
- 撤销成交仅从有根据的生效期恢复，不回填未观察空白
- 身份凭证的适用期、supersedes、多候选歧义与门店房号复用隔离
- 来源时钟校正跨日/跨月移动观察时间
- 补丁语义：未提供 != 清空
- 数据版本：变化统一解释，旧版本不可变
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lake import build_version, connect, ingest_dir, init_schema, query  # noqa: E402

TZ = "+08:00"


def ts(day, clock="10:00:00"):
    return f"2026-11-{day:02d}T{clock}{TZ}"


def export(channel, region, batch, pages, observed_at, complete=True):
    return {"channel": channel, "region": region, "batch": batch,
            "expected_pages": pages, "complete": complete, "observed_at": observed_at}


def receipt(channel, batch, page, received=None):
    return {"channel": channel, "batch": batch, "page": page,
            "received": received or f"2026-11-08T09:00:00{TZ}"}


def obs(record, channel, listing, epoch, batch, observed_at, kind="full", fields=None, page=1):
    return {"record_id": record, "channel": channel, "listing_id": listing, "reuse_epoch": epoch,
            "batch": batch, "page": page, "kind": kind, "observed_at": observed_at,
            "fields": fields if fields is not None else {}}


def proof(pid, channel, listing, epoch, candidates, valid_from="2026-10-01",
          valid_to=None, supersedes=None, received="2026-11-02"):
    return {"proof_id": pid, "channel": channel, "listing_id": listing, "reuse_epoch": epoch,
            "candidates": candidates, "valid_from": valid_from, "valid_to": valid_to,
            "received": received, "supersedes": supersedes}


def tx(tid, house, kind, effective, reference=None, received=None):
    return {"transaction_id": tid, "house_id": house, "kind": kind, "effective": effective,
            "received": received or f"2026-11-08T09:00:00{TZ}", "reference": reference}


class LakeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "lake.duckdb")
        self.con = init_schema(connect(self.db_path))
        self._dirs = 0

    def ingest(self, **datasets):
        """把给定数据集写成一个资料目录并导入（增量导入写新目录即可）。"""
        self._dirs += 1
        directory = Path(self.tmp.name) / f"src{self._dirs}"
        directory.mkdir()
        for name, rows in datasets.items():
            content = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
            (directory / f"{name}.ndjson").write_text(content, encoding="utf-8")
        return ingest_dir(self.con, directory)

    def build(self, note=None):
        return build_version(self.con, note=note)

    def house(self, house_id, version=None):
        return query.house_experience(self.con, house_id, version)

    def region(self, region, month, version=None):
        return query.region_month(self.con, region, month, version)

    def seg_days(self, experience, kind):
        return sum(s["days"] for s in experience["segments"] if s["kind"] == kind)


class DelistingVsCoverageTest(LakeCase):
    """真实下架：范围完整后缺席；覆盖不足：失败页 => 未知；补页后翻转并留痕。"""

    def _base(self):
        self.ingest(
            exports=[
                export("A", "west", "a1", [1], ts(1)),
                export("A", "west", "a2", [1], ts(2)),
                export("C", "east", "c1", [1, 2], ts(1)),
                export("C", "east", "c2", [1, 2], ts(2)),
            ],
            page_receipts=[
                receipt("A", "a1", 1), receipt("A", "a2", 1),
                receipt("C", "c1", 1), receipt("C", "c1", 2),
                receipt("C", "c2", 1),  # c2 缺第 2 页 => 范围不完整
            ],
            observations=[
                obs("O1", "A", "L1", "r1", "a1", ts(1), fields={"price_cents": 100}),
                obs("O2", "C", "L2", "r1", "c1", ts(1)),
            ],
            identity=[
                proof("ID1", "A", "L1", "r1", ["H1"]),
                proof("ID2", "C", "L2", "r1", ["H2"]),
            ],
        )

    def test_incomplete_coverage_is_unknown_not_delisted(self):
        self._base()
        result = self.build()
        self.assertEqual(result["version_id"], 1)

        h1 = self.house("H1")
        # a2 完整且缺席 => 确认下架
        self.assertEqual(h1["totals"]["listed_tradable_days"], 1)
        self.assertEqual(h1["totals"]["delisted_days"], 1)
        self.assertEqual(h1["totals"]["unknown_days"], 0)

        h2 = self.house("H2")
        # c2 缺页 => 第 2 天是未知，既不是下架也不是零天挂牌
        self.assertEqual(h2["totals"]["listed_tradable_days"], 1)
        self.assertEqual(h2["totals"]["delisted_days"], 0)
        self.assertEqual(h2["totals"]["unknown_days"], 1)
        unknown = [s for s in h2["segments"] if s["kind"] == "UNKNOWN"]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["start_day"], "2026-11-02")
        self.assertEqual(unknown[0]["sources"][0]["listing_id"], "L2")

        east = self.region("east", "2026-11")
        self.assertEqual(east["houses"][0]["unknown_days"], 1)
        self.assertEqual(east["houses"][0]["listing_days"], 1)

    def test_backfill_flips_unknown_to_delisted_and_old_version_is_stable(self):
        self._base()
        self.build()
        before = self.house("H2", version=1)
        east_before = self.region("east", "2026-11", version=1)

        # 失败页补到达 => c2 范围完整 => 缺席转为确认下架
        self.ingest(page_receipts=[receipt("C", "c2", 2, received=f"2026-11-09T09:00:00{TZ}")])
        result = self.build(note="补齐 c2 第 2 页")
        self.assertEqual(result["version_id"], 2)

        after = self.house("H2")
        self.assertEqual(after["totals"]["unknown_days"], 0)
        self.assertEqual(after["totals"]["delisted_days"], 1)

        # 变化说明同时覆盖：覆盖完整性、下架判断、经历边界、片区汇总
        changes = query.version_changes(self.con, 2)["changes"]
        self.assertIn("coverage", changes)
        self.assertIn("delisting", changes)
        self.assertIn("house_segments", changes)
        self.assertIn("region_month_house", changes)
        delisting = changes["delisting"]
        self.assertTrue(any(
            d["subject"] == "C/L2/r1@2026-11-02"
            and d["detail"]["before"] == "UNKNOWN" and d["detail"]["after"] == "ABSENT"
            for d in delisting
        ))

        # 已引用的旧版本不可变
        self.assertEqual(self.house("H2", version=1), before)
        self.assertEqual(self.region("east", "2026-11", version=1), east_before)

        # 输入未再变化时重建是 noop，不产生新版本
        self.assertEqual(self.build()["status"], "noop")
        self.assertEqual(len(query.list_versions(self.con)), 2)


class ResidualExposureTest(LakeCase):
    """成交截断可交易经历；成交后仍在的广告 => 渠道滞留；撤销不回填空白。"""

    def _daily_channel(self, obs_days, incomplete_days=()):
        exports, receipts, observations = [], [], []
        for day in range(1, 7):
            batch = f"d{day}"
            pages = [1, 2] if day in incomplete_days else [1]
            exports.append(export("A", "west", batch, pages, ts(day, "08:00:00")))
            receipts.append(receipt("A", batch, 1))
            if day in incomplete_days:
                pass  # 第 2 页缺失 => 范围不完整
            if day in obs_days:
                observations.append(obs(f"O{day}", "A", "L1", "r1", batch, ts(day, "08:00:00")))
        return exports, receipts, observations

    def test_residual_after_sale_and_grounded_cancel(self):
        exports, receipts, observations = self._daily_channel(obs_days={1, 2, 3, 4, 5, 6})
        self.ingest(
            exports=exports, page_receipts=receipts, observations=observations,
            identity=[proof("ID1", "A", "L1", "r1", ["H1"])],
            transactions=[
                tx("SALE", "H1", "signed", ts(3, "12:00:00")),
                tx("CANCEL", "H1", "cancelled", ts(6, "00:00:00"), reference="SALE"),
                tx("BOGUS", "H1", "cancelled", ts(6, "00:00:00"), reference="NOPE"),
            ],
        )
        self.build()
        h1 = self.house("H1")
        # 11-03 的观察在 08:00（成交 12:00 前）=> 当天仍算可交易挂牌
        self.assertEqual(h1["totals"]["listed_tradable_days"], 4)  # 1,2,3,6
        self.assertEqual(h1["totals"]["residual_days"], 2)         # 4,5 成交后仍在
        residual = [s for s in h1["segments"] if s["kind"] == "RESIDUAL"]
        self.assertEqual(len(residual), 1)
        self.assertEqual((residual[0]["start_day"], residual[0]["end_day"]), ("2026-11-04", "2026-11-06"))
        self.assertEqual(residual[0]["sources"][0]["channel"], "A")

        # 限制窗口：SALE 被 CANCEL 从 11-06 起有据解除
        [restriction] = h1["restrictions"]
        self.assertEqual(restriction["transaction_id"], "SALE")
        self.assertEqual(restriction["end_at"], ts(6, "00:00:00"))
        self.assertEqual(restriction["lifted_by"], "CANCEL")

        # 无根据的撤销只进注记，不改变事实
        notes = query.notes(self.con)["notes"]
        self.assertTrue(any(n["category"] == "ungrounded_lift" and n["subject"] == "BOGUS" for n in notes))

        west = self.region("west", "2026-11")
        self.assertEqual(west["houses"][0]["listing_days"], 4)
        self.assertEqual(west["houses"][0]["residual_days"], 2)
        self.assertEqual(west["channels"][0]["residual_days"], 2)

    def test_cancel_does_not_backfill_unobserved_gap(self):
        # 第 4、5 天渠道覆盖失败且无观察：撤销成交后仍保持未知，不得当作挂牌
        exports, receipts, observations = self._daily_channel(
            obs_days={1, 2, 3, 6}, incomplete_days={4, 5})
        self.ingest(
            exports=exports, page_receipts=receipts, observations=observations,
            identity=[proof("ID1", "A", "L1", "r1", ["H1"])],
            transactions=[
                tx("SALE", "H1", "signed", ts(3, "12:00:00")),
                tx("CANCEL", "H1", "cancelled", ts(6, "00:00:00"), reference="SALE"),
            ],
        )
        self.build()
        h1 = self.house("H1")
        self.assertEqual(h1["totals"]["listed_tradable_days"], 4)  # 1,2,3,6
        self.assertEqual(h1["totals"]["residual_days"], 0)         # 无成交后观察
        self.assertEqual(h1["totals"]["unknown_days"], 2)          # 4,5 保持未知


class IdentityTest(LakeCase):
    """同一房屋跨渠道去重；凭证修订为歧义后房屋级不武断归属；epoch 隔离。"""

    def _base(self):
        self.ingest(
            exports=[
                export("A", "west", "a1", [1], ts(1)), export("A", "west", "a2", [1], ts(2)),
                export("B", "west", "b1", [1], ts(1, "11:00:00")),
                export("B", "west", "b2", [1], ts(2, "11:00:00")),
                export("B", "west", "b3", [1], ts(3, "11:00:00")),
            ],
            page_receipts=[receipt(c, b, 1) for c, b in
                           [("A", "a1"), ("A", "a2"), ("B", "b1"), ("B", "b2"), ("B", "b3")]],
            observations=[
                obs("OA1", "A", "L1", "r1", "a1", ts(1), fields={"house_code": "X1"}),
                obs("OA2", "A", "L1", "r1", "a2", ts(2), fields={"house_code": "X1"}),
                obs("OB1", "B", "L2", "r1", "b1", ts(1, "11:00:00")),
                obs("OB2", "B", "L2", "r1", "b2", ts(2, "11:00:00")),
                obs("OB3", "B", "L2", "r1", "b3", ts(3, "11:00:00")),
                # 门店房号复用：同 listing_id 不同 epoch 是另一条广告
                obs("OA3", "A", "L1", "r2", "a2", ts(2, "12:00:00"), fields={"house_code": "X1"}),
            ],
            identity=[
                proof("ID1", "A", "L1", "r1", ["H1"]),
                proof("ID2", "B", "L2", "r1", ["H1"]),
                proof("ID9", "A", "L1", "r2", ["H9"]),
            ],
        )

    def test_same_house_across_channels_not_double_counted(self):
        self._base()
        self.build()
        h1 = self.house("H1")
        # 两条广告同天同房屋 => 只计一天；B 渠道 11-03 独有 => 共 3 天
        self.assertEqual(h1["totals"]["listed_tradable_days"], 3)
        west = self.region("west", "2026-11")
        houses = {h["house_id"]: h for h in west["houses"]}
        self.assertEqual(houses["H1"]["listing_days"], 3)
        self.assertEqual(houses["H9"]["listing_days"], 1)  # 复用房号的另一条广告独立计
        self.assertEqual(west["totals"]["listing_days"], 4)
        channels = {c["channel"]: c for c in west["channels"]}
        self.assertEqual(channels["A"]["house_exposure_days"], 3)  # H1 两天 + H9 一天
        self.assertEqual(channels["B"]["house_exposure_days"], 3)

        # 房号复用不合并：A/L1/r2 属于 H9，与 H1 各自独立
        h9 = self.house("H9")
        self.assertEqual(h9["totals"]["listed_tradable_days"], 1)
        self.assertNotEqual(
            {a["reuse_epoch"] for a in h1["linked_ads"]},
            {a["reuse_epoch"] for a in h9["linked_ads"]},
        )

    def test_identity_revision_to_ambiguous(self):
        self._base()
        self.build()
        h1_v1 = self.house("H1", version=1)
        self.assertEqual(h1_v1["totals"]["listed_tradable_days"], 3)

        # 修订：B/L2/r1 改为候选 [H2, H3] 并替代 ID2
        self.ingest(identity=[
            proof("ID3", "B", "L2", "r1", ["H2", "H3"], supersedes="ID2", received="2026-11-10"),
        ])
        self.build(note="B/L2 身份修订为歧义")
        h1_v2 = self.house("H1")
        # 房屋级汇总不再包含 L2 独有的 11-03；A/L1 的归属不受 supersedes 影响
        self.assertEqual(h1_v2["totals"]["listed_tradable_days"], 2)
        self.assertEqual([a["listing_id"] for a in h1_v2["linked_ads"]], ["L1"])

        # 广告级资料仍然可查，且标记为歧义
        ad = query.ad_detail(self.con, "B", "L2", "r1")
        self.assertEqual(len(ad["observations"]), 3)
        self.assertTrue(all(a["status"] == "AMBIGUOUS" for a in ad["attribution"]))
        self.assertEqual(ad["attribution"][0]["candidates"], ["H2", "H3"])

        # 渠道 B 的曝光保留在广告级，房屋级曝光归零、歧义曝光单列
        west = self.region("west", "2026-11")
        channels = {c["channel"]: c for c in west["channels"]}
        self.assertEqual(channels["B"]["house_exposure_days"], 0)
        self.assertEqual(channels["B"]["ad_exposure_days"], 3)
        self.assertEqual(channels["B"]["ambiguous_ad_exposure_days"], 3)
        houses = {h["house_id"]: h for h in west["houses"]}
        self.assertEqual(houses["H1"]["listing_days"], 2)
        self.assertEqual(west["totals"]["listing_days"], 3)  # H1 两天 + H9 一天

        # 变化说明包含归属与片区汇总
        changes = query.version_changes(self.con, 2)["changes"]
        self.assertIn("attribution", changes)
        self.assertIn("region_month_channel", changes)

        # 旧版本仍然是修订前的结论
        self.assertEqual(self.house("H1", version=1), h1_v1)


class ClockAdjustmentTest(LakeCase):
    """来源时钟校正把批次观察时间推到另一日（跨月），汇总随之迁移。"""

    def test_adjustment_moves_observation_across_month(self):
        self.ingest(
            exports=[export("A", "west", "a1", [1], f"2026-11-30T23:30:00{TZ}")],
            page_receipts=[receipt("A", "a1", 1)],
            observations=[obs("O1", "A", "L1", "r1", "a1", f"2026-11-30T23:30:00{TZ}")],
            identity=[proof("ID1", "A", "L1", "r1", ["H1"])],
        )
        self.build()
        self.assertEqual(self.region("west", "2026-11")["totals"]["listing_days"], 1)
        self.assertEqual(self.region("west", "2026-12")["totals"]["listing_days"], 0)

        self.ingest(clock_adjustments=[{
            "adjustment_id": "CLK1", "channel": "A", "batches": ["a1"],
            "offset_seconds": 3600, "received": f"2026-12-02T09:00:00{TZ}",
        }])
        self.build(note="校正 A 渠道时钟 +1h")
        # 23:30 + 1h => 次日 00:30，挂牌日从 11-30 移到 12-01
        self.assertEqual(self.region("west", "2026-11")["totals"]["listing_days"], 0)
        december = self.region("west", "2026-12")
        self.assertEqual(december["totals"]["listing_days"], 1)
        self.assertEqual(december["channels"][0]["house_exposure_days"], 1)

        changes = query.version_changes(self.con, 2)["changes"]
        self.assertIn("coverage", changes)
        moved = [c for c in changes["region_month_house"] if c["subject"].startswith("west/")]
        self.assertTrue(any("2026-11" in c["subject"] for c in moved))
        self.assertTrue(any("2026-12" in c["subject"] for c in moved))


class PatchSemanticsTest(LakeCase):
    """patch 缺键 = 未提供；显式 null = 清空；full 后缺键 = 隐式清空。"""

    def test_patch_and_full_semantics(self):
        self.ingest(
            exports=[export("A", "west", b, [1], ts(d)) for b, d in
                     [("a1", 1), ("a2", 2), ("a3", 3), ("a4", 4)]],
            page_receipts=[receipt("A", b, 1) for b in ["a1", "a2", "a3", "a4"]],
            observations=[
                obs("O1", "A", "L1", "r1", "a1", ts(1),
                    fields={"price_cents": 100, "description": "x"}),
                obs("O2", "A", "L1", "r1", "a2", ts(2), kind="patch",
                    fields={"description": None}),
                obs("O3", "A", "L1", "r1", "a3", ts(3), kind="patch",
                    fields={"note": "hello"}),
                obs("O4", "A", "L1", "r1", "a4", ts(4),
                    fields={"price_cents": 200}),
            ],
            identity=[proof("ID1", "A", "L1", "r1", ["H1"])],
        )
        self.build()
        ad = query.ad_detail(self.con, "A", "L1", "r1")
        state = ad["field_state"]
        # 未给价格不清空；O4 的 full 把价格改为 200
        self.assertEqual(state["price_cents"], {"status": "SET", "value": 200})
        # 显式 null => 清空
        self.assertEqual(state["description"], {"status": "CLEARED", "value": None})
        # full 快照中缺失的 note => 隐式清空
        self.assertEqual(state["note"], {"status": "CLEARED", "value": None})

        # 回看：O2 之后、O4 之前价格仍是 100
        ad_mid = query.ad_detail(self.con, "A", "L1", "r1", at=ts(3, "23:59:59"))
        self.assertEqual(ad_mid["field_state"]["price_cents"]["value"], 100)
        self.assertEqual(ad_mid["field_state"]["note"], {"status": "SET", "value": "hello"})
        # 从未提供的字段不出现在状态中（与清空不同）
        self.assertNotIn("never_provided", ad["field_state"])


class BaseDataEndToEndTest(LakeCase):
    """仓库自带合成资料的端到端断言（含时钟校正、歧义修订、成交撤销）。"""

    def test_repository_data(self):
        ingest_dir(self.con, ROOT / "data")
        result = self.build()
        self.assertEqual(result["version_id"], 1)

        h1 = self.house("H1")
        # CLK1 把 a1/a2 推后 1 小时：O1 -> 11-02 00:30，O3 -> 11-03 00:30
        self.assertEqual(h1["totals"], {
            "listed_tradable_days": 2, "residual_days": 0,
            "unknown_days": 0, "delisted_days": 0,
        })
        [seg] = h1["segments"]
        self.assertEqual((seg["kind"], seg["start_day"], seg["end_day"]),
                         ("LISTED_TRADABLE", "2026-11-02", "2026-11-04"))
        # 成交 11-03 12:00 截断，11-05 00:00 有据撤销
        [restriction] = h1["restrictions"]
        self.assertEqual((restriction["transaction_id"], restriction["lifted_by"]), ("SALE1", "CANCEL1"))
        self.assertEqual(restriction["end_at"], "2026-11-05T00:00:00+08:00")

        # B/L9 被 ID3 修订为歧义：房屋级不归属，广告级可查
        west = self.region("west", "2026-11")
        channels = {c["channel"]: c for c in west["channels"]}
        self.assertEqual(channels["A"]["house_exposure_days"], 2)
        self.assertEqual(channels["B"]["house_exposure_days"], 0)
        self.assertEqual(channels["B"]["ambiguous_ad_exposure_days"], 1)
        ad = query.ad_detail(self.con, "B", "L9", "r7")
        self.assertEqual(ad["attribution"][0]["status"], "AMBIGUOUS")
        self.assertEqual(ad["attribution"][0]["candidates"], ["H2", "H3"])

        # L1 的 patch：description 清空，价格保持
        ad_l1 = query.ad_detail(self.con, "A", "L1", "r1")
        self.assertEqual(ad_l1["field_state"]["price_cents"], {"status": "SET", "value": 180000000})
        self.assertEqual(ad_l1["field_state"]["description"], {"status": "CLEARED", "value": None})


if __name__ == "__main__":
    unittest.main()
