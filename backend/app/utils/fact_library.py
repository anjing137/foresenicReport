"""统一事实库同步与格式化工具。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Iterable

from sqlalchemy.orm import Session

from app.models.case import (
    Case,
    FactImportance,
    FactReviewStatus,
    HospitalRecord,
    ImagingReport,
    Material,
    MaterialGroup,
    MedicalEvent,
    UnifiedFact,
)


EVENT_TYPE_LABELS = {
    "admission": "入院",
    "discharge": "出院",
    "surgery": "手术",
    "progress": "病程",
    "consultation": "会诊",
    "exam": "检查",
    "treatment": "治疗",
    "medication": "用药",
    "diagnosis": "诊断",
    "other": "其他",
}

FACT_TYPE_ORDER = {
    "hospital_record": 10,
    "medical_event": 20,
    "imaging_report": 30,
}

EVENT_TYPE_ORDER = {
    "admission": 10,
    "diagnosis": 20,
    "exam": 30,
    "surgery": 40,
    "treatment": 50,
    "progress": 60,
    "medication": 70,
    "consultation": 80,
    "discharge": 90,
    "other": 100,
}

CORE_EVENT_TYPES = {"admission", "discharge", "surgery", "diagnosis", "exam"}
CORE_KEYWORDS = (
    "骨折",
    "破裂",
    "损伤",
    "出血",
    "血肿",
    "脾",
    "肋骨",
    "股骨",
    "锁骨",
    "颅脑",
    "手术",
    "内固定",
    "置换",
    "切除",
    "修补",
)

BODY_PART_KEYWORDS = (
    "胫腓骨",
    "胫骨",
    "腓骨",
    "骨盆",
    "髂骨",
    "骶骨",
    "耻骨",
    "坐骨",
    "股骨",
    "髋",
    "小腿",
    "下肢",
    "肋骨",
    "锁骨",
    "肩胛骨",
    "肩",
    "颅脑",
    "头颅",
    "额部",
    "胸部",
    "腹部",
    "上腹部",
    "下腹部",
    "脾",
    "膝",
    "踝",
    "足",
)

LAB_REPORT_KEYWORDS = (
    "血常规",
    "尿常规",
    "凝血",
    "肝功能",
    "肾功能",
    "电解质",
    "葡萄糖",
    "白蛋白",
    "胆红素",
    "乙型肝炎",
    "丙型肝炎",
    "梅毒",
    "HIV",
    "免疫缺陷病毒",
    "检验报告",
    "检验项目",
)

IMAGING_TYPE_KEYWORDS = ("CT", "DR", "MRI", "X线", "CR", "超声", "彩超", "B超", "磁共振", "平片")

SYSTEM_VERIFIED_NOTE = "系统预采信：病历诊断/手术记录与检查事实相互印证。"
MANUAL_REVIEW_STATUSES = {
    FactReviewStatus.CONFIRMED,
    FactReviewStatus.NEEDS_EDIT,
    FactReviewStatus.EXCLUDED,
}
SYSTEM_VERIFY_EVENT_ROLES = {"admission", "discharge", "surgery", "diagnosis", "exam"}
POSITIVE_FINDING_KEYWORDS = (
    "骨折",
    "断裂",
    "内固定",
    "置换",
    "骨痂",
    "缺如",
    "缺失",
    "破裂",
    "裂伤",
    "血肿",
    "出血",
    "积液",
    "挫伤",
    "术后",
    "修补",
    "脱落",
)
WEAK_ONLY_KEYWORDS = ("未见明显异常", "未见异常", "未见明确", "考虑", "疑似", "待排", "建议复查")


def _loads_list(value) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _dumps_list(values: Iterable | None) -> str:
    if values is None:
        values = []
    return json.dumps(list(values), ensure_ascii=False)


def _dedupe_ints(values: Iterable | None) -> list[int]:
    result = []
    for value in values or []:
        try:
            ivalue = int(value)
        except (TypeError, ValueError):
            continue
        if ivalue not in result:
            result.append(ivalue)
    return result


def _join_sentences(parts: Iterable[str]) -> str:
    cleaned = []
    for part in parts:
        text = str(part or "").strip()
        if not text:
            continue
        if text[-1] not in "。；;":
            text += "。"
        cleaned.append(text)
    return "".join(cleaned)


def _clip(text: str | None, limit: int = 600) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _canonical_hospital_name(db: Session, hospital_name: str | None, group_id: int | None) -> str:
    if group_id:
        group = db.query(MaterialGroup).filter(MaterialGroup.id == group_id).first()
        if group and group.group_name:
            return group.group_name
    return hospital_name or ""


def _materials_for_source(db: Session, material_id: int | None, group_id: int | None) -> list[Material]:
    if group_id:
        return db.query(Material).filter(Material.group_id == group_id).order_by(
            Material.page_number.is_(None),
            Material.page_number,
            Material.id,
        ).all()
    if material_id:
        material = db.query(Material).filter(Material.id == material_id).first()
        return [material] if material else []
    return []


def _source_payload(
    db: Session,
    material_id: int | None = None,
    group_id: int | None = None,
    source_material_ids=None,
    source_page_numbers=None,
) -> tuple[list[int], list[int]]:
    material_ids = _dedupe_ints(_loads_list(source_material_ids))
    if not material_ids:
        material_ids = [m.id for m in _materials_for_source(db, material_id, group_id)]
    page_numbers = _dedupe_ints(_loads_list(source_page_numbers))
    if not page_numbers and material_ids:
        materials = db.query(Material).filter(Material.id.in_(material_ids)).all()
        by_id = {m.id: m for m in materials}
        page_numbers = [
            by_id[mid].page_number
            for mid in material_ids
            if mid in by_id and by_id[mid].page_number is not None
        ]
    return material_ids, page_numbers


def _importance_from_text(fact_type: str, fact_role: str | None, text: str) -> str:
    if fact_role in CORE_EVENT_TYPES:
        return FactImportance.CORE
    if any(keyword in (text or "") for keyword in CORE_KEYWORDS):
        return FactImportance.CORE
    return FactImportance.SUPPORTING


def _body_keywords(text: str | None) -> set[str]:
    text = str(text or "")
    found = {keyword for keyword in BODY_PART_KEYWORDS if keyword in text}
    if "胫骨" in found or "腓骨" in found:
        found.add("胫腓骨")
    if "耻骨" in found or "坐骨" in found or "骶骨" in found or "髂骨" in found:
        found.add("骨盆")
    if "小腿" in found:
        found.add("下肢")
    return found


def _fact_payload_text(payload: dict) -> str:
    return " ".join(
        str(payload.get(key) or "")
        for key in ("title", "summary", "source_quote", "hospital_name")
    )


def _payload_pages(payload: dict) -> list:
    return _loads_list(payload.get("source_page_numbers"))


def _payload_quality_flags(payload: dict) -> list[str]:
    return [str(flag or "") for flag in _loads_list(payload.get("quality_flags"))]


def _has_positive_finding(text: str | None) -> bool:
    text = str(text or "")
    return any(keyword in text for keyword in POSITIVE_FINDING_KEYWORDS)


def _is_weak_or_generic_payload(payload: dict) -> bool:
    text = _fact_payload_text(payload)
    flags = _payload_quality_flags(payload)
    if "lab_report" in flags or "generic_exam_part" in flags:
        return True
    if not _has_positive_finding(text) and any(keyword in text for keyword in WEAK_ONLY_KEYWORDS):
        return True
    return False


def _system_verification_note(parts: set[str]) -> str:
    if parts:
        ordered = [part for part in BODY_PART_KEYWORDS if part in parts]
        body_text = "、".join(ordered[:4])
        if body_text:
            return f"系统预采信：病历诊断/手术记录与检查事实在{body_text}相关损伤上相互印证。"
    return SYSTEM_VERIFIED_NOTE


def _is_lab_report_text(text: str | None) -> bool:
    text = str(text or "")
    return any(keyword in text for keyword in LAB_REPORT_KEYWORDS)


def _ordered_body_parts(text: str | None) -> list[str]:
    found = _body_keywords(text)
    ordered = []
    for keyword in BODY_PART_KEYWORDS:
        if keyword in found and keyword not in ordered:
            ordered.append(keyword)
    # 归并同义部位，避免标题写成“胫骨、腓骨、胫腓骨”
    if "胫腓骨" in ordered:
        ordered = [item for item in ordered if item not in {"胫骨", "腓骨"}]
    if "骨盆" in ordered:
        ordered = [item for item in ordered if item not in {"髂骨", "骶骨", "耻骨", "坐骨"}]
    if "下肢" in ordered:
        ordered = [item for item in ordered if item != "小腿"]
    return ordered


def _infer_exam_part(report: ImagingReport) -> str:
    current = str(report.exam_part or "").strip()
    if current:
        return current
    text = " ".join([
        report.exam_type or "",
        report.film_number or "",
        report.report_content or "",
    ])
    parts = _ordered_body_parts(text)
    if parts:
        return "、".join(parts[:4])
    if _is_lab_report_text(text):
        return "检验报告"
    if any(keyword in text for keyword in IMAGING_TYPE_KEYWORDS):
        return "检查报告"
    return "检查报告"


def _normalized_exam_type(report: ImagingReport, inferred_part: str) -> str:
    exam_type = str(report.exam_type or "").strip()
    text = " ".join([exam_type, report.report_content or ""])
    if inferred_part == "检验报告":
        return "检验"
    if exam_type and exam_type not in {"检查报告", "影像学检查"}:
        return exam_type
    for keyword in IMAGING_TYPE_KEYWORDS:
        if keyword in text:
            if keyword in {"磁共振"}:
                return "MRI"
            if keyword in {"彩超", "B超"}:
                return "超声"
            return keyword
    return exam_type or "检查报告"


def _imaging_title(report: ImagingReport) -> tuple[str, str, str]:
    inferred_part = _infer_exam_part(report)
    exam_type = _normalized_exam_type(report, inferred_part)
    if not report.exam_part and inferred_part:
        report.exam_part = inferred_part
    if report.exam_type in {None, "", "检查报告", "影像学检查"} and exam_type:
        report.exam_type = exam_type
    if inferred_part == "检验报告":
        return "检验报告", inferred_part, exam_type
    if inferred_part == "检查报告":
        return exam_type if exam_type != "检查报告" else "检查报告", inferred_part, exam_type
    title = f"{inferred_part}{exam_type if exam_type not in {'检查报告', '检验'} else ''}".strip()
    return title or "检查报告", inferred_part, exam_type


def _imaging_quality_flags(report: ImagingReport, inferred_part: str, summary: str) -> str:
    flags = _loads_list(report.quality_flags)
    if inferred_part and not (report.exam_part or "").strip():
        flags.append("exam_part_inferred")
    if inferred_part in {"检查报告", "检验报告"}:
        flags.append("generic_exam_part")
    if _is_lab_report_text(summary):
        flags.append("lab_report")
    deduped = []
    for flag in flags:
        if flag and flag not in deduped:
            deduped.append(flag)
    return _dumps_list(deduped)


def _imaging_importance(report: ImagingReport, title: str, summary: str, inferred_part: str) -> str:
    existing = report.review_status
    if existing == FactReviewStatus.EXCLUDED:
        return FactImportance.EXCLUDED
    text = " ".join([title, summary, report.report_content or ""])
    if any(keyword in text for keyword in CORE_KEYWORDS):
        return FactImportance.CORE
    if inferred_part == "检验报告":
        return FactImportance.SUPPORTING
    if inferred_part == "检查报告":
        return FactImportance.SUPPORTING
    return FactImportance.SUPPORTING


def _fact_day(value: str | None) -> str:
    _precision, sort_time = _parse_fact_datetime(value)
    return sort_time[:10] if re.match(r"\d{4}-\d{2}-\d{2}", sort_time) else ""


def _related_imaging_report_text(db: Session, event: MedicalEvent, event_text: str) -> str:
    keywords = _body_keywords(event_text)
    if not keywords:
        return ""
    event_day = _fact_day(event.event_date)
    reports = db.query(ImagingReport).filter(ImagingReport.case_id == event.case_id).all()
    matches = []
    for report in reports:
        report_text = " ".join([
            report.exam_part or "",
            report.exam_type or "",
            report.report_content or "",
        ])
        overlap = keywords & _body_keywords(report_text)
        if not overlap:
            continue
        if event.hospital_name and report.hospital_name and event.hospital_name != report.hospital_name:
            continue
        report_day = _fact_day(report.report_datetime or report.report_date)
        same_day_bonus = 0 if event_day and report_day and event_day == report_day else 1
        pages = _dedupe_ints(_loads_list(report.source_page_numbers))
        if not pages and report.material_id:
            material = db.query(Material).filter(Material.id == report.material_id).first()
            pages = [material.page_number] if material and material.page_number is not None else []
        title = "".join([report.exam_part or "", report.exam_type or "检查报告"]).strip() or "检查报告"
        matches.append((same_day_bonus, report_day or "9999-99-99", title, pages))
    if not matches:
        return ""
    matches.sort(key=lambda item: (item[0], item[1], item[2]))
    parts = []
    for _bonus, day, title, pages in matches[:3]:
        page_text = f"（来源页{', '.join(str(page) for page in pages)}）" if pages else ""
        date_text = "" if day == "9999-99-99" else day
        parts.append(f"{date_text}{title}{page_text}".strip())
    return "；".join(parts)


def _upsert_fact(db: Session, case_id: int, data: dict) -> UnifiedFact:
    fact = db.query(UnifiedFact).filter(
        UnifiedFact.case_id == case_id,
        UnifiedFact.fact_key == data["fact_key"],
    ).first()
    if not fact:
        fact = UnifiedFact(case_id=case_id, fact_key=data["fact_key"])
        db.add(fact)
        preserve_review = False
    else:
        preserve_review = (
            fact.review_status in MANUAL_REVIEW_STATUSES
            or (fact.review_status == FactReviewStatus.PENDING and bool(fact.review_note))
        )

    preserved = {
        "review_status": fact.review_status,
        "importance": fact.importance,
        "review_note": fact.review_note,
    }

    for key, value in data.items():
        if key in {"review_status", "importance", "review_note"} and preserve_review:
            continue
        setattr(fact, key, value)

    if preserve_review:
        fact.review_status = preserved["review_status"]
        fact.importance = preserved["importance"]
        fact.review_note = preserved["review_note"]
    return fact


def _apply_system_verification(payloads: list[dict]) -> None:
    """Mark high-confidence, cross-supported facts as system verified.

    This reduces doctor workload without treating machine judgment as manual
    confirmation. Manual statuses are preserved by _upsert_fact.
    """
    usable = [
        payload
        for payload in payloads
        if payload
        and payload.get("importance") != FactImportance.EXCLUDED
        and payload.get("review_status") not in MANUAL_REVIEW_STATUSES
        and _payload_pages(payload)
    ]
    medical_payloads = [p for p in usable if p.get("fact_type") in {"hospital_record", "medical_event"}]
    imaging_payloads = [p for p in usable if p.get("fact_type") == "imaging_report"]

    medical_terms: set[str] = set()
    for payload in medical_payloads:
        text = _fact_payload_text(payload)
        if _has_positive_finding(text):
            medical_terms.update(_body_keywords(text))

    imaging_terms: set[str] = set()
    for payload in imaging_payloads:
        text = _fact_payload_text(payload)
        if not _is_weak_or_generic_payload(payload) and _has_positive_finding(text):
            imaging_terms.update(_body_keywords(text))

    for payload in usable:
        text = _fact_payload_text(payload)
        if payload.get("review_status") in MANUAL_REVIEW_STATUSES:
            continue
        if not _has_positive_finding(text):
            continue
        parts = _body_keywords(text)
        if not parts:
            continue

        should_verify = False
        if payload.get("fact_type") == "imaging_report":
            should_verify = (
                payload.get("importance") == FactImportance.CORE
                and not _is_weak_or_generic_payload(payload)
                and bool(parts & medical_terms)
            )
        elif payload.get("fact_type") == "hospital_record":
            should_verify = payload.get("importance") == FactImportance.CORE and bool(parts & imaging_terms)
        elif payload.get("fact_type") == "medical_event":
            should_verify = (
                payload.get("fact_role") in SYSTEM_VERIFY_EVENT_ROLES
                and payload.get("importance") == FactImportance.CORE
                and bool(parts & imaging_terms)
            )

        if should_verify:
            payload["review_status"] = FactReviewStatus.SYSTEM_VERIFIED
            payload["review_note"] = _system_verification_note(parts)


def _hospital_record_fact(db: Session, record: HospitalRecord) -> dict:
    material_ids, page_numbers = _source_payload(db, record.material_id, record.group_id)
    hospital = _canonical_hospital_name(db, record.hospital_name, record.group_id)
    summary = _join_sentences([
        f"{record.admission_date}入院" if record.admission_date else "",
        f"{record.discharge_date}出院，住院{record.hospital_days}天" if record.discharge_date and record.hospital_days else (
            f"{record.discharge_date}出院" if record.discharge_date else ""
        ),
        f"主诉：{record.chief_complaint}" if record.chief_complaint else "",
        f"现病史：{record.present_illness_history}" if record.present_illness_history else "",
        f"体格检查：{record.physical_examination}" if record.physical_examination else "",
        f"入院诊断：{record.admission_diagnosis}" if record.admission_diagnosis else "",
        f"治疗过程：{record.treatment_process}" if record.treatment_process else "",
        f"出院诊断：{record.discharge_diagnosis}" if record.discharge_diagnosis else "",
        f"出院医嘱：{record.discharge_orders}" if record.discharge_orders else "",
    ])
    return {
        "fact_key": f"hospital_record:{record.id}",
        "fact_type": "hospital_record",
        "fact_role": "admission",
        "fact_date": record.admission_date or record.discharge_date or "",
        "hospital_name": hospital,
        "title": f"{hospital or '医院'}入院及住院病历",
        "summary": _clip(summary, 1600),
        "source_quote": "",
        "source_kind": "hospital_record",
        "source_id": record.id,
        "source_material_ids": _dumps_list(material_ids),
        "source_page_numbers": _dumps_list(page_numbers),
        "review_status": record.review_status or FactReviewStatus.PENDING,
        "importance": _importance_from_text("hospital_record", "admission", summary),
        "extraction_confidence": record.extraction_confidence,
        "quality_flags": record.quality_flags or "[]",
    }


def _medical_event_fact(db: Session, event: MedicalEvent) -> dict:
    material_ids, page_numbers = _source_payload(
        db,
        None,
        event.group_id,
        event.source_material_ids,
        event.source_page_numbers,
    )
    hospital = _canonical_hospital_name(db, event.hospital_name, event.group_id)
    role = event.event_type or "other"
    label = EVENT_TYPE_LABELS.get(role, role)
    summary = _join_sentences([
        event.summary or "",
        f"诊断：{event.diagnosis}" if event.diagnosis else "",
        f"发现：{event.findings}" if event.findings else "",
        f"处理：{event.treatment}" if event.treatment else "",
    ])
    related_imaging = _related_imaging_report_text(
        db,
        event,
        " ".join([
            event.title or "",
            summary or "",
            event.source_quote or "",
        ]),
    )
    if related_imaging:
        summary = _join_sentences([summary, f"相关检查：{related_imaging}"])
    return {
        "fact_key": f"medical_event:{event.id}",
        "fact_type": "medical_event",
        "fact_role": role,
        "fact_date": event.event_date or "",
        "hospital_name": hospital,
        "title": event.title or label,
        "summary": _clip(summary, 1200),
        "source_quote": _clip(event.source_quote, 600),
        "source_kind": "medical_event",
        "source_id": event.id,
        "source_material_ids": _dumps_list(material_ids),
        "source_page_numbers": _dumps_list(page_numbers),
        "review_status": event.review_status or FactReviewStatus.PENDING,
        "importance": _importance_from_text("medical_event", role, " ".join([event.title or "", summary])),
        "extraction_confidence": event.extraction_confidence,
        "quality_flags": event.quality_flags or "[]",
    }


def _imaging_report_fact(db: Session, report: ImagingReport) -> dict:
    material_ids, page_numbers = _source_payload(
        db,
        report.material_id,
        report.group_id if report.source_material_ids else None,
        report.source_material_ids,
        report.source_page_numbers,
    )
    hospital = _canonical_hospital_name(db, report.hospital_name, report.group_id)
    title, inferred_part, exam_type = _imaging_title(report)
    summary = _join_sentences([
        f"检查号：{report.film_number}" if report.film_number else "",
        report.report_content or "",
    ])
    quality_flags = _imaging_quality_flags(report, inferred_part, summary)
    report.quality_flags = quality_flags
    return {
        "fact_key": f"imaging_report:{report.id}",
        "fact_type": "imaging_report",
        "fact_role": "imaging",
        "fact_date": report.report_datetime or report.report_date or "",
        "hospital_name": hospital,
        "title": title,
        "summary": _clip(summary, 1200),
        "source_quote": _clip(report.report_content, 600),
        "source_kind": "imaging_report",
        "source_id": report.id,
        "source_material_ids": _dumps_list(material_ids),
        "source_page_numbers": _dumps_list(page_numbers),
        "review_status": report.review_status or FactReviewStatus.PENDING,
        "importance": _imaging_importance(report, title, summary, inferred_part),
        "extraction_confidence": report.extraction_confidence,
        "quality_flags": quality_flags,
    }


def _parse_fact_datetime(value: str | None) -> tuple[int, str]:
    """Return a minute-level sortable key. Unknown dates sort last."""
    raw = str(value or "").strip()
    if not raw:
        return 9, "9999-99-99 99:99"

    text = raw.replace("T", " ").replace("：", ":")
    patterns = [
        r"(?P<y>\d{4})[年\-/\.](?P<m>\d{1,2})[月\-/\.](?P<d>\d{1,2})(?:日)?\s*(?P<h>\d{1,2})[:时](?P<mi>\d{1,2})(?:[:分]\d{1,2})?",
        r"(?P<y>\d{4})[年\-/\.](?P<m>\d{1,2})[月\-/\.](?P<d>\d{1,2})(?:日)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        parts = match.groupdict()
        try:
            year = int(parts["y"])
            month = int(parts["m"])
            day = int(parts["d"])
            has_minute = bool(parts.get("h") and parts.get("mi"))
            hour = int(parts.get("h") or 23)
            minute = int(parts.get("mi") or 59)
            parsed = datetime(year, month, day, hour, minute)
        except (TypeError, ValueError):
            continue
        precision = 0 if has_minute else 1
        return precision, parsed.strftime("%Y-%m-%d %H:%M")

    normalized_digits = re.sub(r"\D+", "", text)
    if len(normalized_digits) >= 8:
        try:
            parsed = datetime(
                int(normalized_digits[:4]),
                int(normalized_digits[4:6]),
                int(normalized_digits[6:8]),
                int(normalized_digits[8:10] or 23) if len(normalized_digits) >= 10 else 23,
                int(normalized_digits[10:12] or 59) if len(normalized_digits) >= 12 else 59,
            )
            precision = 0 if len(normalized_digits) >= 12 else 1
            return precision, parsed.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass

    return 8, raw


def _source_page_sort_value(fact: UnifiedFact) -> int:
    pages = _dedupe_ints(_loads_list(fact.source_page_numbers))
    return min(pages) if pages else 999999


def fact_sort_key(fact: UnifiedFact) -> tuple:
    precision, sort_time = _parse_fact_datetime(fact.fact_date)
    return (
        sort_time,
        precision,
        FACT_TYPE_ORDER.get(fact.fact_type or "", 999),
        EVENT_TYPE_ORDER.get(fact.fact_role or "", 999),
        _source_page_sort_value(fact),
        fact.id or 0,
    )


def unified_fact_to_dict(fact: UnifiedFact) -> dict:
    return {
        "id": fact.id,
        "case_id": fact.case_id,
        "fact_key": fact.fact_key,
        "fact_type": fact.fact_type,
        "fact_role": fact.fact_role,
        "fact_date": fact.fact_date,
        "hospital_name": fact.hospital_name,
        "title": fact.title,
        "summary": fact.summary,
        "source_quote": fact.source_quote,
        "source_kind": fact.source_kind,
        "source_id": fact.source_id,
        "source_material_ids": _loads_list(fact.source_material_ids),
        "source_page_numbers": _loads_list(fact.source_page_numbers),
        "review_status": fact.review_status,
        "importance": fact.importance,
        "extraction_confidence": fact.extraction_confidence,
        "quality_flags": _loads_list(fact.quality_flags),
        "review_note": fact.review_note,
        "created_at": fact.created_at.isoformat() if fact.created_at else None,
        "updated_at": fact.updated_at.isoformat() if fact.updated_at else None,
    }


def list_unified_facts(case_id: int, db: Session) -> list[UnifiedFact]:
    facts = db.query(UnifiedFact).filter(UnifiedFact.case_id == case_id).all()
    return sorted(facts, key=fact_sort_key)


def sync_unified_facts(case_id: int, db: Session) -> dict:
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        return {"success": False, "error": "案件不存在", "facts": []}

    db.query(UnifiedFact).filter(
        UnifiedFact.case_id == case_id,
        UnifiedFact.fact_type.in_(["case_basic", "case_facts", "clinical_exam"]),
    ).delete(synchronize_session=False)
    managed_types = ["hospital_record", "medical_event", "imaging_report"]
    payloads = []
    payloads.extend(
        _hospital_record_fact(db, item)
        for item in db.query(HospitalRecord).filter(HospitalRecord.case_id == case_id).all()
    )
    payloads.extend(
        _medical_event_fact(db, item)
        for item in db.query(MedicalEvent).filter(MedicalEvent.case_id == case_id).all()
    )
    payloads.extend(
        _imaging_report_fact(db, item)
        for item in db.query(ImagingReport).filter(ImagingReport.case_id == case_id).all()
    )
    _apply_system_verification(payloads)

    payload_keys = {payload["fact_key"] for payload in payloads if payload and payload.get("fact_key")}
    stale_query = db.query(UnifiedFact).filter(
        UnifiedFact.case_id == case_id,
        UnifiedFact.fact_type.in_(managed_types),
    )
    if payload_keys:
        stale_query = stale_query.filter(~UnifiedFact.fact_key.in_(payload_keys))
    stale_query.delete(synchronize_session=False)

    synced = []
    for payload in payloads:
        if not payload or not (payload.get("summary") or payload.get("source_quote")):
            continue
        synced.append(_upsert_fact(db, case_id, payload))

    db.commit()
    facts = list_unified_facts(case_id, db)
    return {
        "success": True,
        "synced_count": len(synced),
        "facts": [unified_fact_to_dict(fact) for fact in facts],
        "counts": fact_counts(facts),
    }


def fact_counts(facts: list[UnifiedFact]) -> dict:
    counts = {
        "total": len(facts),
        "confirmed": 0,
        "system_verified": 0,
        "pending": 0,
        "needs_edit": 0,
        "excluded": 0,
        "core": 0,
        "supporting": 0,
    }
    for fact in facts:
        status = fact.review_status or FactReviewStatus.PENDING
        if status in counts:
            counts[status] += 1
        if fact.importance == FactImportance.CORE:
            counts["core"] += 1
        elif fact.importance == FactImportance.SUPPORTING:
            counts["supporting"] += 1
    return counts


def accepted_facts_for_generation(case_id: int, db: Session, fact_types: set[str] | None = None) -> list[UnifiedFact]:
    facts = list_unified_facts(case_id, db)
    if fact_types:
        facts = [fact for fact in facts if fact.fact_type in fact_types]
    facts = [
        fact for fact in facts
        if fact.review_status != FactReviewStatus.EXCLUDED
        and fact.importance != FactImportance.EXCLUDED
    ]
    trusted = [
        fact
        for fact in facts
        if fact.review_status in (FactReviewStatus.CONFIRMED, FactReviewStatus.SYSTEM_VERIFIED)
    ]
    return trusted or facts


def update_unified_fact(db: Session, fact_id: int, data: dict) -> UnifiedFact | None:
    fact = db.query(UnifiedFact).filter(UnifiedFact.id == fact_id).first()
    if not fact:
        return None
    allowed = {"review_status", "importance", "review_note", "summary", "title", "hospital_name", "fact_date"}
    for key, value in (data or {}).items():
        if key not in allowed:
            continue
        if key == "review_status" and value not in FactReviewStatus.ALL:
            continue
        if key == "importance" and value not in FactImportance.ALL:
            continue
        setattr(fact, key, value)
    if fact.review_status == FactReviewStatus.EXCLUDED:
        fact.importance = FactImportance.EXCLUDED
    db.commit()
    db.refresh(fact)
    return fact


def format_facts_for_generation(facts: list[UnifiedFact], max_chars: int = 8000) -> str:
    lines = []
    for fact in sorted(facts, key=fact_sort_key):
        parts = [
            fact.fact_date or "日期未明",
            fact.hospital_name or "",
            fact.title or "",
            fact.summary or "",
        ]
        if fact.source_quote:
            parts.append(f"依据：{fact.source_quote}")
        pages = _loads_list(fact.source_page_numbers)
        if pages:
            parts.append(f"来源页：{', '.join(str(p) for p in pages)}")
        line = " | ".join(str(part).strip() for part in parts if str(part or "").strip())
        lines.append(line)
        if sum(len(item) for item in lines) > max_chars:
            break
    return "\n".join(lines)[:max_chars]
