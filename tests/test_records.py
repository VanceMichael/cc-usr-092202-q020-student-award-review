import copy
import json
import unittest
from pathlib import Path

from src.records import load_records, validate_domain

FIXTURE = Path("fixtures/context.json")


def fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def expect_invalid(testcase, value, *fragments):
    with testcase.assertRaises(ValueError) as ctx:
        validate_domain(value)
    message = str(ctx.exception)
    for fragment in fragments:
        testcase.assertIn(fragment, message)


class PublicFixtureTest(unittest.TestCase):
    def test_fixture_loads_and_satisfies_all_invariants(self):
        value = load_records(FIXTURE)
        self.assertEqual(value["domain"], "student-award-review")
        self.assertGreaterEqual(value["version"], 2)
        self.assertEqual(len(value["candidates"]), 3)
        self.assertEqual({c["id"] for c in value["judges"]},
                         {"judge-01", "judge-02", "judge-03", "judge-04"})
        self.assertEqual([s["id"] for s in value["stages"]],
                         ["screen", "interview", "final"])

    def test_six_material_types_per_candidate_and_external_channels(self):
        value = load_records(FIXTURE)
        types = {"nomination", "transcript", "project_role",
                 "service_hours", "recommendation", "public_work"}
        channels = set()
        for cid in {c["id"] for c in value["candidates"]}:
            owned = [m for m in value["materials"] if m["candidate_id"] == cid]
            self.assertEqual({m["type"] for m in owned}, types)
            channels.update(m["verification"]["channel"] for m in owned)
        self.assertEqual(
            channels,
            {"nomination_authority", "registrar", "club", "ngo",
             "recommender", "public_link"},
        )


class VersionChainTest(unittest.TestCase):
    def test_supersedes_successor_must_be_reciprocal(self):
        value = fixture()
        value["materials"][0]["successor"] = None  # 断开 m-nom-01-v1 -> v2
        expect_invalid(self, value, "版本链")

    def test_old_versions_kept_but_hidden_from_panel_and_public(self):
        value = load_records(FIXTURE)
        withdrawn = next(m for m in value["materials"] if m["id"] == "m-rec-01-v1")
        self.assertIn("audit", withdrawn["visibility"])
        self.assertNotIn("screen", withdrawn["visibility"])

        mutated = fixture()
        next(m for m in mutated["materials"] if m["id"] == "m-rec-01-v1")[
            "visibility"] = ["audit", "screen"]
        expect_invalid(self, mutated, "不得向评委或公众展示")

    def test_confirmed_material_must_match_external_verification(self):
        value = fixture()
        next(m for m in value["materials"] if m["id"] == "m-pro-03-v2")[
            "verification"]["status"] = "disputed"
        expect_invalid(self, value, "外部验真状态")


class RecusalTest(unittest.TestCase):
    def test_conflict_declaration_must_match_recusal_list(self):
        value = fixture()
        value["judges"][0]["recused"] = []  # judge-01 申报冲突却未列入回避
        expect_invalid(self, value, "回避名单")

    def test_recused_judge_cannot_be_on_panel(self):
        value = fixture()
        value["stages"][0]["panels"][0]["judge_ids"].append("judge-01")
        expect_invalid(self, value, "应回避评委")

    def test_score_from_non_panel_judge_rejected(self):
        value = fixture()
        value["scores"][0]["judge_id"] = "judge-01"  # judge-01 对 cand-01 已回避
        expect_invalid(self, value, "有效面板")


class FrozenCriteriaTest(unittest.TestCase):
    def test_score_before_freeze_rejected(self):
        value = fixture()
        value["scores"][0]["scored_at"] = "2026-03-31"
        expect_invalid(self, value, "冻结之前")

    def test_each_stage_has_its_own_frozen_criteria(self):
        value = fixture()
        value["stages"][2]["criteria"]["version"] = "cv-screen-1"
        expect_invalid(self, value, "各自冻结")

    def test_wrong_criteria_version_on_score_rejected(self):
        value = fixture()
        value["scores"][0]["criteria_version"] = "cv-final-1"
        expect_invalid(self, value, "冻结版本")


class ExtremeGapReviewTest(unittest.TestCase):
    def test_extreme_gap_without_review_rejected(self):
        value = fixture()
        value["score_reviews"] = []
        expect_invalid(self, value, "极端分差复核")

    def test_review_can_never_eliminate(self):
        value = fixture()
        value["score_reviews"][0]["elimination"] = True
        expect_invalid(self, value, "淘汰")

    def test_sample_review_records_real_gap_and_retention(self):
        value = load_records(FIXTURE)
        review = value["score_reviews"][0]
        self.assertEqual(review["trigger"], "extreme_gap")
        self.assertGreater(review["gap_to_median"], review["threshold"])
        self.assertFalse(review["elimination"])
        self.assertEqual(review["outcome"], "retained")


class AppealFreezeTest(unittest.TestCase):
    def test_freeze_snapshot_must_match_then_current_evidence(self):
        value = fixture()
        value["appeals"][0]["freeze"]["material_versions"].remove("m-tra-02-v1")
        expect_invalid(self, value, "证据快照")

    def test_freeze_judges_must_match_panel(self):
        value = fixture()
        value["appeals"][0]["freeze"]["judge_ids"].remove("judge-04")
        expect_invalid(self, value, "评委名单")

    def test_freeze_moment_is_filing_moment(self):
        value = fixture()
        value["appeals"][0]["freeze"]["frozen_at"] = "2026-05-22"
        expect_invalid(self, value, "受理时刻")


class PublicationTest(unittest.TestCase):
    def test_rationale_only_cites_confirmed_facts(self):
        value = fixture()
        value["results"][0]["rationale"]["internal_refs"].append("m-rec-01-v1")
        expect_invalid(self, value, "未确认事实")

    def test_published_refs_require_publish_authorization(self):
        value = fixture()
        # cand-03 的敏感作品仅 final 可见，伪造为第一名公开理由
        value["results"][0]["rationale"]["published_refs"].append("m-pub-03-v1")
        value["results"][0]["rationale"]["internal_refs"].append("m-pub-03-v1")
        expect_invalid(self, value, "其他候选人", "未授权发布")

    def test_minor_winner_publication_is_award_only(self):
        value = load_records(FIXTURE)
        minor_result = next(r for r in value["results"]
                            if r["candidate_id"] == "cand-02")
        self.assertEqual(minor_result["public_detail_level"], "award_only")
        self.assertEqual(minor_result["rationale"]["published_refs"], [])

        mutated = fixture()
        mutated["results"][1]["public_detail_level"] = "full"
        expect_invalid(self, mutated, "授权")

    def test_sensitive_material_never_published(self):
        value = fixture()
        next(m for m in value["materials"] if m["id"] == "m-svc-03-v2")[
            "visibility"].append("published")
        expect_invalid(self, value, "敏感材料")


class ReconstructionTest(unittest.TestCase):
    def test_every_rank_has_complete_reconstruction_bundle(self):
        value = fixture()
        value["reconstructions"][0]["evidence_versions"].remove("m-rec-01-v1")
        expect_invalid(self, value, "重建包证据版本不完整")

    def test_reconstruction_includes_superseded_versions_and_judges(self):
        value = load_records(FIXTURE)
        rank2 = next(b for b in value["reconstructions"] if b["rank"] == 2)
        self.assertIn("m-tra-02-v1", rank2["evidence_versions"])
        self.assertIn("appeal-01", rank2["appeal_freeze_ids"])
        self.assertEqual(rank2["eligible_judges"]["interview"],
                         ["judge-01", "judge-03", "judge-04"])

    def test_missing_bundle_for_rank_rejected(self):
        value = fixture()
        value["reconstructions"] = [
            b for b in value["reconstructions"] if b["rank"] != 1
        ]
        expect_invalid(self, value, "证据重建包")


class RetentionTest(unittest.TestCase):
    def test_unsuccessful_candidate_has_dated_cleanup(self):
        value = load_records(FIXTURE)
        cleanup = value["retention"]["unsuccessful"][0]
        self.assertEqual(cleanup["candidate_id"], "cand-03")
        self.assertEqual(cleanup["purge_after"], "2026-12-20")  # 公布后 183 天
        self.assertIn("anonymized", cleanup["disposition"])

    def test_cleanup_without_schedule_rejected(self):
        value = fixture()
        value["retention"]["unsuccessful"] = []
        expect_invalid(self, value, "到期清理安排")

    def test_cleanup_list_covers_all_versions_including_old_ones(self):
        value = fixture()
        value["retention"]["unsuccessful"][0]["material_ids"].remove("m-pro-03-v1")
        expect_invalid(self, value, "全部材料版本")


class MinorConsentTest(unittest.TestCase):
    def test_minor_requires_guardian_countersign(self):
        value = fixture()
        value["candidates"][1]["consent"]["guardian_countersign"] = None
        expect_invalid(self, value, "监护人联签")


class AggregationTest(unittest.TestCase):
    def test_panel_means_and_weighted_total_match_scores(self):
        value = load_records(FIXTURE)
        totals = {r["candidate_id"]: r["weighted_total"]
                  for r in value["aggregated_results"]}
        self.assertAlmostEqual(totals["cand-01"], 88.47, places=2)
        self.assertAlmostEqual(totals["cand-02"], 81.73, places=2)
        self.assertAlmostEqual(totals["cand-03"], 77.67, places=2)

    def test_tampered_mean_detected(self):
        value = fixture()
        value["aggregated_results"][0]["panel_means"]["screen"] = 99.0
        expect_invalid(self, value, "面板均值")


if __name__ == "__main__":
    unittest.main()
