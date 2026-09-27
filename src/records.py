"""讀取並檢查共享領域資料，並可從評分表重建最終名次。

本模組不依賴第三方庫：JSON Schema 負責形狀約束，這裡負責跨記錄的
語義不變量，例如版本留存、分軌驗真、利益回避、凍結評分、申訴凍結、
授權展示、保留期限以及「任意名次皆可重建」。
"""

import json
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

REQUIRED_KEYS = {
    "domain", "version", "sample_id", "actors", "facts", "constraints",
    "settings", "institutions", "candidates", "materials", "verifications",
    "confirmed_facts", "judges", "coi_declarations", "stages", "anonymous_map",
    "stage_evidence", "score_sheets", "score_reviews", "appeals",
    "stage_results", "results", "reconstruction", "retention_schedule", "records",
}

STAGE_IDS = ("S1", "S2", "S3")
DIMENSIONS = ("academic", "leadership", "service")

# 六類材料各自的驗真軌道：材料類型 -> 容許的驗證者類型
VERIFIER_TRACK = {
    "nomination": "secretariat",
    "transcript": "institution",
    "project_role": "club",
    "service_hours": "ngo",
    "recommendation": "nominator",
    "public_work": "public_registry",
}


def load_records(path: Path) -> dict:
    """返回結構完整、通過全部語義不變量檢查的領域資料。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    validate_domain(value)
    return value


def validate_domain(value: dict) -> None:
    """檢查領域資料的跨記錄不變量，發現問題時拋出 ValueError。"""
    missing = REQUIRED_KEYS - value.keys()
    if missing:
        raise ValueError(f"領域資料缺少必要字段：{sorted(missing)}")
    if value["domain"] != "student-award-review":
        raise ValueError("領域標識不正確")
    if value["version"] < 1 or len(value["actors"]) < 2:
        raise ValueError("領域資料內容不完整")

    ctx = _Index(value)
    _check_identifiers(ctx)
    _check_candidates(ctx)
    _check_materials_and_verifications(ctx)
    _check_facts(ctx)
    _check_judges_and_coi(ctx)
    _check_stages_and_rubrics(ctx)
    _check_score_sheets(ctx)
    _check_evidence_visibility(ctx)
    _check_extreme_gaps(ctx)
    _check_appeals_and_publication(ctx)
    _check_results(ctx)
    _check_retention(ctx)
    _check_reconstruction(ctx)


# ---------------------------------------------------------------- 索引

class _Index:
    """把各集合建成查找索引，供不變量檢查複用。"""

    def __init__(self, value: dict):
        self.v = value
        self.settings = value["settings"]
        self.institutions = {x["id"]: x for x in value["institutions"]}
        self.candidates = {x["id"]: x for x in value["candidates"]}
        self.materials = {x["id"]: x for x in value["materials"]}
        self.verifications = {x["id"]: x for x in value["verifications"]}
        self.facts = {x["id"]: x for x in value["confirmed_facts"]}
        self.judges = {x["id"]: x for x in value["judges"]}
        self.stages = {x["id"]: x for x in value["stages"]}
        self.sheets = value["score_sheets"]
        self.reviews = value["score_reviews"]
        self.appeals = value["appeals"]
        self.packet_to_candidate = {
            e["packet_code"]: e["candidate_id"] for e in value["anonymous_map"]["entries"]
        }
        # (material_id, version_no) -> 版本記錄
        self.version = {}
        for m in value["materials"]:
            for ver in m["versions"]:
                self.version[(m["id"], ver["version_no"])] = ver
        # (material_id, version_no) -> 該版本最新一條驗證
        self.verification_by_version = {}
        for verif in value["verifications"]:
            key = (verif["material_id"], verif["version_no"])
            old = self.verification_by_version.get(key)
            if old is None or _dt(verif["responded_at"]) > _dt(old["responded_at"]):
                self.verification_by_version[key] = verif
        # (stage, candidate) -> 證據事實 id 集合
        self.evidence = {
            (e["stage_id"], e["candidate_id"]): set(e["fact_ids"])
            for e in value["stage_evidence"]
        }

    def active_material_version(self, material_id: str):
        versions = sorted(self.materials[material_id]["versions"], key=lambda x: x["version_no"])
        return versions[-1] if versions and versions[-1]["status"] == "active" else None

    def candidate_for_sheet(self, sheet: dict) -> str:
        if sheet["stage_id"] == "S1":
            return self.packet_to_candidate[sheet["packet_code"]]
        return sheet["candidate_id"]


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _day(text: str) -> date:
    return date.fromisoformat(text)


def _d(number) -> Decimal:
    return Decimal(str(number))


# ---------------------------------------------------------------- 基本檢查

def _check_identifiers(ctx: _Index) -> None:
    def unique(rows, label):
        seen = set()
        for row in rows:
            if row["id"] in seen:
                raise ValueError(f"{label}標識重複：{row['id']}")
            seen.add(row["id"])

    unique(ctx.v["materials"], "材料")
    unique(ctx.v["verifications"], "驗證記錄")
    unique(ctx.v["confirmed_facts"], "確認事實")
    unique(ctx.v["judges"], "評委")
    unique(ctx.v["score_sheets"], "評分表")
    unique(ctx.v["stages"], "評審階段")
    if len({r["id"] for r in ctx.v["records"]}) != len(ctx.v["records"]):
        raise ValueError("領域記錄標識重複")
    if set(ctx.stages) != set(STAGE_IDS):
        raise ValueError("必須恰好包含匿名初審、面談、終審三個階段")


def _check_candidates(ctx: _Index) -> None:
    for cid, cand in ctx.candidates.items():
        if cand["institution_id"] not in ctx.institutions:
            raise ValueError(f"候選人 {cid} 的院校不存在")
        if cand["is_minor"] and "guardian_consent" not in cand:
            raise ValueError(f"未成年候選人 {cid} 缺少家長同意書")
        if not cand["is_minor"] and "guardian_consent" in cand:
            raise ValueError(f"成年候選人 {cid} 不應有家長同意書")
    codes = [e["packet_code"] for e in ctx.v["anonymous_map"]["entries"]]
    mapped = [e["candidate_id"] for e in ctx.v["anonymous_map"]["entries"]]
    if len(set(codes)) != len(codes) or set(mapped) != set(ctx.candidates):
        raise ValueError("匿名映射必須與全體候選人一一對應")


def _check_materials_and_verifications(ctx: _Index) -> None:
    deadline = _dt(ctx.settings["assignment_deadline"])
    seen_kinds = defaultdict(set)
    for mid, mat in ctx.materials.items():
        cid = mat["candidate_id"]
        if cid not in ctx.candidates:
            raise ValueError(f"材料 {mid} 所屬候選人不存在")
        if mat["kind"] not in VERIFIER_TRACK:
            raise ValueError(f"材料 {mid} 類型不在六類驗真範圍內")
        if mat["kind"] in seen_kinds[cid]:
            raise ValueError(f"候選人 {cid} 的 {mat['kind']} 材料重複收取")
        seen_kinds[cid].add(mat["kind"])

        versions = sorted(mat["versions"], key=lambda x: x["version_no"])
        nums = [v["version_no"] for v in versions]
        if nums != list(range(1, len(versions) + 1)):
            raise ValueError(f"材料 {mid} 版本號不連續")
        if versions[0]["action"] != "initial":
            raise ValueError(f"材料 {mid} 首版必須是 initial")
        if _dt(versions[0]["submitted_at"]) > deadline:
            raise ValueError(f"材料 {mid} 初版遲於收取截止時間")
        for prev, nxt in zip(versions, versions[1:]):
            if prev["status"] != "superseded":
                raise ValueError(f"材料 {mid} 被替換版本必須保留為 superseded，不得覆蓋")
            if nxt["action"] not in ("resubmit", "correction", "withdrawal"):
                raise ValueError(f"材料 {mid} 後續動作只能是補交、更正或撤回")
            if _dt(nxt["submitted_at"]) < _dt(prev["submitted_at"]):
                raise ValueError(f"材料 {mid} 版本時間倒流")
        if versions[-1]["action"] == "withdrawal" and versions[-1]["status"] != "withdrawn":
            raise ValueError(f"材料 {mid} 撤回版本狀態必須為 withdrawn")

        # 敏感材料只能在行政與初審範圍可見
        if mat["sensitive"] and not set(mat["access_scopes"]) <= {"admin", "screening"}:
            raise ValueError(f"敏感材料 {mid} 超出授權展示範圍")

        # 現行有效版本必須在其所屬驗真軌道上獲得確認
        active = ctx.active_material_version(mid)
        if active is not None:
            verif = ctx.verification_by_version.get((mid, active["version_no"]))
            if verif is None or verif["status"] != "confirmed":
                raise ValueError(f"材料 {mid} 現行版本缺少所屬軌道的確認")

    # 每位候選人六類材料齊全（分別收取、分別驗真）
    for cid in ctx.candidates:
        if seen_kinds[cid] != set(VERIFIER_TRACK):
            raise ValueError(f"候選人 {cid} 六類材料未收齊")

    for vid, verif in ctx.verifications.items():
        key = (verif["material_id"], verif["version_no"])
        if key not in ctx.version:
            raise ValueError(f"驗證記錄 {vid} 指向不存在的材料版本")
        mat = ctx.materials[verif["material_id"]]
        if verif["verifier_kind"] != VERIFIER_TRACK[mat["kind"]]:
            raise ValueError(f"驗證記錄 {vid} 走錯驗真軌道")
        if _dt(verif["responded_at"]) < _dt(verif["requested_at"]):
            raise ValueError(f"驗證記錄 {vid} 回覆早於發出")


def _check_facts(ctx: _Index) -> None:
    for fid, fact in ctx.facts.items():
        cid = fact["candidate_id"]
        mid = fact["material_id"]
        if cid not in ctx.candidates:
            raise ValueError(f"事實 {fid} 候選人不存在")
        if mid not in ctx.materials or ctx.materials[mid]["candidate_id"] != cid:
            raise ValueError(f"事實 {fid} 材料歸屬不一致")
        version = ctx.version.get((mid, fact["version_no"]))
        if version is None:
            raise ValueError(f"事實 {fid} 指向不存在的材料版本")
        if version["status"] != "active":
            raise ValueError(f"事實 {fid} 只能引用現行有效版本（撤回或作廢版本不得成事實）")
        verif = ctx.verification_by_version.get((mid, fact["version_no"]))
        if verif is None or verif["status"] != "confirmed":
            raise ValueError(f"事實 {fid} 未經確認，不得採信")
        if _dt(fact["confirmed_at"]) < _dt(verif["responded_at"]):
            raise ValueError(f"事實 {fid} 確認時間早於外部驗真回覆")
        stages = set(fact["visible_in_stages"])
        if not stages <= set(STAGE_IDS):
            raise ValueError(f"事實 {fid} 可見階段非法")

        if fact["sensitive"]:
            if fact["publication_mode"] != "none" or fact["redacted"]:
                raise ValueError(f"敏感事實 {fid} 不得公開且必須保持未遮蔽原文用於初審")
            if stages - {"S1"}:
                raise ValueError(f"敏感事實 {fid} 完整內容僅初審可見")
            consent = ctx.candidates[cid].get("guardian_consent")
            if consent is None or not any(s.startswith("screening") for s in consent["scopes"]):
                raise ValueError(f"敏感事實 {fid} 缺少家長初審授權")
        if fact["redacted"]:
            parent = fact.get("derived_from_fact")
            if parent not in ctx.facts:
                raise ValueError(f"遮蔽事實 {fid} 必須標明來源事實")
            source = ctx.facts[parent]
            if not source["sensitive"] or (source["material_id"], source["version_no"]) != (mid, fact["version_no"]):
                raise ValueError(f"遮蔽事實 {fid} 必須派生自同版本敏感事實")
            if fact["publication_mode"] != "redacted":
                raise ValueError(f"遮蔽事實 {fid} 發布方式必須為 redacted")


# ---------------------------------------------------------------- 評委與回避

def _check_judges_and_coi(ctx: _Index) -> None:
    for j in ctx.v["judges"]:
        if j["id"] not in ctx.judges:
            raise ValueError("評委索引異常")
    first_window = min(_dt(s["window_starts_at"]) for s in ctx.v["stages"])
    conflicts = set()
    for c in ctx.v["coi_declarations"]:
        if c["judge_id"] not in ctx.judges or c["candidate_id"] not in ctx.candidates:
            raise ValueError(f"利益申報 {c['id']} 引用不存在的評委或候選人")
        if _dt(c["declared_at"]) > first_window:
            raise ValueError(f"利益申報 {c['id']} 遲於首階段開始，無法據此分配")
        conflicts.add((c["judge_id"], c["candidate_id"]))
    ctx.conflicts = conflicts


def _check_stages_and_rubrics(ctx: _Index) -> None:
    ordered = [ctx.stages[sid] for sid in STAGE_IDS]
    starts = [_dt(s["window_starts_at"]) for s in ordered]
    if starts != sorted(starts):
        raise ValueError("三個評審階段時間順序錯亂")
    for s in ordered:
        weights = s["rubric"]["weights"]
        if sum(_d(w) for w in weights.values()) != Decimal(1):
            raise ValueError(f"階段 {s['id']} 評分權重必須合計為 1")
        if _dt(s["rubric_frozen_at"]) > _dt(s["window_starts_at"]):
            raise ValueError(f"階段 {s['id']} 標準必須在階段開始前凍結")
        if _dt(s["locked_at"]) <= _dt(s["window_starts_at"]):
            raise ValueError(f"階段 {s['id']} 凍結時間必須晚於開始時間")
    comp = ctx.settings["stage_composition"]
    if sum(_d(comp[sid]) for sid in STAGE_IDS) != Decimal(1):
        raise ValueError("三階段合成權重必須合計為 1")

    anon = ctx.v["anonymous_map"]
    s1 = ctx.stages["S1"]
    if _dt(anon["sealed_at"]) > _dt(s1["window_starts_at"]):
        raise ValueError("匿名映射必須在初審開始前密封")
    if _dt(anon["opened_at"]) < _dt(s1["locked_at"]):
        raise ValueError("匿名映射只能在初審凍結後啟封")


def _check_score_sheets(ctx: _Index) -> None:
    lo = _d(ctx.settings["score_scale"]["min"])
    hi = _d(ctx.settings["score_scale"]["max"])
    min_panel = ctx.settings["min_panel_size"]
    groups = defaultdict(list)
    for sh in ctx.sheets:
        sid = sh["stage_id"]
        stage = ctx.stages.get(sid)
        if stage is None or sh["judge_id"] not in ctx.judges:
            raise ValueError(f"評分表 {sh['id']} 階段或評委不存在")
        if sid == "S1":
            if sh.get("candidate_id") is not None or sh.get("packet_code") not in ctx.packet_to_candidate:
                raise ValueError(f"初審評分表 {sh['id']} 必須匿名，僅使用卷宗編號")
        else:
            if sh.get("packet_code") is not None or sh.get("candidate_id") not in ctx.candidates:
                raise ValueError(f"階段 {sid} 評分表 {sh['id']} 必須使用候選人編號")
        cid = ctx.candidate_for_sheet(sh)
        if cid not in stage["candidate_ids"]:
            raise ValueError(f"評分表 {sh['id']} 候選人不在該階段名單內")
        recorded = _dt(sh["recorded_at"])
        if not (_dt(stage["window_starts_at"]) <= recorded <= _dt(stage["locked_at"])):
            raise ValueError(f"評分表 {sh['id']} 錄入時間超出階段窗口")
        if (sh["judge_id"], cid) in ctx.conflicts:
            raise ValueError(f"評分表 {sh['id']} 違反利益回避：評委已申報與該候選人的關係")
        judges = groups[(sid, cid)]
        if sh["judge_id"] in judges:
            raise ValueError(f"階段 {sid} 候選人 {cid} 收到同一評委重複評分")
        judges.append(sh["judge_id"])
        for dim in DIMENSIONS:
            val = _d(sh["scores"][dim])
            if not (lo <= val <= hi):
                raise ValueError(f"評分表 {sh['id']} 的 {dim} 超出量尺")
    for (sid, cid), panel in groups.items():
        if len(panel) < min_panel:
            raise ValueError(f"階段 {sid} 候選人 {cid} 有效評委不足 {min_panel} 人")
    ctx.panel_groups = groups


def _check_evidence_visibility(ctx: _Index) -> None:
    """每個階段看到的事實 = 對該階段可見、且在標準凍結前確認的事實。"""
    # 構造 (stage, candidate) -> 依可見性與凍結時間應當可用的事實
    expected = defaultdict(set)
    for fid, fact in ctx.facts.items():
        for sid in fact["visible_in_stages"]:
            if _dt(fact["confirmed_at"]) <= _dt(ctx.stages[sid]["rubric_frozen_at"]):
                expected[(sid, fact["candidate_id"])].add(fid)
    for key, fids in expected.items():
        if ctx.evidence.get(key, set()) != fids:
            raise ValueError(f"階段 {key[0]} 候選人 {key[1]} 證據包與凍結時可見事實不一致")
    # 反過來：證據包裡不得出現不可見或未凍結事實
    for (sid, cid), fids in ctx.evidence.items():
        for fid in fids:
            fact = ctx.facts.get(fid)
            if fact is None or fact["candidate_id"] != cid or sid not in fact["visible_in_stages"]:
                raise ValueError(f"階段 {sid} 使用了不可見事實 {fid}")
            if _dt(fact["confirmed_at"]) > _dt(ctx.stages[sid]["rubric_frozen_at"]):
                raise ValueError(f"階段 {sid} 引用了標準凍結後才確認的事實 {fid}")


def _sheet_weighted_total(sheet: dict, weights: dict) -> Decimal:
    return sum(_d(sheet["scores"][dim]) * _d(weights[dim]) for dim in DIMENSIONS)


def compute_stage_totals(value: dict, rounding: int | None = 4) -> dict:
    """從評分表與各階段凍結權重重算每個候選人的階段總分。"""
    stages = {s["id"]: s for s in value["stages"]}
    packet = {e["packet_code"]: e["candidate_id"] for e in value["anonymous_map"]["entries"]}
    grouped = defaultdict(list)
    for sh in value["score_sheets"]:
        cid = sh["candidate_id"] if sh["stage_id"] != "S1" else packet[sh["packet_code"]]
        grouped[(sh["stage_id"], cid)].append(sh)
    totals = {}
    for (sid, cid), sheets in grouped.items():
        weights = stages[sid]["rubric"]["weights"]
        avg = sum(_sheet_weighted_total(sh, weights) for sh in sheets) / Decimal(len(sheets))
        if rounding is not None:
            avg = avg.quantize(Decimal(10) ** -rounding, rounding=ROUND_HALF_UP)
        totals[(sid, cid)] = avg
    return totals


def _check_extreme_gaps(ctx: _Index) -> None:
    threshold = _d(ctx.settings["extreme_gap_threshold"])
    grouped = defaultdict(list)
    for sh in ctx.sheets:
        grouped[(sh["stage_id"], ctx.candidate_for_sheet(sh))].append(sh)

    actual = {}
    for (sid, cid), sheets in grouped.items():
        for dim in DIMENSIONS:
            vals = [_d(sh["scores"][dim]) for sh in sheets]
            gap = max(vals) - min(vals)
            if gap >= threshold:
                actual[(sid, cid, dim)] = gap
    recorded = {(r["stage_id"], r["candidate_id"], r["dimension"]): r for r in ctx.reviews}

    if set(actual) != set(recorded):
        raise ValueError("極端分差與覆核記錄沒有一一對應（漏觸發或多觸發）")
    for key, gap in actual.items():
        review = recorded[key]
        if _d(review["gap"]) != gap:
            raise ValueError(f"覆核記錄 {review['id']} 的分差與評分表不符")
        if review["elimination_effect"] != "none":
            raise ValueError(f"覆核記錄 {review['id']} 不得產生淘汰效果")
        if review.get("resolved_at") and _dt(review["resolved_at"]) < _dt(review["triggered_at"]):
            raise ValueError(f"覆核記錄 {review['id']} 解決時間早於觸發時間")
        # 被覆核候選人仍須在後續階段名單與最終名次中（極端分差不淘汰）
        sid, cid, _ = key
        later = STAGE_IDS[STAGE_IDS.index(sid) + 1:]
        for later_id in later:
            if cid not in ctx.stages[later_id]["candidate_ids"]:
                raise ValueError(f"候選人 {cid} 因極端分差被移出階段 {later_id}，違反覆核政策")
        if all(e["candidate_id"] != cid for e in ctx.v["results"]["entries"]):
            raise ValueError(f"候選人 {cid} 因極端分差缺失最終名次")


# ---------------------------------------------------------------- 申訴、結果、保留

def _check_appeals_and_publication(ctx: _Index) -> None:
    win = ctx.settings["appeal_freeze_window"]
    start, end = _dt(win["starts_at"]), _dt(win["ends_at"])
    s3_locked = _dt(ctx.stages["S3"]["locked_at"])
    if not (s3_locked <= start):
        raise ValueError("申訴凍結窗必須在終審凍結後開始")
    for a in ctx.appeals:
        if a["candidate_id"] not in ctx.candidates:
            raise ValueError(f"申訴 {a['id']} 候選人不存在")
        filed, resolved = _dt(a["filed_at"]), _dt(a["resolved_at"])
        if not (start <= filed <= end) or not (start <= resolved <= end):
            raise ValueError(f"申訴 {a['id']} 必須在凍結窗內提出並了結")
        if a["ranking_changed"]:
            raise ValueError(f"申訴 {a['id']} 凍結窗內不得變更名次")
    published = _dt(ctx.v["results"]["published_at"])
    if published <= end:
        raise ValueError("最終結果必須在申訴凍結窗結束後發布")
    for a in ctx.appeals:
        if _dt(a["resolved_at"]) > published:
            raise ValueError(f"申訴 {a['id']} 了結時間不得晚於發布")


def _check_results(ctx: _Index) -> None:
    totals = compute_stage_totals(ctx.v)
    # 申報的階段總分必須可由評分表重建
    for row in ctx.v["stage_results"]:
        key = (row["stage_id"], row["candidate_id"])
        if key not in totals:
            raise ValueError(f"階段結果缺少評分表：{key}")
        if abs(totals[key] - _d(row["total"])) > Decimal("0.0001"):
            raise ValueError(f"階段結果 {key} 無法由評分表重建：{row['total']} != {totals[key]}")

    # 初審門檻
    threshold = _d(ctx.settings["screening_pass"]["min_total"])
    for cid in ctx.candidates:
        advanced = cid in ctx.stages["S1"]["advanced_candidate_ids"]
        if advanced and totals[("S1", cid)] < threshold:
            raise ValueError(f"候選人 {cid} 初審未達門檻卻進入下一階段")
        if cid in ctx.stages["S2"]["candidate_ids"] and not advanced:
            raise ValueError(f"候選人 {cid} 未通過初審卻出現在面談名單")

    comp = ctx.settings["stage_composition"]
    weights = {sid: _d(comp[sid]) for sid in STAGE_IDS}
    rounding = ctx.v["reconstruction"]["rounding_decimals"]
    quant = Decimal(10) ** -rounding
    expected = []
    for cid in ctx.candidates:
        score = sum(weights[sid] * totals[(sid, cid)] for sid in STAGE_IDS)
        expected.append((cid, score.quantize(quant, rounding=ROUND_HALF_UP)))
    expected.sort(key=lambda x: x[1], reverse=True)

    entries = ctx.v["results"]["entries"]
    if len(entries) != len(expected):
        raise ValueError("最終名次條目數與候選人不符")
    award_by_rank = {1: "champion", 2: "first_runner_up", 3: "second_runner_up"}
    facts_used = set()
    for i, (entry, (cid, score)) in enumerate(zip(entries, expected), start=1):
        if entry["rank"] != i or entry["candidate_id"] != cid:
            raise ValueError(f"第 {i} 名無法由凍結規則與評分重建")
        if abs(_d(entry["composite"]) - score) > Decimal("0.0001"):
            raise ValueError(f"第 {i} 名綜合分無法重建：{entry['composite']} != {score}")
        expected_award = award_by_rank.get(i, "merit" if i < len(expected) else "none")
        if entry["award"] != expected_award:
            raise ValueError(f"第 {i} 名獎項與名次不匹配")
        _check_reason_facts(ctx, entry)
        facts_used.update(entry["reason_fact_ids"])
    ctx.reconstructed = [
        {"rank": i, "candidate_id": cid, "composite": score}
        for i, (cid, score) in enumerate(expected, start=1)
    ]


def _check_reason_facts(ctx: _Index, entry: dict) -> None:
    cid = entry["candidate_id"]
    fact_ids = entry["reason_fact_ids"]
    if entry["award"] == "none":
        if fact_ids or entry["publication_name_allowed"]:
            raise ValueError(f"未入選者 {cid} 不應發布獲獎理由或姓名")
        return
    if not fact_ids:
        raise ValueError(f"獲獎者 {cid} 的獲獎理由必須有事實支撐")
    for fid in fact_ids:
        fact = ctx.facts.get(fid)
        if fact is None or fact["candidate_id"] != cid:
            raise ValueError(f"獲獎理由引用了不屬於 {cid} 的事實 {fid}")
        if fact["sensitive"] or fact["publication_mode"] == "none":
            raise ValueError(f"獲獎理由引用了敏感或禁止公開事實 {fid}")
        verif = ctx.verification_by_version[(fact["material_id"], fact["version_no"])]
        if verif["status"] != "confirmed":
            raise ValueError(f"獲獎理由引用了未經確認事實 {fid}")
    cand = ctx.candidates[cid]
    if entry["publication_name_allowed"]:
        if cand["is_minor"]:
            scopes = cand["guardian_consent"]["scopes"]
            if not any(s.startswith("publication") for s in scopes):
                raise ValueError(f"未成年獲獎者 {cid} 公開姓名缺少家長授權")


def _check_retention(ctx: _Index) -> None:
    published = _day(ctx.v["results"]["published_at"][:10])
    ret = ctx.settings["retention"]
    schedule = {(r["candidate_id"], r["class"]): r for r in ctx.v["retention_schedule"]}

    for entry in ctx.v["results"]["entries"]:
        cid = entry["candidate_id"]
        if entry["award"] == "none":
            key = (cid, "unsuccessful_disposal")
            row = schedule.get(key)
            if row is None:
                raise ValueError(f"未入選者 {cid} 缺少清理安排")
            expect = _add_months(published, ret["unsuccessful_months"]).isoformat()
            if row["retain_until"] != expect:
                raise ValueError(f"未入選者 {cid} 清理期限應為 {expect}")
        else:
            row = schedule.get((cid, "awardee_archive"))
            if row is None:
                raise ValueError(f"獲獎者 {cid} 缺少封存安排")
            expect = published.replace(year=published.year + ret["awardee_archive_years"]).isoformat()
            if row["retain_until"] != expect:
                raise ValueError(f"獲獎者 {cid} 封存期限應為 {expect}")

    # 撤回版本保留一年審計軌跡
    for mid, mat in ctx.materials.items():
        last = sorted(mat["versions"], key=lambda x: x["version_no"])[-1]
        if last["status"] == "withdrawn":
            rows = [r for r in ctx.v["retention_schedule"]
                    if r["candidate_id"] == mat["candidate_id"] and r["class"] == "withdrawn_audit"
                    and r.get("material_id") == mid]
            if not rows:
                raise ValueError(f"撤回材料 {mid} 缺少審計保留安排")
            expect = published.replace(year=published.year + ret["withdrawn_audit_years"]).isoformat()
            if rows[0]["retain_until"] != expect:
                raise ValueError(f"撤回材料 {mid} 審計保留期限應為 {expect}")


def _add_months(day_value: date, months: int) -> date:
    month_index = (day_value.month - 1) + months
    year = day_value.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, day_value.day)


# ---------------------------------------------------------------- 重建快照

def reconstruct_ranking(value: dict) -> dict:
    """從原始評分表、凍結 rubric 與有效評委重建最終名次。

    返回排名、綜合分、各階段總分、各階段有效評委與所用證據，
    供「任何最終名次都可以重建當時的證據、評分規則與有效評委」核查。
    """
    validate_domain(value)
    ctx = _Index(value)
    totals = compute_stage_totals(value)
    comp = value["settings"]["stage_composition"]
    rounding = value["reconstruction"]["rounding_decimals"]
    quant = Decimal(10) ** -rounding

    panel = defaultdict(dict)
    for sh in value["score_sheets"]:
        cid = ctx.candidate_for_sheet(sh)
        panel[(sh["stage_id"], cid)].setdefault("judges", set()).add(sh["judge_id"])
        panel[(sh["stage_id"], cid)].setdefault("sheets", []).append(sh["id"])

    ranked = []
    for cid in ctx.candidates:
        composite = sum(_d(comp[sid]) * totals[(sid, cid)] for sid in STAGE_IDS)
        stages_view = {}
        for sid in STAGE_IDS:
            info = panel[(sid, cid)]
            stages_view[sid] = {
                "total": totals[(sid, cid)],
                "rubric": ctx.stages[sid]["rubric"],
                "locked_at": ctx.stages[sid]["locked_at"],
                "judges": sorted(info["judges"]),
                "score_sheet_ids": sorted(info["sheets"]),
                "fact_ids": sorted(ctx.evidence[(sid, cid)]),
            }
        ranked.append({
            "candidate_id": cid,
            "composite": composite.quantize(quant, rounding=ROUND_HALF_UP),
            "stages": stages_view,
        })
    ranked.sort(key=lambda x: x["composite"], reverse=True)
    for i, row in enumerate(ranked, start=1):
        row["rank"] = i
    return {
        "snapshot_id": value["results"]["snapshot_id"],
        "formula": {sid: _d(comp[sid]) for sid in STAGE_IDS},
        "entries": ranked,
    }


def _check_reconstruction(ctx: _Index) -> None:
    rec = ctx.v["reconstruction"]
    results = ctx.v["results"]
    if rec["id"] != results["snapshot_id"]:
        raise ValueError("重建快照標識必須與結果快照一致")
    if {sid: _d(x) for sid, x in rec["formula"].items()} != {
        sid: _d(ctx.settings["stage_composition"][sid]) for sid in STAGE_IDS
    }:
        raise ValueError("重建快照中的合成公式與凍結設定不一致")

    sheets_by_stage = defaultdict(set)
    judges_by_stage = defaultdict(set)
    for sh in ctx.sheets:
        sheets_by_stage[sh["stage_id"]].add(sh["id"])
        judges_by_stage[sh["stage_id"]].add(sh["judge_id"])
    for snap in rec["stage_snapshots"]:
        sid = snap["stage_id"]
        stage = ctx.stages[sid]
        if snap["locked_at"] != stage["locked_at"]:
            raise ValueError(f"快照 {sid} 凍結時間與階段記錄不一致")
        if snap["rubric"] != stage["rubric"]:
            raise ValueError(f"快照 {sid} rubric 與凍結標準不一致")
        if set(snap["score_sheet_ids"]) != sheets_by_stage[sid]:
            raise ValueError(f"快照 {sid} 評分表清單與實際評分表不一致")
        if set(snap["panel_judges"]) != judges_by_stage[sid]:
            raise ValueError(f"快照 {sid} 有效評委清單與實際評分評委不一致")
    if set(rec["appeal_ids_resolved"]) != {a["id"] for a in ctx.appeals}:
        raise ValueError("重建快照必須列出申訴窗內全部了結申訴")
    if rec["anonymous_map_opened_at"] != ctx.v["anonymous_map"]["opened_at"]:
        raise ValueError("重建快照的匿名映射啟封時間不一致")
