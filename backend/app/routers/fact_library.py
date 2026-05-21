"""统一事实库接口。"""
import html
import json
import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.case import (
    Case,
    FactReviewStatus,
    HospitalRecord,
    ImagingReport,
    Material,
    MaterialGroup,
    MaterialType,
    OcrStatus,
    UnifiedFact,
)
from app.utils.source_material import material_page_payload, material_sequence_key
from app.utils.fact_library import (
    fact_counts,
    list_unified_facts,
    sync_unified_facts,
    unified_fact_to_dict,
    update_unified_fact,
)
from app.utils.medical_event_harness import rebuild_medical_events_for_group

router = APIRouter(prefix="/api/facts", tags=["统一事实库"])


_MEDICAL_STOP_LABELS = [
    "姓名", "性别", "年龄", "科室", "科室名称", "住院科室", "病床号", "床位号", "住院号",
    "入院日期", "入院时间", "出院日期", "出院时间", "住院天数", "实际住院",
    "主诉", "现病史", "既往史", "传染病史", "预防接种史", "手术史", "输血史",
    "个人史", "婚育史", "家族史", "体格检查", "专科检查", "辅助检查", "入院诊断",
    "诊疗经过", "治疗经过", "出院诊断", "出院情况", "出院医嘱", "医师签字", "手术记录",
]


def _clean_medical_ocr(text: str | None) -> str:
    """把 OCR 里的 HTML/Markdown 噪声压成适合规则提取的一行文本。"""
    text = html.unescape(str(text or ""))
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("|", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_labeled_text(
    text: str,
    labels: list[str],
    *,
    stop_labels: list[str] | None = None,
    max_chars: int = 800,
) -> str | None:
    if not text:
        return None
    stop_labels = stop_labels or _MEDICAL_STOP_LABELS
    for label in labels:
        stop_re = "|".join(re.escape(item) for item in stop_labels if item != label)
        pattern = rf"{re.escape(label)}\s*[：:]?\s*(.+?)(?=(?:{stop_re})\s*[：:]?\s*|$)"
        match = re.search(pattern, text)
        if not match:
            continue
        value = re.sub(r"\s+", " ", match.group(1)).strip(" ：:;；。")
        if value:
            return value[:max_chars].strip()
    return None


def _format_datetime_match(match: re.Match) -> str | None:
    try:
        year = int(match.group("y"))
        month = int(match.group("m"))
        day = int(match.group("d"))
        hour_raw = match.groupdict().get("h")
        minute_raw = match.groupdict().get("mi")
        if hour_raw:
            hour = int(hour_raw)
            minute = int(minute_raw or 0)
            return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}"
        return f"{year:04d}-{month:02d}-{day:02d}"
    except (TypeError, ValueError):
        return None


def _extract_datetime(texts: list[str], labels: list[str]) -> str | None:
    label_re = "|".join(re.escape(label) for label in labels)
    patterns = [
        (0, rf"(?:{label_re})\s*[：:]?\s*(?P<y>\d{{4}})\s*[年\-/\.]\s*(?P<m>\d{{1,2}})\s*[月\-/\.]\s*(?P<d>\d{{1,2}})\s*(?:日)?\s*(?P<h>\d{{1,2}})\s*[:时]\s*(?P<mi>\d{{1,2}})"),
        (1, rf"(?:{label_re})\s*[：:]?\s*(?P<y>\d{{4}})\s*[年\-/\.]\s*(?P<m>\d{{1,2}})\s*[月\-/\.]\s*(?P<d>\d{{1,2}})\s*(?:日)?\s*(?P<h>\d{{1,2}})\s*时"),
        (2, rf"(?:{label_re})\s*[：:]?\s*(?P<y>\d{{4}})\s*[年\-/\.]\s*(?P<m>\d{{1,2}})\s*[月\-/\.]\s*(?P<d>\d{{1,2}})\s*(?:日)?"),
    ]
    candidates: list[tuple[int, int, str]] = []
    for text_index, text in enumerate(texts):
        for precision, pattern in patterns:
            match = re.search(pattern, text)
            if match:
                value = _format_datetime_match(match)
                if value:
                    candidates.append((precision, text_index, value))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _extract_hospital_days(texts: list[str]) -> int | None:
    for text in texts:
        match = re.search(r"(?:住院天数|实际住院)\s*[：:]?\s*(\d+)\s*天?", text)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                continue
    return None


def _preferred_medical_source(materials: list[Material]) -> Material | None:
    preferred = {"medical_home_page": 0, "admission_record": 1, "discharge_record": 2}
    ordered = sorted(
        materials,
        key=lambda item: (
            preferred.get(item.material_subtype or "", 99),
            item.page_number if item.page_number is not None else 999999,
            item.id,
        ),
    )
    return ordered[0] if ordered else None


def _ensure_basic_hospital_record_from_ocr(
    case: Case,
    group: MaterialGroup,
    materials: list[Material],
    db: Session,
) -> HospitalRecord | None:
    """LLM 提取失败时，从住院首页/入院记录/出院小结中兜底生成基础病历事实。"""
    existing = db.query(HospitalRecord).filter(
        HospitalRecord.case_id == case.id,
        HospitalRecord.group_id == group.id,
    ).first()
    existing_is_editable_fallback = bool(
        existing
        and existing.review_status != FactReviewStatus.CONFIRMED
        and "rule_fallback_from_ocr" in str(existing.quality_flags or "")
    )
    if existing and not existing_is_editable_fallback:
        return existing

    clean_texts = [_clean_medical_ocr(material.ocr_text) for material in materials if material.ocr_text]
    clean_texts = [text for text in clean_texts if text]
    if not clean_texts:
        return existing
    merged_text = "\n".join(clean_texts)

    admission_date = _extract_datetime(clean_texts, ["入院日期", "入院时间"])
    discharge_date = _extract_datetime(clean_texts, ["出院日期", "出院时间"])
    admission_diagnosis = _extract_labeled_text(merged_text, ["入院诊断"], max_chars=800)
    discharge_diagnosis = _extract_labeled_text(merged_text, ["出院诊断"], max_chars=800)
    chief_complaint = _extract_labeled_text(merged_text, ["主诉"], max_chars=300)
    present_illness_history = _extract_labeled_text(merged_text, ["现病史", "入院情况"], max_chars=900)
    physical_examination = _extract_labeled_text(merged_text, ["体格检查", "专科检查"], max_chars=900)
    treatment_process = _extract_labeled_text(merged_text, ["诊疗经过", "治疗经过"], max_chars=900)
    discharge_orders = _extract_labeled_text(merged_text, ["出院医嘱"], max_chars=900)

    has_core_value = any([
        admission_date,
        discharge_date,
        admission_diagnosis,
        discharge_diagnosis,
        chief_complaint,
        present_illness_history,
    ])
    if not has_core_value:
        return existing

    source = _preferred_medical_source(materials)
    record = existing or HospitalRecord(case_id=case.id, group_id=group.id)
    record.material_id = source.id if source else record.material_id
    record.hospital_name = group.group_name
    record.admission_number = _extract_labeled_text(merged_text, ["住院号", "病案号"], max_chars=80)
    record.chief_complaint = chief_complaint
    record.present_illness_history = present_illness_history
    record.physical_examination = physical_examination
    record.admission_diagnosis = admission_diagnosis
    record.treatment_process = treatment_process
    record.discharge_diagnosis = discharge_diagnosis
    record.discharge_orders = discharge_orders
    record.admission_date = admission_date
    record.discharge_date = discharge_date
    record.hospital_days = _extract_hospital_days(clean_texts)
    record.review_status = record.review_status or FactReviewStatus.PENDING
    record.extraction_confidence = 70
    record.quality_flags = json.dumps(["rule_fallback_from_ocr", "needs_doctor_review"], ensure_ascii=False)
    if not existing:
        db.add(record)
    db.flush()
    return record


def _text_len(value) -> int:
    return len(str(value or "").strip())


def _hospital_record_needs_refresh(record: HospitalRecord | None) -> bool:
    """判断已有病历事实是否明显不完整，需要由事实库构建流程补提。

    医生已确认的病历事实不自动覆盖，避免把人工核验内容冲掉。
    """
    if not record:
        return True
    if record.review_status == FactReviewStatus.CONFIRMED:
        return False
    core_lengths = [
        _text_len(record.chief_complaint),
        _text_len(record.present_illness_history),
        _text_len(record.physical_examination),
        _text_len(record.admission_diagnosis),
        _text_len(record.discharge_diagnosis),
        _text_len(record.discharge_orders),
    ]
    missing_count = sum(1 for length in core_lengths if length == 0)
    has_dates = bool(record.admission_date or record.discharge_date)
    return (
        missing_count >= 3
        or not has_dates
        or _text_len(record.physical_examination) < 20
        or _text_len(record.discharge_orders) < 20
    )


@router.get("/case/{case_id}")
def get_case_facts(case_id: int, db: Session = Depends(get_db)):
    """获取案件统一事实库；首次进入时自动同步现有事实。"""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    facts = list_unified_facts(case_id, db)
    legacy_types = {"case_basic", "case_facts", "clinical_exam"}
    if not facts or any(fact.fact_type in legacy_types for fact in facts):
        return sync_unified_facts(case_id, db)
    return {
        "success": True,
        "facts": [unified_fact_to_dict(fact) for fact in facts],
        "counts": fact_counts(facts),
    }


@router.post("/case/{case_id}/sync")
def sync_case_facts(case_id: int, db: Session = Depends(get_db)):
    """从病历事实和检查事实同步到统一事实库。"""
    result = sync_unified_facts(case_id, db)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error") or "同步失败")
    return result


@router.post("/case/{case_id}/build")
def build_case_fact_library(case_id: int, db: Session = Depends(get_db)):
    """建立/更新事实库：先补提缺失的病历和检查事实，再汇总成统一事实。"""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    from app.routers.llm_extract import _extract_and_save, _generate_material_list, extract_medical_group

    medical_results = []
    groups = db.query(MaterialGroup).filter(
        MaterialGroup.case_id == case_id,
        MaterialGroup.material_type == MaterialType.MEDICAL_RECORD,
    ).order_by(MaterialGroup.sort_order).all()

    for group in groups:
        completed_materials = db.query(Material).filter(
            Material.group_id == group.id,
            Material.ocr_status == OcrStatus.COMPLETED,
            Material.ocr_text.isnot(None),
            Material.ocr_text != "",
        ).order_by(Material.page_number, Material.id).all()
        completed_count = len(completed_materials)
        record = db.query(HospitalRecord).filter(
            HospitalRecord.case_id == case_id,
            HospitalRecord.group_id == group.id,
        ).first()
        if not completed_count:
            medical_results.append({
                "group_id": group.id,
                "group_name": group.group_name,
                "status": "skipped",
                "reason": "没有已完成OCR的病历材料",
            })
            continue
        extract_result = None
        record_status = "skipped"
        should_refresh_record = _hospital_record_needs_refresh(record)
        had_record_before_refresh = record is not None
        if should_refresh_record:
            try:
                extract_result = extract_medical_group(case_id, group.id, db)
            except HTTPException as exc:
                extract_result = {
                    "group_id": group.id,
                    "group_name": group.group_name,
                    "status": "failed",
                    "error": exc.detail,
                }
            except Exception as exc:
                extract_result = {
                    "group_id": group.id,
                    "group_name": group.group_name,
                    "status": "failed",
                    "error": str(exc),
                }

            if extract_result and extract_result.get("status") == "completed":
                record = db.query(HospitalRecord).filter(
                    HospitalRecord.case_id == case_id,
                    HospitalRecord.group_id == group.id,
                ).first()
                record_status = "refreshed" if had_record_before_refresh else "completed"

        if not record:
            record = _ensure_basic_hospital_record_from_ocr(case, group, completed_materials, db)
            if record:
                record_status = "completed"

        event_result = {"created": 0, "deleted_stale": 0, "preserved": 0, "skipped_duplicate": 0}
        if record:
            event_result = rebuild_medical_events_for_group(case, group, record, completed_materials, db)
            medical_results.append({
                "group_id": group.id,
                "group_name": group.group_name,
                "status": "completed" if record_status in {"completed", "refreshed"} or event_result.get("created") else "skipped",
                "reason": (
                    "已有病历事实不完整，已补提并更新事件事实"
                    if record_status == "refreshed"
                    else ("已存在病历事实，已更新事件事实" if record_status == "skipped" else None)
                ),
                "method": "llm_or_rule_record_plus_event_harness",
                "record_id": record.id,
                "hospital_name": record.hospital_name,
                "events": event_result,
                "llm_error": extract_result.get("error") if extract_result else None,
            })
        elif extract_result:
            medical_results.append(extract_result)

    imaging_results = []
    imaging_materials = db.query(Material).filter(
        Material.case_id == case_id,
        Material.ocr_status == OcrStatus.COMPLETED,
        Material.ocr_text.isnot(None),
        Material.ocr_text != "",
        Material.material_type == MaterialType.IMAGING_REPORT,
    ).order_by(Material.page_number, Material.id).all()

    for material in imaging_materials:
        existing = db.query(ImagingReport).filter(
            ImagingReport.case_id == case_id,
            ImagingReport.material_id == material.id,
        ).first()
        if existing:
            imaging_results.append({
                "material_id": material.id,
                "filename": material.original_filename,
                "status": "skipped",
                "reason": "已存在检查事实",
                "report_id": existing.id,
            })
            continue
        try:
            imaging_results.append(_extract_and_save(material=material, case=case, db=db))
        except Exception as exc:
            imaging_results.append({
                "material_id": material.id,
                "filename": material.original_filename,
                "status": "failed",
                "error": str(exc),
            })

    try:
        _generate_material_list(case_id, case, db)
    except Exception:
        pass

    result = sync_unified_facts(case_id, db)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error") or "建立事实库失败")

    medical_completed = sum(1 for item in medical_results if item.get("status") == "completed")
    imaging_completed = sum(1 for item in imaging_results if item.get("status") == "completed")
    result.update({
        "message": f"事实库已建立/更新：新增病历事实 {medical_completed} 组，新增检查事实 {imaging_completed} 份，统一事实 {result.get('counts', {}).get('total', 0)} 条",
        "medical_results": medical_results,
        "imaging_results": imaging_results,
    })
    return result


@router.put("/{fact_id}")
def update_case_fact(fact_id: int, data: dict, db: Session = Depends(get_db)):
    """更新统一事实库条目的核验状态、重要性或医生备注。"""
    fact = update_unified_fact(db, fact_id, data)
    if not fact:
        raise HTTPException(status_code=404, detail="事实不存在")
    return unified_fact_to_dict(fact)


@router.get("/{fact_id}/source-pages")
def get_unified_fact_source_pages(fact_id: int, db: Session = Depends(get_db)):
    """获取统一事实对应的来源页，用于人工核验原图。"""
    import json

    fact = db.query(UnifiedFact).filter(UnifiedFact.id == fact_id).first()
    if not fact:
        raise HTTPException(status_code=404, detail="事实不存在")

    try:
        source_ids = json.loads(fact.source_material_ids or "[]")
    except (TypeError, json.JSONDecodeError):
        source_ids = []
    source_ids = [int(value) for value in source_ids if str(value).isdigit()]

    pages = []
    if source_ids:
        materials = db.query(Material).filter(Material.id.in_(source_ids)).all()
        by_id = {material.id: material for material in materials}
        pages = [by_id[source_id] for source_id in source_ids if source_id in by_id]
    pages = sorted(pages, key=material_sequence_key)
    return {
        "fact_id": fact.id,
        "count": len(pages),
        "pages": [material_page_payload(page) for page in pages],
    }
