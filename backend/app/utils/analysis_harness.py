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
    FactReviewStatus,
    HospitalRecord,
    ImagingReport,
    MedicalEvent,
    Person,
    Report,
    StandardChunk,
)
from app.utils.standards import chunk_to_reference


CLAUSE_CATALOG: dict[str, dict[str, Any]] = {
    "disability_hip_joint_replacement_9": {
        "standard_name": "人体损伤致残程度分级",
        "category": "伤残等级",
        "grade": "九级",
        "clause_label": "脊柱、骨盆及四肢损伤：四肢任一大关节行关节假体置换术后",
        "search_terms": ("四肢任一大关节行关节假体置换术后", "关节假体置换术后", "大关节行关节假体"),
    },
    "disability_rib_fractures_6_10": {
        "standard_name": "人体损伤致残程度分级",
        "category": "伤残等级",
        "grade": "十级",
        "clause_label": "颈部及胸部损伤：肋骨骨折6根以上，或者肋骨部分缺失2根以上；肋骨骨折4根以上并后遗2处畸形愈合",
        "search_terms": ("肋骨骨折6根以上", "肋骨部分缺失2根以上", "畸形愈合"),
    },
    "period_femoral_neck_fracture_surgery": {
        "standard_name": "人身损害误工期、护理期、营养期评定规范",
        "category": "三期",
        "clause_label": "10.2.8 股骨颈骨折；手术治疗：误工180-365日，护理90-150日，营养90-180日",
        "ranges": {"误工期": "180-365日", "护理期": "90-150日", "营养期": "90-180日"},
        "search_terms": ("股骨颈骨折", "手术治疗：误工180", "护理90", "营养90"),
    },
    "period_multiple_injuries": {
        "standard_name": "人身损害误工期、护理期、营养期评定规范",
        "category": "三期",
        "clause_label": "附录A.4 多处损伤不能简单累加，应以较长期限为主并结合其他损伤综合考虑",
        "search_terms": ("多处损伤", "不能将多处损伤", "简单累加"),
    },
    "period_upper_limit": {
        "standard_name": "人身损害误工期、护理期、营养期评定规范",
        "category": "三期",
        "clause_label": "附录A.5 受伤后至定残之日前一日的时间已超过误工期上限的，可计算至定残日前一日",
        "search_terms": ("定残之日前一日", "误工期上限", "超过误工期"),
    },
}


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

    candidates = _build_injury_candidates(db, records, events, imaging)
    candidates.extend(_build_period_candidates(db, records, events, imaging, case.entrustment_matter or ""))
    if attach_saved_status:
        _attach_saved_candidate_status(case_id, db, candidates)
    references = _collect_standard_references(candidates)
    warnings = _build_warnings(case, records, events, imaging, candidates)

    return {
        "case_context": _build_case_context(case, person, report),
        "evidence_counts": {
            "hospital_records": len(records),
            "medical_events": len(events),
            "imaging_reports": len(imaging),
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

    for item in payload.get("candidates") or []:
        key = item.get("id")
        if not key:
            continue
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


def list_saved_analysis_candidates(case_id: int, db: Session) -> list[dict[str, Any]]:
    rows = (
        db.query(AnalysisCandidate)
        .filter(AnalysisCandidate.case_id == case_id)
        .order_by(AnalysisCandidate.id.asc())
        .all()
    )
    return [_candidate_row_to_dict(row) for row in rows]


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
) -> list[dict[str, Any]]:
    corpus = _build_evidence_corpus(records, events, imaging)
    candidates: list[dict[str, Any]] = []

    hip_evidence = _match_evidence(
        corpus,
        include=("髋关节置换", "全髋关节置换", "半髋关节置换", "人工关节", "股骨头置换", "股骨颈骨折"),
        require_any=("置换", "人工关节", "股骨颈"),
    )
    if hip_evidence:
        replacement_hit = any(any(term in ev["text"] for term in ("髋关节置换", "人工关节", "股骨头置换", "全髋")) for ev in hip_evidence)
        decision = "met" if replacement_hit else "uncertain"
        candidates.append(
            _candidate(
                db,
                candidate_id="hip_joint_replacement",
                title="左髋关节置换术后",
                category="伤残等级",
                decision=decision,
                confidence=92 if decision == "met" else 70,
                grade="九级" if decision == "met" else "需核对是否已行关节假体置换",
                suggestion="可按九级伤残候选处理" if decision == "met" else "需核对手术方式和术后影像",
                reason="病历/手术或术后检查材料提示股骨颈骨折并行髋关节置换，属于四肢大关节假体置换术后。" if decision == "met" else "材料提示髋部重大损伤，但尚需确认是否为关节假体置换术后。",
                evidence=hip_evidence,
                clause_keys=("disability_hip_joint_replacement_9",),
                must_mention=("髋关节置换", "人工关节", "股骨颈骨折"),
            )
        )

    rib_evidence = _match_evidence(corpus, include=("肋骨骨折", "肋骨陈旧性骨折", "肋骨骨痂", "肋骨畸形愈合"))
    if rib_evidence:
        max_count = _max_rib_count(" ".join(ev["text"] for ev in rib_evidence))
        decision = "met" if max_count >= 6 else "uncertain"
        candidates.append(
            _candidate(
                db,
                candidate_id="rib_fractures",
                title="多发肋骨骨折",
                category="伤残等级",
                decision=decision,
                confidence=88 if decision == "met" else 62,
                grade="十级" if decision == "met" else "需核对肋骨根数及是否畸形愈合",
                suggestion=f"已识别肋骨骨折约{max_count}根，可按十级候选处理" if decision == "met" else f"已识别肋骨骨折约{max_count or '不明'}根，需人工核对",
                reason="检查事实提示6根以上肋骨骨折，符合十级候选方向。" if decision == "met" else "检查事实提示肋骨骨折，但数量或畸形愈合条件尚不足以直接定级。",
                evidence=rib_evidence,
                clause_keys=("disability_rib_fractures_6_10",),
                must_mention=("肋骨骨折",),
            )
        )

    abdominal_evidence = _match_evidence(corpus, include=("肠系膜", "腹腔探查", "结肠浆膜", "肠修补", "脾破裂", "脾脏"))
    if abdominal_evidence:
        candidates.append(
            _candidate(
                db,
                candidate_id="abdominal_injury",
                title="腹部探查及肠系膜/肠壁损伤",
                category="伤残等级",
                decision="uncertain",
                confidence=58,
                grade="需人工核对",
                suggestion="仅作为分析时需核对的损伤事实，不自动定级",
                reason="病历提示腹部探查及肠系膜或肠壁相关处理，但现阶段缺少稳定的结构化条款映射，且需区分疑似脾破裂与术中实际发现。",
                evidence=abdominal_evidence,
                clause_keys=(),
                must_mention=("肠系膜", "腹腔探查"),
            )
        )

    dental_evidence = _match_evidence(corpus, include=("牙缺失", "牙齿缺失", "牙槽骨", "下颌骨", "上颌骨", "口腔"))
    if dental_evidence:
        candidates.append(
            _candidate(
                db,
                candidate_id="dental_maxillofacial_injury",
                title="口腔颌面部/牙齿损伤",
                category="伤残等级",
                decision="uncertain",
                confidence=55,
                grade="需人工核对",
                suggestion="需核对缺牙数量、牙槽骨损伤范围和临床检查",
                reason="材料存在牙齿或颌面部损伤线索，但伤残条款通常依赖缺牙数量、牙槽骨损伤范围或功能影响，需人工确认。",
                evidence=dental_evidence,
                clause_keys=(),
                must_mention=("牙", "颌"),
            )
        )

    dvt_evidence = _match_evidence(corpus, include=("深静脉血栓", "静脉血栓", "血栓形成"))
    if dvt_evidence:
        candidates.append(
            _candidate(
                db,
                candidate_id="dvt_relatedness",
                title="下肢深静脉血栓",
                category="因果关系/辅助事实",
                decision="uncertain",
                confidence=45,
                grade="不自动作为伤残结论",
                suggestion="需结合外伤、制动、既往疾病及临床因果关系判断",
                reason="血栓可能与创伤后制动、基础疾病或治疗过程有关，不能仅凭检查报告直接认定为委托事项中的伤残依据。",
                evidence=dvt_evidence,
                clause_keys=(),
                must_mention=("血栓",),
            )
        )

    return candidates


def _build_period_candidates(
    db: Session,
    records: list[HospitalRecord],
    events: list[MedicalEvent],
    imaging: list[ImagingReport],
    entrustment: str,
) -> list[dict[str, Any]]:
    if not any(term in entrustment for term in ("误工", "护理", "营养", "三期")):
        return []

    corpus = _build_evidence_corpus(records, events, imaging)
    femoral_evidence = _match_evidence(corpus, include=("股骨颈骨折", "髋关节置换", "人工关节", "股骨头置换"))
    candidates: list[dict[str, Any]] = []
    if femoral_evidence:
        candidates.append(
            _candidate(
                db,
                candidate_id="period_femoral_neck_fracture_surgery",
                title="股骨颈骨折手术治疗的误工期、护理期、营养期",
                category="三期",
                decision="met",
                confidence=86,
                grade="三期范围",
                suggestion="误工期180-365日；护理期90-150日；营养期90-180日，并结合多发损伤综合评定",
                reason="材料提示股骨颈骨折并经手术治疗，可作为三期评定的主要损伤之一。",
                evidence=femoral_evidence,
                clause_keys=("period_femoral_neck_fracture_surgery", "period_multiple_injuries", "period_upper_limit"),
                must_mention=("误工", "护理", "营养"),
            )
        )
    return candidates


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
    if any(ev.get("review_status") not in (FactReviewStatus.CONFIRMED, None, "") for ev in evidence):
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
) -> list[dict[str, Any]]:
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
) -> list[str]:
    warnings: list[str] = []
    if not case.entrustment_matter:
        warnings.append("委托事项为空，分析说明无法判断哪些损伤应进入正式结论。")
    if not events:
        warnings.append("尚无病历事件型事实，分析说明只能依赖病历汇总字段，来源页精度会降低。")
    if not imaging:
        warnings.append("尚无检查事实，影像依据不足。")
    if not any(item.get("decision") == "met" for item in candidates):
        warnings.append("未形成 decision=met 的伤残候选，正式伤残结论需人工核对。")
    unreviewed = sum(1 for item in candidates for ev in item.get("evidence") or [] if ev.get("review_status") == FactReviewStatus.PENDING)
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
