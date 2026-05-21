"""病历事件事实抽取 harness。

目标不是替代医生判断，而是把病历页拆成有来源页、有时间、有类型的候选事实。
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy.orm import Session

from app.models.case import (
    Case,
    FactReviewStatus,
    HospitalRecord,
    Material,
    MaterialGroup,
    MedicalEvent,
)


HARNESS_FLAG = "medical_event_harness_v1"

STOP_LABELS = [
    "姓名", "性别", "年龄", "科室", "科室名称", "住院科室", "病床号", "床位号", "住院号",
    "入院日期", "入院时间", "出院日期", "出院时间", "住院天数", "实际住院",
    "主诉", "现病史", "入院情况", "既往史", "体格检查", "专科检查", "辅助检查",
    "入院诊断", "初步诊断", "修正诊断", "术前诊断", "术中诊断", "术后诊断",
    "手术日期", "手术时间", "手术名称", "麻醉方式", "术中所见", "手术经过", "手术过程",
    "诊疗经过", "治疗经过", "出院诊断", "出院情况", "出院医嘱", "医师签字",
    "手术者签名", "医生签字", "记录时间",
]

RELEVANCE_KEYWORDS = (
    "车祸", "外伤", "交通事故", "骨折", "损伤", "破裂", "出血", "血肿", "挫伤",
    "胫骨", "腓骨", "胫腓骨", "骨盆", "骶骨", "耻骨", "坐骨", "股骨", "髋",
    "肋骨", "锁骨", "颅脑", "脾", "腹", "胸", "手术", "内固定", "外固定", "复位",
    "切除", "修补", "置换", "陪护", "功能锻炼", "复查",
)

LOW_VALUE_SUBTYPES = {
    "consultation_record",
    "medical_order_nursing",
    "lab_report",
    "other_medical_record",
}


@dataclass
class EventProposal:
    event_type: str
    title: str
    materials: list[Material]
    event_date: str | None = None
    summary: str | None = None
    diagnosis: str | None = None
    findings: str | None = None
    treatment: str | None = None
    source_quote: str | None = None
    material_subtype: str | None = None
    confidence: int = 70
    flags: list[str] = field(default_factory=list)


def _clean_ocr(text: str | None) -> str:
    text = html.unescape(str(text or ""))
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("|", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _clip(text: str | None, limit: int = 700) -> str | None:
    text = re.sub(r"\s+", " ", str(text or "")).strip(" ：:;；。")
    if not text:
        return None
    return text[:limit].strip()


def _extract_labeled_text(text: str, labels: Iterable[str], limit: int = 700) -> str | None:
    if not text:
        return None
    for label in labels:
        stop_re = "|".join(re.escape(item) for item in STOP_LABELS if item != label)
        pattern = rf"{re.escape(label)}\s*[：:]?\s*(.+?)(?=(?:{stop_re})\s*[：:]?\s*|$)"
        match = re.search(pattern, text)
        if match:
            value = _clip(match.group(1), limit)
            if value:
                return value
    return None


def _format_datetime(match: re.Match) -> str | None:
    try:
        y = int(match.group("y"))
        m = int(match.group("m"))
        d = int(match.group("d"))
        h = match.groupdict().get("h")
        mi = match.groupdict().get("mi")
        if h:
            return f"{y:04d}-{m:02d}-{d:02d} {int(h):02d}:{int(mi or 0):02d}"
        return f"{y:04d}-{m:02d}-{d:02d}"
    except (TypeError, ValueError):
        return None


def _extract_datetime(texts: Iterable[str], labels: Iterable[str] | None = None) -> str | None:
    label_prefix = ""
    if labels:
        label_re = "|".join(re.escape(label) for label in labels)
        label_prefix = rf"(?:{label_re})\s*[：:]?\s*"
    patterns = [
        (0, label_prefix + r"(?P<y>\d{4})\s*[年\-/\.]\s*(?P<m>\d{1,2})\s*[月\-/\.]\s*(?P<d>\d{1,2})\s*(?:日)?\s*(?P<h>\d{1,2})\s*[:时]\s*(?P<mi>\d{1,2})"),
        (1, label_prefix + r"(?P<y>\d{4})\s*[年\-/\.]\s*(?P<m>\d{1,2})\s*[月\-/\.]\s*(?P<d>\d{1,2})\s*(?:日)?\s*(?P<h>\d{1,2})\s*时"),
        (2, label_prefix + r"(?P<y>\d{4})\s*[年\-/\.]\s*(?P<m>\d{1,2})\s*[月\-/\.]\s*(?P<d>\d{1,2})\s*(?:日)?"),
    ]
    candidates: list[tuple[int, int, str]] = []
    for text_index, text in enumerate(texts):
        for precision, pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            value = _format_datetime(match)
            if value:
                candidates.append((precision, text_index, value))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _json_ids(values: Iterable[int]) -> str:
    return json.dumps(list(dict.fromkeys(int(value) for value in values)), ensure_ascii=False)


def _source_pages(materials: Iterable[Material]) -> list[int]:
    pages = []
    for material in materials:
        if material.page_number is not None and material.page_number not in pages:
            pages.append(material.page_number)
    return pages


def _source_ids(materials: Iterable[Material]) -> list[int]:
    ids = []
    for material in materials:
        if material.id not in ids:
            ids.append(material.id)
    return ids


def _event_identity(
    event_type: str,
    event_date: str | None,
    title: str | None,
    source_ids: Iterable[int],
) -> tuple:
    return (
        event_type or "",
        event_date or "",
        re.sub(r"\s+", "", title or "")[:80],
        tuple(source_ids),
    )


def _identity_from_event(event: MedicalEvent) -> tuple:
    try:
        source_ids = json.loads(event.source_material_ids or "[]")
    except (TypeError, json.JSONDecodeError):
        source_ids = []
    return _event_identity(event.event_type, event.event_date, event.title, source_ids)


def _is_auto_event(event: MedicalEvent) -> bool:
    return HARNESS_FLAG in str(event.quality_flags or "")


def _is_signature_or_empty(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 120:
        return True
    stripped = re.sub(r"(姓名|住院号|科室|床位号|医师签字|医生签字|签名|日期|手术记录|页码|第\d+页)", "", compact)
    return len(stripped) < 80 and ("签字" in compact or "签名" in compact)


def _is_relevant(text: str | None) -> bool:
    return any(keyword in str(text or "") for keyword in RELEVANCE_KEYWORDS)


def _merged_text(materials: Iterable[Material]) -> str:
    return "\n".join(_clean_ocr(material.ocr_text) for material in materials if material.ocr_text)


def _proposal_flags(proposal: EventProposal) -> list[str]:
    text = " ".join([
        proposal.title or "",
        proposal.summary or "",
        proposal.diagnosis or "",
        proposal.findings or "",
        proposal.treatment or "",
    ])
    relevance = "relevance:high" if _is_relevant(text) else "relevance:supporting"
    flags = [HARNESS_FLAG, "source_bound", relevance]
    flags.extend(proposal.flags)
    return list(dict.fromkeys(flags))


def _proposal_has_content(proposal: EventProposal) -> bool:
    return any([
        proposal.summary,
        proposal.diagnosis,
        proposal.findings,
        proposal.treatment,
        proposal.source_quote,
    ])


def _build_admission_event(record: HospitalRecord | None, materials: list[Material]) -> EventProposal | None:
    text = _merged_text(materials)
    if not text and not record:
        return None
    event_date = (record.admission_date if record else None) or _extract_datetime([text], ["入院日期", "入院时间"])
    chief = record.chief_complaint if record else _extract_labeled_text(text, ["主诉"], 260)
    history = record.present_illness_history if record else _extract_labeled_text(text, ["现病史", "入院情况"], 650)
    diagnosis = record.admission_diagnosis if record else _extract_labeled_text(text, ["入院诊断", "初步诊断"], 650)
    findings = record.physical_examination if record else _extract_labeled_text(text, ["体格检查", "专科检查"], 650)
    summary = "；".join(item for item in [f"主诉：{chief}" if chief else "", history or ""] if item)
    proposal = EventProposal(
        event_type="admission",
        title="入院记录",
        materials=materials,
        event_date=event_date,
        summary=_clip(summary, 800),
        diagnosis=diagnosis,
        findings=findings,
        source_quote=_clip(_extract_labeled_text(text, ["主诉", "现病史", "入院情况"], 500), 500),
        material_subtype="admission_record",
        confidence=82,
    )
    return proposal if _proposal_has_content(proposal) else None


def _build_discharge_event(record: HospitalRecord | None, materials: list[Material]) -> EventProposal | None:
    text = _merged_text(materials)
    if not text and not record:
        return None
    event_date = (record.discharge_date if record else None) or _extract_datetime([text], ["出院日期", "出院时间"])
    diagnosis = record.discharge_diagnosis if record else _extract_labeled_text(text, ["出院诊断"], 650)
    treatment = record.treatment_process if record else _extract_labeled_text(text, ["诊疗经过", "治疗经过"], 700)
    orders = record.discharge_orders if record else _extract_labeled_text(text, ["出院医嘱"], 700)
    summary_parts = []
    if record and record.hospital_days:
        summary_parts.append(f"住院{record.hospital_days}天")
    discharge_state = _extract_labeled_text(text, ["出院情况"], 500)
    if discharge_state:
        summary_parts.append(f"出院情况：{discharge_state}")
    if orders:
        summary_parts.append(f"出院医嘱：{orders}")
    proposal = EventProposal(
        event_type="discharge",
        title="出院记录",
        materials=materials,
        event_date=event_date,
        summary=_clip("；".join(summary_parts), 900),
        diagnosis=diagnosis,
        treatment=treatment,
        source_quote=_clip(_extract_labeled_text(text, ["出院诊断", "出院医嘱"], 500), 500),
        material_subtype="discharge_record",
        confidence=82,
    )
    return proposal if _proposal_has_content(proposal) else None


def _operation_signature_should_merge(material: Material, text: str, previous: EventProposal | None) -> bool:
    if not previous:
        return False
    if material.page_number is None or not previous.materials:
        return False
    last_page = previous.materials[-1].page_number
    return last_page is not None and material.page_number - last_page <= 1 and _is_signature_or_empty(text)


def _build_operation_events(materials: list[Material]) -> list[EventProposal]:
    events: list[EventProposal] = []
    for material in materials:
        text = _clean_ocr(material.ocr_text)
        if not text:
            continue
        if _operation_signature_should_merge(material, text, events[-1] if events else None):
            events[-1].materials.append(material)
            events[-1].flags.append("signature_page_merged")
            continue

        event_date = _extract_datetime([text], ["手术开始", "手术开始时间", "手术时间", "手术日期"])
        surgery_name = _extract_labeled_text(text, ["手术名称", "手术及操作名称", "拟实施手术"], 240)
        diagnosis = _extract_labeled_text(text, ["术前诊断", "术中诊断", "术后诊断"], 650)
        findings = _extract_labeled_text(text, ["术中所见"], 650)
        treatment = _extract_labeled_text(text, ["手术经过、术中发现的情况及处理", "手术经过", "手术过程"], 900)
        if not any([surgery_name, diagnosis, findings, treatment]) and _is_signature_or_empty(text):
            continue

        title = surgery_name or "手术记录"
        if events:
            previous = events[-1]
            same_title = re.sub(r"\s+", "", previous.title or "") == re.sub(r"\s+", "", title or "")
            same_date = (previous.event_date or "") == (event_date or "")
            last_page = previous.materials[-1].page_number
            adjacent = last_page is not None and material.page_number is not None and material.page_number - last_page <= 1
            if adjacent and (same_title or same_date):
                previous.materials.append(material)
                previous.diagnosis = previous.diagnosis or diagnosis
                previous.findings = previous.findings or findings
                previous.treatment = previous.treatment or treatment
                previous.source_quote = previous.source_quote or _clip(treatment or findings or diagnosis, 500)
                continue

        proposal = EventProposal(
            event_type="surgery",
            title=title,
            materials=[material],
            event_date=event_date,
            summary=surgery_name,
            diagnosis=diagnosis,
            findings=findings,
            treatment=treatment,
            source_quote=_clip(treatment or findings or diagnosis or surgery_name, 500),
            material_subtype="operation_record",
            confidence=84 if event_date and surgery_name else 68,
        )
        if _proposal_has_content(proposal):
            events.append(proposal)
    return events


def _build_progress_events(materials: list[Material]) -> list[EventProposal]:
    events: list[EventProposal] = []
    for material in materials:
        text = _clean_ocr(material.ocr_text)
        if not text or not _is_relevant(text) or _is_signature_or_empty(text):
            continue
        title = "病程记录"
        if "首次病程记录" in text:
            title = "首次病程记录"
        elif "术后" in text:
            title = "术后病程记录"
        summary = _extract_labeled_text(text, ["病例特点", "病情分析", "诊疗计划", "病程记录"], 700)
        if not summary:
            summary = _clip(text, 450)
        proposal = EventProposal(
            event_type="progress",
            title=title,
            materials=[material],
            event_date=_extract_datetime([text]),
            summary=summary,
            source_quote=_clip(summary, 450),
            material_subtype="progress_note",
            confidence=58,
            flags=["progress_supporting"],
        )
        if _proposal_has_content(proposal):
            events.append(proposal)
    return events


def _propose_events(record: HospitalRecord | None, materials: list[Material]) -> list[EventProposal]:
    by_subtype: dict[str, list[Material]] = {}
    for material in materials:
        by_subtype.setdefault(material.material_subtype or "", []).append(material)

    proposals: list[EventProposal] = []

    admission_materials = by_subtype.get("admission_record") or by_subtype.get("medical_home_page") or []
    admission = _build_admission_event(record, admission_materials)
    if admission:
        proposals.append(admission)

    proposals.extend(_build_operation_events(by_subtype.get("operation_record") or []))

    discharge_materials = by_subtype.get("discharge_record") or []
    discharge = _build_discharge_event(record, discharge_materials)
    if discharge:
        proposals.append(discharge)

    proposals.extend(_build_progress_events(by_subtype.get("progress_note") or []))

    return [
        proposal
        for proposal in proposals
        if proposal.materials
        and (proposal.material_subtype not in LOW_VALUE_SUBTYPES)
        and _proposal_has_content(proposal)
    ]


def rebuild_medical_events_for_group(
    case: Case,
    group: MaterialGroup,
    hospital_record: HospitalRecord | None,
    materials: list[Material],
    db: Session,
) -> dict:
    """重建某个病历分组的机器生成事件事实，保留医生确认过的事件。"""
    existing_events = db.query(MedicalEvent).filter(
        MedicalEvent.case_id == case.id,
        MedicalEvent.group_id == group.id,
    ).all()

    deleted = 0
    for event in existing_events:
        if _is_auto_event(event) and event.review_status != FactReviewStatus.CONFIRMED:
            db.delete(event)
            deleted += 1
    if deleted:
        db.flush()

    preserved_events = db.query(MedicalEvent).filter(
        MedicalEvent.case_id == case.id,
        MedicalEvent.group_id == group.id,
    ).all()
    preserved_keys = {_identity_from_event(event) for event in preserved_events}

    created = []
    skipped_duplicate = 0
    for proposal in _propose_events(hospital_record, materials):
        source_ids = _source_ids(proposal.materials)
        identity = _event_identity(proposal.event_type, proposal.event_date, proposal.title, source_ids)
        if identity in preserved_keys:
            skipped_duplicate += 1
            continue

        event = MedicalEvent(
            case_id=case.id,
            group_id=group.id,
            hospital_record_id=hospital_record.id if hospital_record else None,
            hospital_name=group.group_name,
            event_type=proposal.event_type,
            event_date=proposal.event_date,
            title=proposal.title,
            summary=proposal.summary,
            diagnosis=proposal.diagnosis,
            findings=proposal.findings,
            treatment=proposal.treatment,
            source_quote=proposal.source_quote,
            material_subtype=proposal.material_subtype,
            source_material_ids=_json_ids(source_ids),
            source_page_numbers=_json_ids(_source_pages(proposal.materials)),
            review_status=FactReviewStatus.PENDING,
            extraction_confidence=proposal.confidence,
            quality_flags=json.dumps(_proposal_flags(proposal), ensure_ascii=False),
        )
        db.add(event)
        created.append(event)
    db.flush()

    return {
        "created": len(created),
        "deleted_stale": deleted,
        "preserved": len(preserved_events),
        "skipped_duplicate": skipped_duplicate,
        "event_ids": [event.id for event in created],
    }
