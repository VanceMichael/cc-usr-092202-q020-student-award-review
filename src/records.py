"""读取并检查杰出学生评审证据室的共享领域资料。

校验覆盖主办方的核心承诺：
- 六类材料分渠道验真，补交/更正/撤回保留旧版本且版本链双向一致；
- 利益冲突申报、回避名单、阶段面板与评分记录彼此一致；
- 评分只能在阶段标准冻结后由该候选人该阶段的有效评委提交；
- 极端分差必须有复核记录，复核结论不得是淘汰；
- 申诉冻结快照与当时的证据版本、标准、评委、分数一致；
- 获奖理由只引用已确认且授权公开的材料；
- 每个最终名次都能重建证据（含旧版本）、规则与评委；
- 未入选者资料有到期清理安排；未成年人有监护人联签，敏感材料不公开。
"""

import json
from datetime import date, timedelta
from pathlib import Path
from statistics import median

REQUIRED_TOP_LEVEL = {
    "domain", "version", "sample_id", "actors", "facts", "constraints",
    "candidates", "materials", "judges", "stages", "scores",
    "score_reviews", "appeals", "results", "reconstructions", "retention",
}

MATERIAL_TYPES = {
    "nomination", "transcript", "project_role", "service_hours",
    "recommendation", "public_work",
}
STAGE_ORDER = ["screen", "interview", "final"]
PUBLISHED_SCOPE = "published"


def load_records(path: Path) -> dict:
    """返回结构完整的领域资料。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    validate_domain(value)
    return value


def validate_domain(value: dict) -> None:
    """对领域资料执行全部不变量校验，失败时抛出 ValueError。"""
    errors: list[str] = []

    if not REQUIRED_TOP_LEVEL.issubset(value):
        missing = sorted(REQUIRED_TOP_LEVEL - value.keys())
        raise ValueError(f"领域资料缺少必要字段: {', '.join(missing)}")
    if value["domain"] != "student-award-review":
        errors.append("domain 标识必须为 student-award-review")
    if value["version"] < 2:
        errors.append("version 必须为 2 或以上")

    candidates = {c["id"]: c for c in value["candidates"]}
    materials = {m["id"]: m for m in value["materials"]}
    judges = {j["id"]: j for j in value["judges"]}
    stages = {s["id"]: s for s in value["stages"]}
    scores = {s["id"]: s for s in value["scores"]}
    appeals = {a["id"]: a for a in value["appeals"]}

    _check_unique_ids(value, errors)
    _check_materials(candidates, materials, errors)
    _check_judges_and_panels(candidates, judges, stages, errors)
    _check_scores(candidates, materials, judges, stages, scores,
                  value.get("constants", {}), value["score_reviews"], errors)
    _check_appeals(candidates, materials, stages, scores, appeals, errors)
    _check_results(candidates, materials, value["results"], errors)
    _check_reconstructions(candidates, materials, stages, scores, appeals,
                           value["results"], value["reconstructions"], errors)
    _check_aggregations(stages, scores, candidates,
                        value.get("aggregated_results", []), errors)
    _check_retention(candidates, materials, scores, value["results"],
                     value.get("constants", {}), value["retention"], errors)
    _check_consent_and_sensitivity(candidates, materials, value["results"], errors)

    if errors:
        raise ValueError("领域资料不变量校验失败:\n- " + "\n- ".join(errors))


# --------------------------------------------------------------------------
# 基础结构
# --------------------------------------------------------------------------

def _check_unique_ids(value: dict, errors: list[str]) -> None:
    id_groups = {
        "候选人": [c["id"] for c in value["candidates"]],
        "材料": [m["id"] for m in value["materials"]],
        "评委": [j["id"] for j in value["judges"]],
        "阶段": [s["id"] for s in value["stages"]],
        "评分": [s["id"] for s in value["scores"]],
        "复核": [r["id"] for r in value["score_reviews"]],
        "申诉": [a["id"] for a in value["appeals"]],
        "重建包": [r["bundle_id"] for r in value["reconstructions"]],
    }
    for label, ids in id_groups.items():
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            errors.append(f"{label}标识重复: {', '.join(dupes)}")


# --------------------------------------------------------------------------
# 材料与版本链
# --------------------------------------------------------------------------

def _check_materials(candidates: dict, materials: dict, errors: list[str]) -> None:
    for cid, candidate in candidates.items():
        seen_types = set()
        for m in value_materials_of(materials, cid):
            seen_types.add(m["type"])
        missing_types = MATERIAL_TYPES - seen_types
        if missing_types:
            errors.append(
                f"候选人 {cid} 缺少材料类别: {', '.join(sorted(missing_types))}"
            )

    for mid, m in materials.items():
        if m["candidate_id"] not in candidates:
            errors.append(f"材料 {mid} 引用了不存在的候选人 {m['candidate_id']}")
        if m["type"] not in MATERIAL_TYPES:
            errors.append(f"材料 {mid} 的类别 {m['type']} 不合法")

        old_id, new_id = m.get("supersedes"), m.get("successor")
        if old_id is not None:
            old = materials.get(old_id)
            if old is None:
                errors.append(f"材料 {mid} 的 supersedes 指向不存在的 {old_id}")
            elif old.get("successor") != mid:
                errors.append(f"材料 {mid} 与 {old_id} 的版本链不一致（successor 未指回）")
            elif old["candidate_id"] != m["candidate_id"] or old["type"] != m["type"]:
                errors.append(f"材料 {mid} 跨候选人或跨类别取代 {old_id}")
        if new_id is not None:
            new = materials.get(new_id)
            if new is None:
                errors.append(f"材料 {mid} 的 successor 指向不存在的 {new_id}")
            elif new.get("supersedes") != mid:
                errors.append(f"材料 {mid} 与 {new_id} 的版本链不一致（supersedes 未指回）")

        ver_status = m["status"]
        v_status = m["verification"]["status"]
        if ver_status == "confirmed" and v_status != "confirmed":
            errors.append(f"材料 {mid} 标记为 confirmed 但外部验真状态为 {v_status}")
        if ver_status in {"superseded", "withdrawn", "disputed", "unavailable"}:
            if "screen" in m["visibility"] or "published" in m["visibility"]:
                errors.append(f"材料 {mid} 状态为 {ver_status}，不得向评委或公众展示")

    # 同一候选人同一类别的版本号不得重复
    seen_versions: dict[tuple[str, str], list[int]] = {}
    for m in materials.values():
        key = (m["candidate_id"], m["type"])
        seen_versions.setdefault(key, []).append(m["version"])
    for key, versions in seen_versions.items():
        if len(versions) != len(set(versions)):
            errors.append(f"候选人 {key[0]} 的 {key[1]} 材料版本号重复")


def value_materials_of(materials: dict, candidate_id: str) -> list[dict]:
    return [m for m in materials.values() if m["candidate_id"] == candidate_id]


# --------------------------------------------------------------------------
# 回避与面板
# --------------------------------------------------------------------------

def _check_judges_and_panels(candidates: dict, judges: dict, stages: dict,
                             errors: list[str]) -> None:
    for jid, judge in judges.items():
        conflict_set = {c["candidate_id"] for c in judge["conflicts"]}
        unknown = conflict_set - set(candidates)
        if unknown:
            errors.append(f"评委 {jid} 申报了不存在的候选人冲突: {', '.join(sorted(unknown))}")
        if conflict_set != set(judge["recused"]):
            errors.append(
                f"评委 {jid} 的利益冲突申报 {sorted(conflict_set)} 与回避名单 "
                f"{sorted(judge['recused'])} 不一致"
            )

    stage_ids = [sid for sid in STAGE_ORDER if sid in stages]
    if stage_ids != STAGE_ORDER:
        errors.append("阶段必须依次包含 screen、interview、final")
    frozen_versions: dict[str, str] = {}
    for sid in stage_ids:
        stage = stages[sid]
        frozen_versions[sid] = stage["criteria"]["version"]
        panels = {p["candidate_id"]: p["judge_ids"] for p in stage["panels"]}
        if set(panels) != set(candidates):
            errors.append(f"阶段 {sid} 的面板未覆盖全部候选人")
        for cid, panel in panels.items():
            for jid in panel:
                if jid not in judges:
                    errors.append(f"阶段 {sid} 候选人 {cid} 的面板含不存在的评委 {jid}")
                elif cid in judges[jid]["recused"]:
                    errors.append(f"阶段 {sid} 候选人 {cid} 的面板包含应回避评委 {jid}")
    if len(set(frozen_versions.values())) != len(frozen_versions):
        errors.append("三个阶段必须使用各自冻结的评分标准版本")
    if stages.get("screen") and not stages["screen"]["anonymized"]:
        errors.append("匿名初审必须开启匿名")


# --------------------------------------------------------------------------
# 评分与极端分差复核
# --------------------------------------------------------------------------

def _check_scores(candidates: dict, materials: dict, judges: dict, stages: dict,
                  scores: dict, constants: dict, reviews: list[dict],
                  errors: list[str]) -> None:
    panel_lookup = {
        (sid, p["candidate_id"]): set(p["judge_ids"])
        for sid, stage in stages.items()
        for p in stage["panels"]
    }
    frozen_at = {sid: stage["criteria"]["frozen_at"] for sid, stage in stages.items()}
    criteria_version = {sid: stage["criteria"]["version"] for sid, stage in stages.items()}
    scale = {sid: stage["criteria"]["scale"] for sid, stage in stages.items()}

    for scid, sc in scores.items():
        if sc["candidate_id"] not in candidates:
            errors.append(f"评分 {scid} 引用不存在的候选人 {sc['candidate_id']}")
        if sc["judge_id"] not in judges:
            errors.append(f"评分 {scid} 引用不存在的评委 {sc['judge_id']}")
        if sc["stage_id"] not in stages:
            errors.append(f"评分 {scid} 引用不存在的阶段 {sc['stage_id']}")
            continue
        sid, cid, jid = sc["stage_id"], sc["candidate_id"], sc["judge_id"]
        panel = panel_lookup.get((sid, cid))
        if panel is not None and jid not in panel:
            errors.append(f"评分 {scid}：评委 {jid} 不在 {sid} 阶段候选人 {cid} 的有效面板内")
        if sc["criteria_version"] != criteria_version[sid]:
            errors.append(f"评分 {scid} 使用的标准版本不是 {sid} 阶段冻结版本")
        if sc["scored_at"] < frozen_at[sid]:
            errors.append(f"评分 {scid} 提交于 {sid} 阶段标准冻结之前")
        low, high = scale[sid]
        if not low <= sc["score"] <= high:
            errors.append(f"评分 {scid} 超出 {sid} 阶段量尺 {scale[sid]}")

    # 极端分差：超过阈值必须有复核，且复核不能导致淘汰
    threshold = constants.get("extreme_score_gap_threshold", 20)
    grouped: dict[tuple[str, str], list[dict]] = {}
    for sc in scores.values():
        grouped.setdefault((sc["stage_id"], sc["candidate_id"]), []).append(sc)
    reviewed_score_ids = {r["score_id"] for r in reviews}
    for (sid, cid), group in grouped.items():
        values = [sc["score"] for sc in group]
        med = median(values)
        for sc in group:
            gap = abs(sc["score"] - med)
            if gap > threshold and sc["id"] not in reviewed_score_ids:
                errors.append(
                    f"{sid} 阶段候选人 {cid} 的评分 {sc['id']} 与中位数差距 {gap:g} "
                    f"超过阈值 {threshold}，缺少极端分差复核"
                )

    for r in reviews:
        sc = scores.get(r["score_id"])
        if sc is None:
            errors.append(f"复核 {r['id']} 引用不存在的评分 {r['score_id']}")
            continue
        if sc["stage_id"] != r["stage_id"] or sc["candidate_id"] != r["candidate_id"]:
            errors.append(f"复核 {r['id']} 与所复核评分的阶段或候选人不一致")
        if r["elimination"]:
            errors.append(f"复核 {r['id']} 不得作出淘汰结论")
        if r.get("threshold", threshold) > r.get("gap_to_median", 0):
            errors.append(f"复核 {r['id']} 记录的分差未超过其阈值")
        if r["trigger"] != "extreme_gap":
            errors.append(f"复核 {r['id']} 的触发类型不合法")


# --------------------------------------------------------------------------
# 申诉冻结
# --------------------------------------------------------------------------

def _check_appeals(candidates: dict, materials: dict, stages: dict, scores: dict,
                   appeals: dict, errors: list[str]) -> None:
    for aid, appeal in appeals.items():
        cid, sid = appeal["candidate_id"], appeal["stage_id"]
        if cid not in candidates:
            errors.append(f"申诉 {aid} 引用不存在的候选人 {cid}")
        if sid not in stages:
            errors.append(f"申诉 {aid} 引用不存在的阶段 {sid}")
            continue
        freeze = appeal["freeze"]
        stage = stages[sid]
        if freeze["frozen_at"] != appeal["filed_at"]:
            errors.append(f"申诉 {aid} 的冻结时刻必须是受理时刻")
        if freeze["criteria_version"] != stage["criteria"]["version"]:
            errors.append(f"申诉 {aid} 冻结的标准版本与 {sid} 阶段冻结版本不一致")

        panel = {p["candidate_id"]: set(p["judge_ids"])
                 for p in stage["panels"]}.get(cid, set())
        if set(freeze["judge_ids"]) != panel:
            errors.append(f"申诉 {aid} 冻结的评委名单与当时面板不一致")

        stage_scores = {s["id"] for s in scores.values()
                        if s["stage_id"] == sid and s["candidate_id"] == cid}
        if set(freeze["score_ids"]) != stage_scores:
            errors.append(f"申诉 {aid} 冻结的评分集合与当时评分记录不一致")

        candidate_materials = {m["id"] for m in materials.values()
                               if m["candidate_id"] == cid}
        frozen_materials = set(freeze["material_versions"])
        if not frozen_materials <= candidate_materials:
            errors.append(f"申诉 {aid} 冻结了不属于候选人 {cid} 的材料版本")
        # 受理时该候选人已提交的全部版本（含旧版）都应入快照
        submitted_by_freeze = {
            mid for mid, m in materials.items()
            if m["candidate_id"] == cid and m["submitted_at"] <= freeze["frozen_at"]
        }
        if submitted_by_freeze != frozen_materials:
            errors.append(
                f"申诉 {aid} 的证据快照与受理时已提交版本不一致："
                f"缺少 {sorted(submitted_by_freeze - frozen_materials)}，"
                f"多出 {sorted(frozen_materials - submitted_by_freeze)}"
            )


# --------------------------------------------------------------------------
# 结果发布
# --------------------------------------------------------------------------

def _check_results(candidates: dict, materials: dict, results: list[dict],
                   errors: list[str]) -> None:
    ranks = [r["rank"] for r in results]
    if ranks != sorted(ranks) or len(ranks) != len(set(ranks)):
        errors.append("最终名次必须唯一且按顺序排列")
    for r in results:
        cid = r["candidate_id"]
        if cid not in candidates:
            errors.append(f"名次 {r.get('rank')} 引用不存在的候选人 {cid}")
            continue
        rationale = r["rationale"]
        internal, published = set(rationale["internal_refs"]), set(rationale["published_refs"])
        if not published <= internal:
            errors.append(f"名次 {r['rank']} 的公开理由引用了内部理由之外的材料")
        for label, refs in (("内部", internal), ("公开", published)):
            for mid in refs:
                m = materials.get(mid)
                if m is None:
                    errors.append(f"名次 {r['rank']} 的{label}理由引用不存在的材料 {mid}")
                    continue
                if m["candidate_id"] != cid:
                    errors.append(f"名次 {r['rank']} 的{label}理由引用了其他候选人的材料 {mid}")
                if m["status"] != "confirmed" or m["verification"]["status"] != "confirmed":
                    errors.append(f"名次 {r['rank']} 的{label}理由引用了未确认事实 {mid}")
        for mid in published:
            m = materials.get(mid)
            if m is not None and PUBLISHED_SCOPE not in m["visibility"]:
                errors.append(f"名次 {r['rank']} 公开引用了未授权发布的材料 {mid}")
        detail = r.get("public_detail_level", "none")
        if detail == "award_only" and published:
            errors.append(f"名次 {r['rank']} 授权仅发布奖项，却公开了具体材料")
        candidate = candidates[cid]
        release = candidate["consent"]["public_release"]
        if detail != release:
            errors.append(
                f"名次 {r['rank']} 的公开展示程度 {detail} 与候选人授权 {release} 不一致"
            )


# --------------------------------------------------------------------------
# 可重建性
# --------------------------------------------------------------------------

def _check_reconstructions(candidates: dict, materials: dict, stages: dict,
                           scores: dict, appeals: dict, results: list[dict],
                           reconstructions: list[dict], errors: list[str]) -> None:
    by_rank = {b["rank"]: b for b in reconstructions}
    for r in results:
        bundle = by_rank.get(r["rank"])
        if bundle is None:
            errors.append(f"名次 {r['rank']} 缺少证据重建包")
            continue
        cid = r["candidate_id"]
        if bundle["candidate_id"] != cid:
            errors.append(f"名次 {r['rank']} 重建包候选人不一致")
        evidence = {m["id"] for m in materials.values() if m["candidate_id"] == cid}
        if set(bundle["evidence_versions"]) != evidence:
            errors.append(
                f"名次 {r['rank']} 重建包证据版本不完整："
                f"缺少 {sorted(evidence - set(bundle['evidence_versions']))}"
            )
        for sid, stage in stages.items():
            if bundle["criteria_versions"].get(sid) != stage["criteria"]["version"]:
                errors.append(f"名次 {r['rank']} 重建包缺少 {sid} 阶段冻结标准")
            panel = {p["candidate_id"]: p["judge_ids"]
                     for p in stage["panels"]}.get(cid)
            if panel is not None and bundle["eligible_judges"].get(sid) != panel:
                errors.append(f"名次 {r['rank']} 重建包 {sid} 阶段有效评委与面板不一致")
        candidate_scores = {s["id"] for s in scores.values() if s["candidate_id"] == cid}
        if set(bundle["score_ids"]) != candidate_scores:
            errors.append(f"名次 {r['rank']} 重建包评分记录不完整")
        for aid in bundle.get("appeal_freeze_ids", []):
            appeal = appeals.get(aid)
            if appeal is None:
                errors.append(f"名次 {r['rank']} 重建包引用不存在的申诉 {aid}")
            elif appeal["candidate_id"] != cid:
                errors.append(f"名次 {r['rank']} 重建包引用了其他候选人的申诉 {aid}")

    if len(by_rank) != len(reconstructions):
        errors.append("重建包名次重复")


# --------------------------------------------------------------------------
# 汇总成绩
# --------------------------------------------------------------------------

def _check_aggregations(stages: dict, scores: dict, candidates: dict,
                        aggregated: list[dict], errors: list[str]) -> None:
    for row in aggregated:
        cid = row["candidate_id"]
        if cid not in candidates:
            errors.append(f"汇总成绩引用不存在的候选人 {cid}")
            continue
        for sid in STAGE_ORDER:
            values = [s["score"] for s in scores.values()
                      if s["candidate_id"] == cid and s["stage_id"] == sid]
            if not values:
                errors.append(f"候选人 {cid} 在 {sid} 阶段没有评分")
                continue
            actual_mean = sum(values) / len(values)
            if abs(actual_mean - row["panel_means"][sid]) > 0.01:
                errors.append(
                    f"候选人 {cid} 的 {sid} 阶段面板均值应为 {actual_mean:.2f}"
                )
        total = sum(row["panel_means"][sid] * row["stage_weights"][sid]
                    for sid in STAGE_ORDER)
        if abs(total - row["weighted_total"]) > 0.02:
            errors.append(f"候选人 {cid} 的加权总分应为 {total:.2f}")


# --------------------------------------------------------------------------
# 到期清理、未成年人与敏感经历
# --------------------------------------------------------------------------

def _check_retention(candidates: dict, materials: dict, scores: dict,
                     results: list[dict], constants: dict, retention: dict,
                     errors: list[str]) -> None:
    winner_ids = {r["candidate_id"] for r in results}
    unsuccessful = retention.get("unsuccessful", [])
    listed = {u["candidate_id"] for u in unsuccessful}

    missing_cleanup = set(candidates) - winner_ids - listed
    if missing_cleanup:
        errors.append(f"未入选者缺少到期清理安排: {', '.join(sorted(missing_cleanup))}")
    if listed & winner_ids:
        errors.append("获奖者不得出现在未入选者清理清单中")

    announced = min(date.fromisoformat(r["announced_at"]) for r in results)
    days = constants.get("unsuccessful_retention_days_after_announcement", 183)
    expected_purge = announced + timedelta(days=days)
    for u in unsuccessful:
        cid = u["candidate_id"]
        purge = date.fromisoformat(u["purge_after"])
        if purge != expected_purge:
            errors.append(f"候选人 {cid} 的清理日期应为 {expected_purge.isoformat()}")
        if date.fromisoformat(u["retain_until"]) != purge:
            errors.append(f"候选人 {cid} 的保留截止日与清理日不一致")
        material_ids = {m["id"] for m in materials.values() if m["candidate_id"] == cid}
        if set(u["material_ids"]) != material_ids:
            errors.append(f"候选人 {cid} 的清理清单未覆盖全部材料版本（含旧版）")
        score_ids = {s["id"] for s in scores.values() if s["candidate_id"] == cid}
        if set(u.get("score_ids", [])) != score_ids:
            errors.append(f"候选人 {cid} 的清理清单未覆盖全部评分记录")


def _check_consent_and_sensitivity(candidates: dict, materials: dict,
                                   results: list[dict], errors: list[str]) -> None:
    winner_ids = {r["candidate_id"] for r in results}
    for cid, candidate in candidates.items():
        consent = candidate["consent"]
        nominations = [m for m in materials.values()
                       if m["candidate_id"] == cid and m["type"] == "nomination"]
        current_nomination = next(
            (m for m in nominations if m.get("successor") is None), None
        )
        if candidate.get("minor"):
            if not consent.get("guardian_countersign"):
                errors.append(f"未成年候选人 {cid} 的提名授权缺少监护人联签")
            if current_nomination and not current_nomination.get("guardian_countersign"):
                errors.append(f"未成年候选人 {cid} 的提名材料缺少监护人联签标记")
        elif consent.get("guardian_countersign"):
            errors.append(f"成年候选人 {cid} 不应有监护人联签")

    for mid, m in materials.items():
        if m.get("sensitive") and PUBLISHED_SCOPE in m["visibility"]:
            errors.append(f"敏感材料 {mid} 未获公开发布授权，不得出现在 published 范围")

    for r in results:
        candidate = candidates[r["candidate_id"]]
        if candidate.get("minor") and r.get("public_detail_level") == "full":
            errors.append(f"未成年获奖者 {r['rank']} 不得按 full 程度公开展示")
        if candidate.get("sensitive_experience") and r["candidate_id"] in winner_ids:
            for mid in r["rationale"]["published_refs"]:
                if materials[mid].get("sensitive"):
                    errors.append(f"名次 {r['rank']} 不得公开敏感经历材料 {mid}")
