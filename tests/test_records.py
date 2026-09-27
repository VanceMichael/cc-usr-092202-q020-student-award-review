import copy
import json
import unittest
from decimal import Decimal
from pathlib import Path

from src.records import load_records, reconstruct_ranking, validate_domain

FIXTURE = Path("fixtures/context.json")


def fresh():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def by_id(rows, row_id):
    return next(r for r in rows if r.get("id") == row_id)


def expect_error(testcase, data, fragment):
    with testcase.assertRaises(ValueError) as ctx:
        validate_domain(data)
    testcase.assertIn(fragment, str(ctx.exception))


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self.data = fresh()

    # ------------------------------------------------------------ 正向

    def test_public_fixture_loads(self):
        value = load_records(FIXTURE)
        self.assertEqual(value["domain"], "student-award-review")
        self.assertEqual(len(value["candidates"]), 5)
        self.assertEqual({s["id"] for s in value["stages"]}, {"S1", "S2", "S3"})

    def test_six_material_kinds_each_verified_on_own_track(self):
        tracks = {}
        for v in self.data["verifications"]:
            material = by_id(self.data["materials"], v["material_id"])
            tracks.setdefault((v["material_id"], material["kind"]), v["verifier_kind"])
        expected = {"nomination", "transcript", "project_role",
                    "service_hours", "recommendation", "public_work"}
        self.assertEqual({m["kind"] for m in self.data["materials"]}, expected)

    def test_reconstruction_matches_published_results(self):
        result = reconstruct_ranking(self.data)
        declared = {e["candidate_id"]: e for e in self.data["results"]["entries"]}
        self.assertEqual([e["candidate_id"] for e in result["entries"]],
                         [e["candidate_id"] for e in sorted(
                             self.data["results"]["entries"], key=lambda e: e["rank"])])
        for entry in result["entries"]:
            pub = declared[entry["candidate_id"]]
            self.assertEqual(entry["rank"], pub["rank"])
            self.assertEqual(Decimal(str(pub["composite"])), entry["composite"])
            # 每個名次都能重建：有效評委、凍結規則、證據齊全
            for sid in ("S1", "S2", "S3"):
                view = entry["stages"][sid]
                self.assertGreaterEqual(len(view["judges"]), 3)
                self.assertAlmostEqual(
                    sum(view["rubric"]["weights"].values()), 1.0)
                self.assertTrue(view["fact_ids"])

    def test_extreme_gaps_reviewed_but_nobody_eliminated(self):
        self.assertEqual({(r["stage_id"], r["candidate_id"])
                          for r in self.data["score_reviews"]},
                         {("S2", "C003"), ("S3", "C004")})
        for review in self.data["score_reviews"]:
            self.assertEqual(review["elimination_effect"], "none")
            self.assertEqual(review["outcome"], "upheld")
        reviewed = {r["candidate_id"] for r in self.data["score_reviews"]}
        finalists = {e["candidate_id"] for e in self.data["results"]["entries"]}
        self.assertTrue(reviewed <= finalists)

    def test_minor_and_sensitive_visibility(self):
        # C005 為未成年人：敏感事實完整內容僅初審可見，下游只見遮蔽摘要
        sensitive = by_id(self.data["confirmed_facts"], "F-0503")
        redacted = by_id(self.data["confirmed_facts"], "F-0503R")
        self.assertEqual(sensitive["visible_in_stages"], ["S1"])
        self.assertEqual(sensitive["publication_mode"], "none")
        self.assertEqual(redacted["derived_from_fact"], "F-0503")
        # 面談、終審、獲獎理由只能引用遮蔽版
        for sid in ("S2", "S3"):
            evidence = next(e for e in self.data["stage_evidence"]
                            if e["stage_id"] == sid and e["candidate_id"] == "C005")
            self.assertIn("F-0503R", evidence["fact_ids"])
            self.assertNotIn("F-0503", evidence["fact_ids"])
        c005 = next(e for e in self.data["results"]["entries"]
                    if e["candidate_id"] == "C005")
        self.assertIn("F-0503R", c005["reason_fact_ids"])
        self.assertNotIn("F-0503", c005["reason_fact_ids"])

    def test_withdrawn_work_leaves_audit_only(self):
        work = by_id(self.data["materials"], "M-0206")
        self.assertEqual(work["versions"][0]["status"], "superseded")
        self.assertEqual(work["versions"][-1]["status"], "withdrawn")
        used_facts = {f for e in self.data["stage_evidence"] for f in e["fact_ids"]}
        self.assertFalse(any(f["material_id"] == "M-0206" for f in
                             self.data["confirmed_facts"]))
        audit = next(r for r in self.data["retention_schedule"]
                     if r["class"] == "withdrawn_audit" and r["candidate_id"] == "C002")
        self.assertEqual(audit["material_id"], "M-0206")

    def test_retention_dates(self):
        unsuccessful = by_id(self.data["retention_schedule"], "RT-C004")
        self.assertEqual(unsuccessful["retain_until"], "2026-11-25")  # 發布後六個月
        archive = by_id(self.data["retention_schedule"], "RT-C001")
        self.assertEqual(archive["retain_until"], "2031-05-25")        # 獲獎者封存五年

    # ------------------------------------------------------------ 變異反例

    def test_mut_superseded_version_must_be_kept(self):
        by_id(self.data["materials"], "M-0104")["versions"][0]["status"] = "active"
        expect_error(self, self.data, "superseded")

    def test_mut_withdrawn_version_cannot_become_fact(self):
        self.data["confirmed_facts"].append({
            "id": "F-BAD-WD", "candidate_id": "C002", "material_id": "M-0206",
            "version_no": 2, "confirmed_at": "2026-03-30T10:00:00+08:00",
            "sensitive": False, "redacted": False, "visible_in_stages": ["S2"],
            "publication_mode": "full", "summary": "撤回作品不應成事實"})
        expect_error(self, self.data, "現行有效版本")

    def test_mut_unconfirmed_active_version_rejected(self):
        by_id(self.data["verifications"], "V-0406-2")["status"] = "unverified"
        expect_error(self, self.data, "所屬軌道")

    def test_mut_wrong_verification_track(self):
        by_id(self.data["verifications"], "V-0304-2")["verifier_kind"] = "institution"
        expect_error(self, self.data, "驗真軌道")

    def test_mut_initial_material_after_deadline(self):
        by_id(self.data["materials"], "M-0406")["versions"][0]["submitted_at"] = \
            "2026-03-30T10:00:00+08:00"
        expect_error(self, self.data, "收取截止")

    def test_mut_coi_judge_cannot_score_conflicted_candidate(self):
        # J2 已就 C002 申報回避；把 C002 的一張面談表改成 J2
        by_id(self.data["score_sheets"], "SH-S2-05")["judge_id"] = "J2"
        expect_error(self, self.data, "利益回避")

    def test_mut_coi_declared_too_late(self):
        self.data["coi_declarations"][0]["declared_at"] = "2026-04-02T10:00:00+08:00"
        expect_error(self, self.data, "遲於首階段")

    def test_mut_screening_sheet_must_be_anonymous(self):
        by_id(self.data["score_sheets"], "SH-S1-01")["candidate_id"] = "C003"
        expect_error(self, self.data, "匿名")

    def test_mut_score_outside_stage_window(self):
        by_id(self.data["score_sheets"], "SH-S3-19")["recorded_at"] = \
            "2026-05-10T20:00:00+08:00"
        expect_error(self, self.data, "超出階段窗口")

    def test_mut_rubric_weights_must_sum_to_one(self):
        by_id(self.data["stages"], "S1")["rubric"]["weights"]["academic"] = 0.5
        expect_error(self, self.data, "合計為 1")

    def test_mut_anonymous_map_opened_before_freeze(self):
        self.data["anonymous_map"]["opened_at"] = "2026-04-13T10:00:00+08:00"
        expect_error(self, self.data, "啟封")

    def test_mut_post_freeze_fact_cannot_enter_screening(self):
        # F-0404 在 4 月 20 日才確認，初審 4 月 1 日凍結，不得進入初審證據包
        packet = next(e for e in self.data["stage_evidence"]
                      if e["stage_id"] == "S1" and e["candidate_id"] == "C004")
        packet["fact_ids"].append("F-0404")
        expect_error(self, self.data, "凍結時可見事實")

    def test_mut_sensitive_material_access_outside_consent(self):
        by_id(self.data["materials"], "M-0504")["access_scopes"].append("final")
        expect_error(self, self.data, "授權展示範圍")

    def test_mut_minor_without_guardian_consent(self):
        by_id(self.data["candidates"], "C002").pop("guardian_consent")
        expect_error(self, self.data, "家長同意書")

    def test_mut_minor_publication_without_scope(self):
        scopes = by_id(self.data["candidates"], "C002")["guardian_consent"]["scopes"]
        scopes.remove("publication:name_school_award")
        expect_error(self, self.data, "家長授權")

    def test_mut_missing_extreme_gap_review(self):
        self.data["score_reviews"] = [
            r for r in self.data["score_reviews"] if r["id"] != "RV-02"]
        expect_error(self, self.data, "覆核記錄")

    def test_mut_extreme_gap_must_not_remove_candidate(self):
        # C003 在面談觸發過極端分差覆核；若把她移出終審，必須被識別為變相淘汰
        s3 = by_id(self.data["stages"], "S3")
        s3["candidate_ids"].remove("C003")
        for sheet_id in ("SH-S3-09", "SH-S3-10", "SH-S3-11"):
            self.data["score_sheets"].remove(
                by_id(self.data["score_sheets"], sheet_id))
        expect_error(self, self.data, "極端分差")

    def test_mut_appeal_cannot_change_ranking_in_freeze(self):
        by_id(self.data["appeals"], "A-001")["ranking_changed"] = True
        expect_error(self, self.data, "凍結窗")

    def test_mut_appeal_outside_window(self):
        by_id(self.data["appeals"], "A-001")["filed_at"] = "2026-05-26T10:00:00+08:00"
        expect_error(self, self.data, "凍結窗")

    def test_mut_publication_before_freeze_window_ends(self):
        self.data["results"]["published_at"] = "2026-05-24T10:00:00+08:00"
        expect_error(self, self.data, "凍結窗")

    def test_mut_reason_cannot_cite_other_candidates_fact(self):
        c001 = next(e for e in self.data["results"]["entries"]
                    if e["candidate_id"] == "C001")
        c001["reason_fact_ids"].append("F-0201")
        expect_error(self, self.data, "不屬於")

    def test_mut_reason_cannot_cite_sensitive_fact(self):
        c005 = next(e for e in self.data["results"]["entries"]
                    if e["candidate_id"] == "C005")
        c005["reason_fact_ids"] = ["F-0501", "F-0502", "F-0503"]
        expect_error(self, self.data, "禁止公開")

    def test_mut_awardee_reason_requires_facts(self):
        c003 = next(e for e in self.data["results"]["entries"]
                    if e["candidate_id"] == "C003")
        c003["reason_fact_ids"] = []
        expect_error(self, self.data, "獲獎理由")

    def test_mut_unsuccessful_candidate_needs_disposal(self):
        self.data["retention_schedule"] = [
            r for r in self.data["retention_schedule"] if r["id"] != "RT-C004"]
        expect_error(self, self.data, "清理安排")

    def test_mut_withdrawn_material_needs_audit_retention(self):
        self.data["retention_schedule"] = [
            r for r in self.data["retention_schedule"] if r["id"] != "RT-WD-0206"]
        expect_error(self, self.data, "審計保留")

    def test_mut_snapshot_rubric_tampering_detected(self):
        snap = next(s for s in self.data["reconstruction"]["stage_snapshots"]
                    if s["stage_id"] == "S2")
        snap["rubric"]["weights"] = {"academic": 0.25, "leadership": 0.5,
                                     "service": 0.25}
        expect_error(self, self.data, "rubric")

    def test_mut_snapshot_judge_list_tampering_detected(self):
        snap = next(s for s in self.data["reconstruction"]["stage_snapshots"]
                    if s["stage_id"] == "S3")
        snap["panel_judges"].remove("J5")
        expect_error(self, self.data, "有效評委")

    def test_mut_published_composite_must_be_recomputable(self):
        c001 = next(e for e in self.data["results"]["entries"]
                    if e["candidate_id"] == "C001")
        c001["composite"] = 9.9
        expect_error(self, self.data, "綜合分無法重建")

    def test_mut_missing_required_key(self):
        self.data.pop("retention_schedule")
        with self.assertRaises(ValueError):
            validate_domain(self.data)


class SnapshotImmutabilityTest(unittest.TestCase):
    def test_validation_does_not_mutate_input(self):
        data = fresh()
        before = copy.deepcopy(data)
        validate_domain(data)
        self.assertEqual(before, data)


class SchemaContractTest(unittest.TestCase):
    def test_fixture_matches_json_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("未安裝 jsonschema，跳過契約校驗")
        schema = json.loads(Path("contracts/domain.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(fresh()))
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
