"""
分析说明生成护栏。

这一层不把「资料摘要」或「鉴定过程」当作最终事实来源，而是回到底层
病历事件、检查报告和已经确认的案件基础字段，先形成可核验的伤残候选清单。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.case import (
    AnalysisCandidate,
    AnalysisCandidateStatus,
    Case,
    FactImportance,
    FactReviewStatus,
    HospitalRecord,
    ImagingReport,
    MedicalEvent,
    Person,
    Report,
    StandardChunk,
    UnifiedFact,
)
from app.utils.standards import chunk_to_reference
from app.utils.standard_clause_catalog import CLAUSE_CATALOG, INJURY_CANDIDATE_SPECS, PERIOD_CANDIDATE_SPECS


def build_analysis_harness_payload(case_id: int, db: Session, attach_saved_status: bool = True) -> dict[str, Any]:
    """返回分析说明生成前的结构化核验材料。"""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        return {"case_context": {}, "candidates": [], "warnings": ["案件不存在。"]}

    person = db.query(Person).filter(Person.case_id == case_id).first()
    report = db.query(Report).filter(Report.case_id == case_id).first()
    records = db.query(HospitalRecord).filter(HospitalRecord.case_id == case_id).all()
    events = (
        db.query(MedicalEvent)
        .filter(MedicalEvent.case_id == case_id)
        .order_by(MedicalEvent.event_date.asc().nullslast(), MedicalEvent.id.asc())
        .all()
    )
    imaging = (
        db.query(ImagingReport)
        .filter(ImagingReport.case_id == case_id)
        .order_by(ImagingReport.report_datetime.asc().nullslast(), ImagingReport.report_date.asc().nullslast(), ImagingReport.id.asc())
        .all()
    )
    unified_facts = _select_unified_analysis_facts(case_id, db)

    candidates = _build_injury_candidates(db, records, events, imaging, unified_facts)
    candidates.extend(_build_period_candidates(db, records, events, imaging, case.entrustment_matter or "", unified_facts))
    if attach_saved_status:
        _attach_saved_candidate_status(case_id, db, candidates)
    references = _collect_standard_references(candidates)
    warnings = _build_warnings(case, records, events, imaging, candidates, unified_facts)

    return {
        "case_context": _build_case_context(case, person, report),
        "evidence_counts": {
            "hospital_records": len(records),
            "medical_events": len(events),
            "imaging_reports": len(imaging),
            "unified_facts": len(unified_facts),
        },
        "narrative_context": {
            "material_summary": (report.material_summary or "") if report else "",
            "appraisal_process": (report.appraisal_process or "") if report else "",
        },
        "candidates": candidates,
        "standard_references": references,
        "warnings": warnings,
    }


def sync_analysis_candidates(case_id: int, db: Session, _retry_on_conflict: bool = True) -> dict[str, Any]:
    """刷新候选清单并落库；保留医生已修改的状态。"""
    payload = build_analysis_harness_payload(case_id, db, attach_saved_status=False)
    now = datetime.now()
    existing_rows = {
        row.candidate_key: row
        for row in db.query(AnalysisCandidate).filter(AnalysisCandidate.case_id == case_id).all()
    }
    current_keys: set[str] = set()

    for item in payload.get("candidates") or []:
        key = item.get("id")
        if not key:
            continue
        current_keys.add(key)
        row = existing_rows.get(key)
        default_status = _default_candidate_status(item)
        if not row:
            row = AnalysisCandidate(
                case_id=case_id,
                candidate_key=key,
                status=default_status,
                created_at=now,
            )
            db.add(row)
        row.title = item.get("title")
        row.category = item.get("category")
        row.decision = item.get("decision")
        row.confidence = item.get("confidence")
        row.grade = item.get("grade")
        row.suggestion = item.get("suggestion")
        row.reason = item.get("reason")
        row.evidence_json = json.dumps(item.get("evidence") or [], ensure_ascii=False)
        row.standards_json = json.dumps(item.get("standards") or [], ensure_ascii=False)
        row.warnings_json = json.dumps(item.get("warnings") or [], ensure_ascii=False)
        row.source = "analysis_harness"
        row.updated_at = now
        if row.status not in AnalysisCandidateStatus.ALL:
            row.status = default_status

    for key, row in existing_rows.items():
        if key in current_keys or row.source != "analysis_harness":
            continue
        row.decision = "not_met"
        row.status = AnalysisCandidateStatus.EXCLUDED
        row.reason = "当前统一事实库已不再支持该候选；如需恢复，请在事实库中重新确认相关事实后刷新候选。"
        row.warnings_json = json.dumps(["当前事实库已排除或未保留该候选所需证据。"], ensure_ascii=False)
        row.updated_at = now

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if not _retry_on_conflict:
            raise
        return sync_analysis_candidates(case_id, db, _retry_on_conflict=False)
    payload["candidates"] = list_saved_analysis_candidates(case_id, db)
    payload["standard_references"] = _collect_standard_references(payload["candidates"])
    return payload


def list_saved_analysis_candidates(
    case_id: int,
    db: Session,
    *,
    include_excluded: bool = False,
) -> list[dict[str, Any]]:
    query = db.query(AnalysisCandidate).filter(AnalysisCandidate.case_id == case_id)
    if not include_excluded:
        query = query.filter(AnalysisCandidate.status != AnalysisCandidateStatus.EXCLUDED)
    rows = query.order_by(AnalysisCandidate.id.asc()).all()
    return [_candidate_row_to_dict(row) for row in rows]


def collect_candidate_standard_references(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _collect_standard_references(candidates)


def update_saved_analysis_candidate(
    candidate_id: int,
    db: Session,
    *,
    status: str | None = None,
    review_note: str | None = None,
) -> dict[str, Any] | None:
    row = db.query(AnalysisCandidate).filter(AnalysisCandidate.id == candidate_id).first()
    if not row:
        return None
    if status is not None:
        if status not in AnalysisCandidateStatus.ALL:
            raise ValueError(f"不支持的候选状态：{status}")
        row.status = status
    if review_note is not None:
        row.review_note = review_note
    row.updated_at = datetime.now()
    db.commit()
    db.refresh(row)
    return _candidate_row_to_dict(row)


def format_analysis_harness_for_prompt(payload: dict[str, Any]) -> str:
    """把护栏材料压缩成给大模型使用的提示词上下文。"""
    case_context = payload.get("case_context") or {}
    lines = [
        "【案件基础事实（已由前置模块确认，生成时必须以此为准）】",
        f"- 被鉴定人：{case_context.get('person_name') or '未明'}；性别：{case_context.get('gender') or '未明'}；出生日期：{case_context.get('birth_date') or '未明'}；事故时年龄：{case_context.get('age_at_accident') or '未明'}；当前年龄：{case_context.get('current_age') or '未明'}",
        f"- 委托单位：{case_context.get('entrusting_unit') or '未明'}",
        f"- 委托事项：{case_context.get('entrustment_matter') or '未明'}",
        f"- 事故时间：{case_context.get('accident_date') or '未明'}；事故地点：{case_context.get('accident_location') or '未明'}",
        "",
        "【伤残/三期候选清单（正式结论只能从 status=accepted 且 decision=met 中选择；needs_review/pending/excluded 不得直接定级）】",
    ]

    candidates = payload.get("candidates") or []
    if not candidates:
        lines.append("- 暂未形成可用候选。")
    for item in candidates:
        standards = "；".join(_format_standard_brief(ref) for ref in item.get("standards") or []) or "未匹配到规范条款"
        evidence = "；".join(_format_evidence_brief(ev) for ev in (item.get("evidence") or [])[:5]) or "未列出来源"
        lines.extend(
            [
                f"- [{item.get('decision')}; status={item.get('status') or _default_candidate_status(item)}] {item.get('title')}",
                f"  类别：{item.get('category') or ''}；建议/范围：{item.get('suggestion') or item.get('grade') or ''}",
                f"  理由：{item.get('reason') or ''}",
                f"  来源：{evidence}",
                f"  规范：{standards}",
            ]
        )
        warning_text = list(item.get("warnings") or [])
        if item.get("status") in (AnalysisCandidateStatus.EXCLUDED, AnalysisCandidateStatus.NEEDS_REVIEW):
            warning_text.append("该候选未被采信，正文不得写成正式结论。")
        if warning_text:
            lines.append(f"  风险提示：{'；'.join(warning_text)}")

    warnings = payload.get("warnings") or []
    if warnings:
        lines.extend(["", "【生成前风险提示】"])
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines).strip()


def validate_analysis_text(text: str, payload: dict[str, Any]) -> list[str]:
    """对 LLM 生成结果做轻量一致性检查。"""
    warnings: list[str] = []
    case_context = payload.get("case_context") or {}
    person_name = case_context.get("person_name") or ""
    gender = case_context.get("gender") or ""
    if person_name and gender:
        wrong_gender = "女" if gender == "男" else "男"
        compact = re.sub(r"\s+", "", text or "")
        if re.search(rf"{re.escape(person_name)}[，,、:：]*(?:现年\d+岁)?{wrong_gender}", compact):
            warnings.append(f"分析说明中可能出现被鉴定人性别错误，应以基本情况中的“{gender}”为准。")

    met_candidates = [
        item
        for item in payload.get("candidates") or []
        if item.get("decision") == "met" and (item.get("status") or _default_candidate_status(item)) == AnalysisCandidateStatus.ACCEPTED
    ]
    for item in met_candidates:
        required = item.get("must_mention") or []
        if required and not any(term in (text or "") for term in required):
            warnings.append(f"分析说明可能遗漏已满足候选：{item.get('title')}。")

    if "5.9.2" in (text or "") and "髋" in (text or "") and any(c.get("id") == "hip_joint_replacement" for c in met_candidates):
        warnings.append("髋关节置换候选已满足，但文本中出现 5.9.2，请人工核对条款号是否误引。")
    return warnings


def repair_analysis_text(text: str, payload: dict[str, Any]) -> str:
    """修正常见的条款号幻觉，不改动事实结论本身。"""
    if not text:
        return text
    met_ids = {
        item.get("id")
        for item in payload.get("candidates") or []
        if item.get("decision") == "met" and (item.get("status") or _default_candidate_status(item)) == AnalysisCandidateStatus.ACCEPTED
    }
    if "hip_joint_replacement" in met_ids:
        replacement = "《人体损伤致残程度分级》中关于“四肢任一大关节行关节假体置换术后”的规定"
        text = re.sub(
            r"《人体损伤致残程度分级》第5\.9\.2条(?:[（(][^）)]*[）)])?之规定",
            replacement,
            text,
        )
        text = re.sub(
            r"《人体损伤致残程度分级》第5\.9\.2条(?:[（(][^）)]*[）)])?",
            replacement,
            text,
        )
        text = re.sub(
            r"《人体损伤致残程度分级》第5\.9\.\d+条(?:\d+[）)](?:项|款))?[（(]四肢任一大关节行关节假体置换术后[）)]之规定",
            replacement,
            text,
        )
        text = re.sub(
            r"《人体损伤致残程度分级》第5\.9\.\d+条(?:\d+[）)](?:项|款))?[（(]四肢任一大关节行关节假体置换术后[）)]",
            replacement,
            text,
        )
    return text


def _build_case_context(case: Case, person: Person | None, report: Report | None) -> dict[str, Any]:
    person_name = (person.name if person else None) or case.person_name or "被鉴定人"
    accident_date = _extract_date(case.accident_date or "")
    birth_date = _extract_date((person.birth_date if person else "") or "")
    today = date.today()
    return {
        "case_id": case.id,
        "case_number": case.case_number,
        "person_name": person_name,
        "gender": person.gender if person else "",
        "birth_date": person.birth_date if person else "",
        "age_at_accident": _age_on(birth_date, accident_date),
        "current_age": _age_on(birth_date, today),
        "entrusting_unit": case.entrusting_unit,
        "entrustment_matter": case.entrustment_matter,
        "accident_date": case.accident_date,
        "accident_location": case.accident_location,
        "accident_description": case.accident_description,
        "case_facts": report.case_facts if report else "",
        "clinical_examination": case.clinical_examination,
    }


def _attach_saved_candidate_status(case_id: int, db: Session, candidates: list[dict[str, Any]]) -> None:
    rows = {
        row.candidate_key: row
        for row in db.query(AnalysisCandidate).filter(AnalysisCandidate.case_id == case_id).all()
    }
    for item in candidates:
        row = rows.get(item.get("id"))
        if row:
            item["candidate_db_id"] = row.id
            item["status"] = row.status
            item["review_note"] = row.review_note
        else:
            item["status"] = _default_candidate_status(item)


def _default_candidate_status(item: dict[str, Any]) -> str:
    if item.get("decision") == "met" and int(item.get("confidence") or 0) >= 80:
        return AnalysisCandidateStatus.ACCEPTED
    if item.get("decision") == "uncertain":
        return AnalysisCandidateStatus.NEEDS_REVIEW
    return AnalysisCandidateStatus.PENDING


def _select_unified_analysis_facts(case_id: int, db: Session) -> list[UnifiedFact]:
    facts = (
        db.query(UnifiedFact)
        .filter(
            UnifiedFact.case_id == case_id,
            UnifiedFact.fact_type.in_(["hospital_record", "medical_event", "imaging_report"]),
            UnifiedFact.review_status != FactReviewStatus.EXCLUDED,
            UnifiedFact.importance != FactImportance.EXCLUDED,
        )
        .all()
    )
    trusted = [
        fact
        for fact in facts
        if fact.review_status in (FactReviewStatus.CONFIRMED, FactReviewStatus.SYSTEM_VERIFIED)
    ]
    return trusted or facts


def _candidate_row_to_dict(row: AnalysisCandidate) -> dict[str, Any]:
    return {
        "candidate_db_id": row.id,
        "id": row.candidate_key,
        "title": row.title,
        "category": row.category,
        "decision": row.decision,
        "status": row.status,
        "confidence": row.confidence,
        "grade": row.grade,
        "suggestion": row.suggestion,
        "reason": row.reason,
        "evidence": _json_list(row.evidence_json),
        "standards": _json_list(row.standards_json),
        "warnings": _json_list(row.warnings_json),
        "review_note": row.review_note,
        "source": row.source,
        "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else None,
        "updated_at": row.updated_at.isoformat(timespec="seconds") if row.updated_at else None,
    }


def _build_injury_candidates(
    db: Session,
    records: list[HospitalRecord],
    events: list[MedicalEvent],
    imaging: list[ImagingReport],
    unified_facts: list[UnifiedFact] | None = None,
) -> list[dict[str, Any]]:
    corpus = _build_evidence_corpus(records, events, imaging, unified_facts)
    return _build_candidates_from_specs(db, corpus, INJURY_CANDIDATE_SPECS)


def _build_period_candidates(
    db: Session,
    records: list[HospitalRecord],
    events: list[MedicalEvent],
    imaging: list[ImagingReport],
    entrustment: str,
    unified_facts: list[UnifiedFact] | None = None,
) -> list[dict[str, Any]]:
    if not any(term in entrustment for term in ("误工", "护理", "营养", "三期")):
        return []

    corpus = _build_evidence_corpus(records, events, imaging, unified_facts)
    return _build_candidates_from_specs(db, corpus, PERIOD_CANDIDATE_SPECS, category="三期")


def _build_candidates_from_specs(
    db: Session,
    corpus: list[dict[str, Any]],
    specs: tuple[dict[str, Any], ...],
    *,
    category: str | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for spec in specs:
        evidence = _match_evidence(
            corpus,
            include=tuple(spec.get("include") or ()),
            require_any=tuple(spec.get("require_any") or ()),
        )
        if spec.get("rule") == "pelvis_deformity":
            evidence = [ev for ev in evidence if _has_positive_pelvis_injury(ev.get("text") or "")]
        if not evidence:
            continue
        candidate = _candidate_from_spec(db, spec, evidence, category=category)
        if candidate:
            candidates.append(candidate)
    return candidates


def _candidate_from_spec(
    db: Session,
    spec: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    category: str | None = None,
) -> dict[str, Any] | None:
    combined = " ".join(ev.get("text") or "" for ev in evidence)
    rule = spec.get("rule")
    candidate_id = spec["id"]
    title = spec["title"]
    candidate_category = category or spec.get("category") or ""
    decision = "uncertain"
    confidence = int(spec.get("confidence") or spec.get("uncertain_confidence") or 60)
    grade = spec.get("grade") or spec.get("uncertain_grade") or "需人工核对"
    suggestion = spec.get("suggestion") or spec.get("uncertain_suggestion") or "需人工核对"
    reason = spec.get("reason") or spec.get("uncertain_reason") or "材料中存在相关事实，但仍需结合条款成立条件人工核对。"
    clause_keys = tuple(spec.get("clause_keys") or ())

    if rule == "rib_count":
        max_count = _max_rib_count(combined)
        decision = "met" if max_count >= 6 else "uncertain"
        confidence = 88 if decision == "met" else 62
        grade = "十级" if decision == "met" else "需核对肋骨根数及是否畸形愈合"
        suggestion = (
            f"已识别肋骨骨折约{max_count}根，可按十级候选处理"
            if decision == "met"
            else f"已识别肋骨骨折约{max_count or '不明'}根，需人工核对"
        )
        reason = (
            "检查事实提示6根以上肋骨骨折，符合十级候选方向。"
            if decision == "met"
            else "检查事实提示肋骨骨折，但数量或畸形愈合条件尚不足以直接定级。"
        )
    elif rule == "pelvis_deformity":
        deformity_hit = any(term in combined for term in ("畸形愈合", "骨盆环不稳定", "骶髂关节分离", "闭孔形态不对称", "骨盆形态异常"))
        decision = "met" if deformity_hit else "uncertain"
        confidence = 84 if decision == "met" else 68
        grade = "十级" if decision == "met" else "需核对是否畸形愈合"
        suggestion = "可按十级候选处理" if decision == "met" else "需核对骨盆骨折是否形成畸形愈合或骨盆形态异常。"
        reason = (
            "材料提示骨盆两处以上骨折并存在畸形愈合或骨盆形态异常线索，符合十级候选方向。"
            if decision == "met"
            else "材料提示骨盆多发骨折，但目前证据多为骨折、骨痂或对位情况，尚不足以直接等同于畸形愈合。"
        )
    elif rule == "always_uncertain":
        decision = "uncertain"
        confidence_any = tuple(spec.get("confidence_any") or ())
        if confidence_any:
            confidence = int(spec.get("high_confidence") if any(term in combined for term in confidence_any) else spec.get("low_confidence"))
    elif rule == "brain_softening":
        has_softening = "脑软化灶" in combined or "软化灶" in combined
        has_neuro_sign = _has_neurologic_symptom_or_sign(combined)
        decision = "met" if has_softening and has_neuro_sign else "uncertain"
        confidence = 84 if decision == "met" else 62
        grade = "十级" if decision == "met" else "需核对是否伴神经系统症状或者体征"
        suggestion = "可按十级伤残候选处理" if decision == "met" else "需核对脑软化灶与神经系统症状/体征是否同时存在。"
        reason = (
            "材料提示颅脑损伤后遗脑软化灶，并有神经系统症状或体征线索，符合十级候选方向。"
            if decision == "met"
            else "材料提示颅脑损伤后遗改变，但尚未同时稳定确认脑软化灶及神经系统症状/体征。"
        )
    elif rule == "abdominal_repair":
        repair_evidence = [ev for ev in evidence if _has_abdominal_organ_repair(ev.get("text") or "")]
        if not repair_evidence:
            return None
        evidence = repair_evidence
        combined = " ".join(ev.get("text") or "" for ev in evidence)
        decision = "met"
        confidence = 82
        grade = "十级"
        suggestion = "可按十级伤残候选处理，仍需核对手术记录中修补的具体脏器。"
        reason = "材料提示肝、脾、胰腺、胃、肠、胆道或膈肌修补术后，符合腹部损伤十级候选方向。"
    elif rule == "dental_count":
        tooth_count = _max_tooth_loss_or_fracture_count(combined)
        alveolar_combo = _has_alveolar_tooth_loss_combo(combined, tooth_count)
        decision = "met" if tooth_count >= 7 or alveolar_combo else "uncertain"
        confidence = 82 if decision == "met" else 55
        grade = "十级" if decision == "met" else "需核对缺牙/折牙数量及牙槽骨缺损"
        suggestion = (
            "可按十级伤残候选处理"
            if decision == "met"
            else "需核对牙齿缺失或折断枚数、牙槽骨缺损范围和临床检查。"
        )
        reason = (
            "材料提示牙齿缺失或折断数量达到7枚以上，或牙槽骨部分缺损合并牙齿缺失/折断4枚以上，符合十级候选方向。"
            if decision == "met"
            else "材料存在牙齿或颌面损伤线索，但当前事实未稳定显示达到缺牙/折牙数量或牙槽骨缺损条件。"
        )
    elif rule == "tib_fib_period":
        open_fracture = "开放" in combined
        candidate_id = "period_tib_fib_open_fracture" if open_fracture else "period_tib_fib_fracture"
        decision = "met"
        confidence = 84 if open_fracture else 78
        grade = "三期范围"
        suggestion = (
            "开放性骨折：误工期150-180日；护理期60-90日；营养期60-90日，并结合多发损伤综合评定"
            if open_fracture
            else "胫腓骨骨折：误工期120-180日；护理期30-90日；营养期60-90日，并结合多发损伤综合评定"
        )
        reason = "材料提示胫腓骨骨折，可作为三期评定的重要损伤之一；如为开放性骨折，应优先采用开放性骨折区间。"
        clause_keys = ("period_tib_fib_open_fracture",) if open_fracture else ("period_multiple_injuries", "period_upper_limit")
    elif spec.get("met_any"):
        decision = "met" if any(term in combined for term in tuple(spec.get("met_any") or ())) else "uncertain"
        confidence = int(spec.get("met_confidence") if decision == "met" else spec.get("uncertain_confidence"))
        grade = spec.get("met_grade") if decision == "met" else spec.get("uncertain_grade")
        suggestion = spec.get("met_suggestion") if decision == "met" else spec.get("uncertain_suggestion")
        reason = spec.get("met_reason") if decision == "met" else spec.get("uncertain_reason")
    else:
        decision = "met"
        confidence = int(spec.get("confidence") or 80)
        grade = spec.get("grade") or "三期范围"

    must_mention = tuple(spec.get("must_mention") or ())
    if not must_mention and candidate_category == "三期":
        must_mention = ("误工", "护理", "营养")

    return _candidate(
        db,
        candidate_id=candidate_id,
        title=title,
        category=candidate_category,
        decision=decision,
        confidence=confidence,
        grade=grade,
        suggestion=suggestion,
        reason=reason,
        evidence=evidence,
        clause_keys=clause_keys,
        must_mention=must_mention,
    )


def _candidate(
    db: Session,
    *,
    candidate_id: str,
    title: str,
    category: str,
    decision: str,
    confidence: int,
    grade: str,
    suggestion: str,
    reason: str,
    evidence: list[dict[str, Any]],
    clause_keys: tuple[str, ...],
    must_mention: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "id": candidate_id,
        "title": title,
        "category": category,
        "decision": decision,
        "confidence": confidence,
        "grade": grade,
        "suggestion": suggestion,
        "reason": reason,
        "evidence": _dedupe_evidence(evidence),
        "standards": [_standard_from_clause(db, key) for key in clause_keys],
        "must_mention": list(must_mention),
        "warnings": _candidate_warnings(decision, evidence),
    }


def _candidate_warnings(decision: str, evidence: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    if decision == "uncertain":
        warnings.append("该候选证据不足或条款映射未稳定，不能直接写成正式结论。")
    if any(ev.get("review_status") not in (FactReviewStatus.CONFIRMED, FactReviewStatus.SYSTEM_VERIFIED, None, "") for ev in evidence):
        warnings.append("部分来源事实尚未人工确认。")
    if not any(ev.get("source_pages") for ev in evidence):
        warnings.append("部分来源缺少页码，建议回到原图核对。")
    return warnings


def _standard_from_clause(db: Session, clause_key: str) -> dict[str, Any]:
    clause = CLAUSE_CATALOG.get(clause_key, {})
    ref = _find_standard_reference(db, clause)
    if not ref:
        ref = {
            "id": None,
            "document_id": None,
            "standard_name": clause.get("standard_name", ""),
            "section_code": None,
            "section_title": None,
            "text": clause.get("clause_label", ""),
            "snippet": clause.get("clause_label", ""),
            "score": 0,
        }
    ref = _normalize_catalog_reference(ref, clause)
    ref.update(
        {
            "clause_key": clause_key,
            "category": clause.get("category", ""),
            "grade": clause.get("grade", ""),
            "clause_label": clause.get("clause_label", ""),
            "reference_role": "primary",
        }
    )
    return ref


def _normalize_catalog_reference(ref: dict[str, Any], clause: dict[str, Any]) -> dict[str, Any]:
    """Known catalog clauses should display the known clause, not the broad chunk title.

    OCR/markdown chunks may contain several clauses. A chunk headed "全身冻伤"
    can still contain Appendix A.4 later in the text, so using the chunk header
    as the citation title leaks unrelated standards into the UI and prompt.
    """
    clause_label = (clause.get("clause_label") or "").strip()
    if not clause_label:
        return ref

    normalized = dict(ref)
    code, title = _split_clause_label(clause_label)
    normalized["section_code"] = code
    normalized["section_title"] = title or clause_label
    normalized["text"] = clause_label
    normalized["snippet"] = clause_label
    return normalized


def _split_clause_label(label: str) -> tuple[str | None, str]:
    match = re.match(r"^((?:附录\s*)?[A-ZＡ-Ｚ]?\d+(?:\.\d+)+|附录\s*[A-ZＡ-Ｚ]\.\d+)\s*(.*)$", label)
    if not match:
        return None, label
    code = re.sub(r"\s+", "", match.group(1))
    return code, match.group(2).strip() or label


def _find_standard_reference(db: Session, clause: dict[str, Any]) -> dict[str, Any] | None:
    standard_name = clause.get("standard_name") or ""
    terms = clause.get("search_terms") or ()
    if not standard_name or not terms:
        return None

    best: tuple[int, StandardChunk] | None = None
    base = db.query(StandardChunk).filter(StandardChunk.standard_name.like(f"%{standard_name}%"))
    for chunk in base.limit(3000).all():
        text = chunk.chunk_text or ""
        score = sum(12 for term in terms if term and term in text)
        if clause.get("grade") and clause["grade"] in text:
            score += 5
        if score and (not best or score > best[0]):
            best = (score, chunk)
    if not best:
        return None
    return chunk_to_reference(best[1], best[0])


def _build_evidence_corpus(
    records: list[HospitalRecord],
    events: list[MedicalEvent],
    imaging: list[ImagingReport],
    unified_facts: list[UnifiedFact] | None = None,
) -> list[dict[str, Any]]:
    if unified_facts:
        return _build_unified_evidence_corpus(unified_facts)

    rows: list[dict[str, Any]] = []
    for record in records:
        text = _join_text(
            record.hospital_name,
            record.chief_complaint,
            record.present_illness_history,
            record.physical_examination,
            record.admission_diagnosis,
            record.treatment_process,
            record.discharge_diagnosis,
            record.discharge_orders,
        )
        rows.append(
            {
                "kind": "hospital_record",
                "id": record.id,
                "date": record.admission_date or record.discharge_date or "",
                "hospital_name": record.hospital_name or "",
                "title": f"{record.hospital_name or ''}住院病历",
                "summary": _clip(text, 260),
                "quote": _clip(record.treatment_process or record.discharge_diagnosis or text, 180),
                "text": text,
                "source_pages": [],
                "source_material_ids": [record.material_id] if record.material_id else [],
                "review_status": record.review_status,
            }
        )
    for event in events:
        text = _join_text(
            event.hospital_name,
            event.event_type,
            event.event_date,
            event.title,
            event.summary,
            event.diagnosis,
            event.findings,
            event.treatment,
            event.source_quote,
            event.material_subtype,
        )
        rows.append(
            {
                "kind": "medical_event",
                "id": event.id,
                "date": event.event_date or "",
                "hospital_name": event.hospital_name or "",
                "title": event.title or event.material_subtype or event.event_type or "病历事实",
                "summary": _clip(event.summary or event.treatment or event.findings or text, 260),
                "quote": _clip(event.source_quote or event.summary or text, 180),
                "text": text,
                "source_pages": _json_list(event.source_page_numbers),
                "source_material_ids": _json_list(event.source_material_ids),
                "review_status": event.review_status,
            }
        )
    for report in imaging:
        text = _join_text(
            report.hospital_name,
            report.report_date,
            report.exam_type,
            report.exam_part,
            report.film_number,
            report.report_content,
        )
        rows.append(
            {
                "kind": "imaging_report",
                "id": report.id,
                "date": report.report_datetime or report.report_date or "",
                "hospital_name": report.hospital_name or "",
                "title": f"{report.exam_type or '检查'} {report.exam_part or ''}".strip(),
                "summary": _clip(report.report_content or text, 260),
                "quote": _clip(report.report_content or text, 180),
                "text": text,
                "source_pages": _json_list(report.source_page_numbers),
                "source_material_ids": _json_list(report.source_material_ids),
                "review_status": report.review_status,
            }
        )
    return rows


def _build_unified_evidence_corpus(facts: list[UnifiedFact]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fact in facts:
        text = _join_text(
            fact.hospital_name,
            fact.fact_date,
            fact.fact_type,
            fact.fact_role,
            fact.title,
            fact.summary,
            fact.source_quote,
        )
        rows.append(
            {
                "kind": fact.source_kind or fact.fact_type,
                "id": fact.source_id or fact.id,
                "date": fact.fact_date or "",
                "hospital_name": fact.hospital_name or "",
                "title": fact.title or fact.fact_role or fact.fact_type or "事实",
                "summary": _clip(fact.summary or fact.source_quote or text, 260),
                "quote": _clip(fact.source_quote or fact.summary or text, 180),
                "text": text,
                "source_pages": _json_list(fact.source_page_numbers),
                "source_material_ids": _json_list(fact.source_material_ids),
                "review_status": fact.review_status,
                "importance": fact.importance,
                "unified_fact_id": fact.id,
            }
        )
    return rows


def _match_evidence(
    corpus: list[dict[str, Any]],
    *,
    include: tuple[str, ...],
    require_any: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    matched: list[tuple[int, dict[str, Any]]] = []
    for row in corpus:
        text = row.get("text") or ""
        if not any(term in text for term in include):
            continue
        if require_any and not any(term in text for term in require_any):
            continue
        score = _evidence_score(row, include)
        matched.append((score, row))
    matched.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in matched[:10]]


def _evidence_score(row: dict[str, Any], terms: tuple[str, ...]) -> int:
    text = row.get("text") or ""
    score = sum(len(term) for term in terms if term in text)
    if row.get("kind") == "medical_event":
        score += 8
    elif row.get("kind") == "imaging_report":
        score += 5
    elif row.get("kind") == "hospital_record":
        score -= 4
    if row.get("source_pages"):
        score += 4
    if row.get("review_status") == FactReviewStatus.CONFIRMED:
        score += 3
    elif row.get("review_status") == FactReviewStatus.SYSTEM_VERIFIED:
        score += 2
    if any(term in text for term in ("全髋关节置换", "髋关节置换", "人工关节", "股骨头置换")):
        score += 14
    if _max_rib_count(text) >= 6:
        score += 15
    if "骨痂" in text or "畸形愈合" in text:
        score += 5
    if "已施手术" in text or "手术开始时间" in text:
        score += 4
    return score


def _max_rib_count(text: str) -> int:
    max_count = 0
    normalized = text.replace("－", "-").replace("—", "-").replace("～", "-").replace("至", "-")
    for match in re.finditer(r"第?\s*(\d{1,2})\s*[-~]\s*(\d{1,2})\s*肋", normalized):
        start, end = int(match.group(1)), int(match.group(2))
        if end >= start:
            max_count = max(max_count, end - start + 1)
    explicit = re.search(r"肋骨骨折\s*(\d+)\s*根以上", normalized)
    if explicit:
        max_count = max(max_count, int(explicit.group(1)))
    side_numbers = re.findall(r"(?:左|右|双侧)?第\s*(\d{1,2})\s*肋", normalized)
    if len(set(side_numbers)) >= max_count:
        max_count = len(set(side_numbers))
    return max_count


def _has_positive_pelvis_injury(text: str) -> bool:
    """Avoid treating a pelvis scan title or normal pelvis description as injury evidence."""
    if not text:
        return False
    normalized = re.sub(r"\s+", "", text)
    patterns = (
        r"骨盆[^。；;，,]{0,16}(?:多发)?骨折",
        r"耻骨[^。；;，,]{0,16}骨折",
        r"坐骨[^。；;，,]{0,16}骨折",
        r"骶骨[^。；;，,]{0,16}骨折",
        r"髋臼[^。；;，,]{0,16}骨折",
        r"骶髂关节[^。；;，,]{0,16}(?:分离|骨折)",
    )
    negative_terms = ("未见", "无明显", "未明确", "尚可", "正常")
    for pattern in patterns:
        for match in re.finditer(pattern, normalized):
            window = normalized[max(0, match.start() - 12): match.end() + 12]
            if any(term in window for term in negative_terms):
                continue
            return True
    return False


def _has_neurologic_symptom_or_sign(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text or "")
    if not normalized:
        return False
    positive_terms = (
        "神经系统症状",
        "神经系统体征",
        "肌力",
        "偏瘫",
        "截瘫",
        "肢体麻木",
        "感觉障碍",
        "运动障碍",
        "病理反射",
        "腱反射",
        "失语",
        "癫痫",
        "头痛",
        "头晕",
        "记忆力",
        "认知",
    )
    negative_windows = ("未见神经系统", "无神经系统", "神经系统未见", "肌力正常", "病理反射未引出")
    if any(term in normalized for term in negative_windows):
        return False
    return any(term in normalized for term in positive_terms)


def _has_abdominal_organ_repair(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text or "")
    if not normalized or "修补" not in normalized:
        return False
    # 肠系膜修补本身不是“胃、肠或者胆道修补术后”，不能据此直接定级。
    if "肠系膜修补" in normalized and not any(term in normalized for term in ("肠壁修补", "小肠修补", "结肠修补", "胃修补", "胆道修补")):
        return False
    direct_terms = (
        "肝修补",
        "肝破裂修补",
        "脾修补",
        "脾破裂修补",
        "胰腺修补",
        "胃修补",
        "胃破裂修补",
        "小肠修补",
        "小肠破裂修补",
        "结肠修补",
        "结肠破裂修补",
        "肠壁修补",
        "肠破裂修补",
        "胆道修补",
        "胆管修补",
        "膈肌修补",
    )
    if any(term in normalized for term in direct_terms):
        return True
    return bool(re.search(r"(肝|脾|胰腺|胃|小肠|结肠|肠壁|胆道|胆管|膈肌)[^。；;，,]{0,12}修补", normalized))


def _max_tooth_loss_or_fracture_count(text: str) -> int:
    normalized = re.sub(r"\s+", "", text or "")
    if not normalized:
        return 0
    max_count = 0
    patterns = (
        r"(?:牙齿|牙|恒牙|乳牙)?(?:缺失|脱落|折断|折裂|缺损)[^。；;，,]{0,8}(\d{1,2})\s*(?:枚|颗|个|只)",
        r"(\d{1,2})\s*(?:枚|颗|个|只)(?:牙齿|牙)?(?:缺失|脱落|折断|折裂|缺损)",
        r"缺牙[^。；;，,]{0,8}(\d{1,2})\s*(?:枚|颗|个|只)",
        r"折牙[^。；;，,]{0,8}(\d{1,2})\s*(?:枚|颗|个|只)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, normalized):
            max_count = max(max_count, int(match.group(1)))
    return max_count


def _has_alveolar_tooth_loss_combo(text: str, tooth_count: int | None = None) -> bool:
    normalized = re.sub(r"\s+", "", text or "")
    if "牙槽骨" not in normalized:
        return False
    count = tooth_count if tooth_count is not None else _max_tooth_loss_or_fracture_count(normalized)
    if count >= 4:
        return True
    return bool(re.search(r"牙槽骨[^。；;，,]{0,16}(?:缺损|骨折|吸收)[^。；;，,]{0,20}(?:牙齿|牙)[^。；;，,]{0,10}(?:缺失|脱落|折断|折裂)", normalized))


def _collect_standard_references(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    refs: list[dict[str, Any]] = []
    for item in candidates:
        status = item.get("status") or _default_candidate_status(item)
        if item.get("decision") != "met" or status != AnalysisCandidateStatus.ACCEPTED:
            continue
        for ref in item.get("standards") or []:
            key = str(ref.get("clause_key") or ref.get("id") or ref.get("clause_label"))
            if key in seen:
                continue
            seen.add(key)
            refs.append(ref)
    return refs


def _build_warnings(
    case: Case,
    records: list[HospitalRecord],
    events: list[MedicalEvent],
    imaging: list[ImagingReport],
    candidates: list[dict[str, Any]],
    unified_facts: list[UnifiedFact] | None = None,
) -> list[str]:
    warnings: list[str] = []
    if not case.entrustment_matter:
        warnings.append("委托事项为空，分析说明无法判断哪些损伤应进入正式结论。")
    if unified_facts:
        if not any(fact.fact_type == "medical_event" for fact in unified_facts):
            warnings.append("统一事实库中缺少病历事件型事实，建议先建立或确认病历事实。")
        if not any(fact.fact_type == "imaging_report" for fact in unified_facts):
            warnings.append("统一事实库中缺少检查事实，影像依据不足。")
    elif not events:
        warnings.append("尚无病历事件型事实，分析说明只能依赖病历汇总字段，来源页精度会降低。")
    if not unified_facts and not imaging:
        warnings.append("尚无检查事实，影像依据不足。")
    if not any(item.get("decision") == "met" for item in candidates):
        warnings.append("未形成 decision=met 的伤残候选，正式伤残结论需人工核对。")
    unreviewed = sum(
        1
        for item in candidates
        for ev in item.get("evidence") or []
        if ev.get("review_status") == FactReviewStatus.PENDING
    )
    if unreviewed:
        warnings.append(f"有 {unreviewed} 条候选来源事实尚未人工确认。")
    return warnings


def _format_standard_brief(ref: dict[str, Any]) -> str:
    title = ref.get("standard_name") or "规范"
    code = ref.get("section_code") or ref.get("section_title") or ref.get("clause_label") or ""
    grade = f"（{ref.get('grade')}）" if ref.get("grade") else ""
    return f"{title}{grade} {code}".strip()


def _format_evidence_brief(ev: dict[str, Any]) -> str:
    pages = ev.get("source_pages") or []
    page_text = f"页码{','.join(str(p) for p in pages)}" if pages else "页码未明"
    date_text = f"{ev.get('date')} " if ev.get("date") else ""
    title = ev.get("title") or ev.get("kind") or "来源"
    quote = ev.get("quote") or ev.get("summary") or ""
    return f"{date_text}{title}（{page_text}）：{_clip(quote, 80)}"


def _dedupe_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, int]] = set()
    result: list[dict[str, Any]] = []
    for item in evidence:
        key = (item.get("kind") or "", int(item.get("id") or 0))
        if key in seen:
            continue
        seen.add(key)
        result.append({k: v for k, v in item.items() if k != "text"})
    return result


def _join_text(*parts: Any) -> str:
    return "\n".join(str(part).strip() for part in parts if part is not None and str(part).strip())


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit]


def _json_list(raw: Any) -> list[Any]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _extract_date(text: str) -> date | None:
    if not text:
        return None
    match = re.search(r"(\d{4})[年\-/\.](\d{1,2})[月\-/\.](\d{1,2})", text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _age_on(birth: date | None, when: date | None) -> int | None:
    if not birth or not when:
        return None
    return when.year - birth.year - ((when.month, when.day) < (birth.month, birth.day))
