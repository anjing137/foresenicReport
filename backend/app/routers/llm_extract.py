"""
LLM 智能提取路由
OCR 识别完成后，调用 LLM 从 OCR 文本中提取结构化字段
"""
import json
import logging
import re
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.models.case import (
    Case, CaseStatus, Material, MaterialGroup, MaterialType, OcrStatus,
    Person, HospitalRecord, ImagingReport, MedicalEvent, Report,
    StandardChunk, StandardDocument,
)
from app.utils.llm import (
    extract_fields, extract_case_summary, call_llm, extract_medical_group_fields,
    call_llm_json_harness, call_llm_text_harness,
)
from app.utils.standards import (
    _parse_entrustment_items,
    _map_entrustment_to_standard_ids,
    build_standard_toc,
    search_clauses_in_documents,
    format_selected_clauses,
    chunk_to_reference,
)
from app.utils.analysis_harness import (
    build_analysis_harness_payload,
    format_analysis_harness_for_prompt,
    list_saved_analysis_candidates,
    sync_analysis_candidates,
    update_saved_analysis_candidate,
    validate_analysis_text,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/llm", tags=["LLM 智能提取"])


# ==================== 用户确认字段保护机制 ====================

def _get_confirmed_fields(obj) -> set:
    """获取对象的已确认字段集合"""
    if not obj or not hasattr(obj, 'confirmed_fields') or not obj.confirmed_fields:
        return set()
    try:
        return set(json.loads(obj.confirmed_fields))
    except (json.JSONDecodeError, TypeError):
        return set()


def _mark_fields_confirmed(obj, field_names: list):
    """标记字段为已确认（用户手动保存时调用）"""
    confirmed = _get_confirmed_fields(obj)
    confirmed.update(field_names)
    obj.confirmed_fields = json.dumps(list(confirmed), ensure_ascii=False)


def _safe_setattr(obj, field_name: str, value, confirmed_fields: set = None):
    """安全设置属性：如果字段已确认，则不覆盖"""
    if confirmed_fields is None:
        confirmed_fields = _get_confirmed_fields(obj)
    if field_name in confirmed_fields:
        return False  # 字段已确认，跳过
    if value is not None and value != "":
        setattr(obj, field_name, value)
        return True
    return False


def _write_report_field(report, field_name: str, value) -> bool:
    """安全写入报告字段：如果用户已手动保存确认过该字段，则不覆盖，返回是否写入成功"""
    if _safe_setattr(report, field_name, value):
        return True
    logger.info(f"报告字段 {field_name} 已被用户确认，跳过 LLM 生成覆盖")
    return False


def _plain_ocr_text(text: str, remove_whitespace: bool = False) -> str:
    """将 OCR Markdown/HTML 清理为便于规则抽取的纯文本。"""
    if not text:
        return ""

    text = text.replace("<nl>", "\n")
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"#+", " ", text)

    if remove_whitespace:
        return re.sub(r"\s+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_extracted_phrase(text: str) -> str:
    """清理抽出的短语，保留中文标点含义但去掉边界噪声。"""
    text = _plain_ocr_text(text, remove_whitespace=True)
    text = re.sub(r"^[：:；;，,。、\s]+", "", text)
    text = re.sub(r"[；;，,。、\s]+$", "", text)
    return text


def _ordered_unique(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        value = (value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _get_person_name(case: Case, db: Session, fallback: str = "") -> str:
    person = db.query(Person).filter(Person.case_id == case.id).first()
    return (
        fallback
        or (person.name if person and person.name else "")
        or case.person_name
        or "被鉴定人"
    )


def _normalize_entrustment_matter(matter: str, person_name: str = "") -> str:
    """把委托事项统一成可直接接在“特委托我鉴定中心”后的短语。"""
    matter = _clean_extracted_phrase(matter)
    person_name = person_name or "被鉴定人"

    if not matter:
        return f"对{person_name}进行鉴定"

    matter = matter.replace("请求委托鉴定机构", "")
    matter = matter.replace("请求依法委托鉴定机构", "")
    matter = matter.replace("请求", "")

    if person_name and person_name != "被鉴定人":
        matter = re.sub(r"对(?:申请人|被鉴定人|伤者|原告)的", f"对{person_name}的", matter)
        matter = re.sub(r"对(?:申请人|被鉴定人|伤者|原告)", f"对{person_name}", matter)
        matter = re.sub(r"(?:申请人|被鉴定人|伤者|原告)的", f"{person_name}的", matter)

    if not matter.startswith("对"):
        if matter.startswith(person_name):
            matter = f"对{matter}"
        elif person_name and person_name != "被鉴定人":
            matter = f"对{person_name}的{matter}"
        else:
            matter = f"对{matter}"

    if not matter.endswith("鉴定"):
        matter = matter.rstrip("。；;，,、")
        matter = f"{matter}进行鉴定"

    return matter


def _extract_entrustment_requirement(text: str) -> str:
    """优先从司法鉴定委托书的“鉴定要求/鉴定事项”等字段抽取真正委托事项。"""
    compact = _plain_ocr_text(text, remove_whitespace=True)
    if not compact:
        return ""

    stop_words = (
        "现将|请根据|鉴定完毕|委托书和相关材料|相关材料移交|"
        "原阳县人民法院|新乡县人民法院|卫辉市人民法院|获嘉县人民法院|"
        "督办人|联系电话|联系人|年月日|[0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日"
    )
    headings = ["鉴定要求", "鉴定事项", "委托鉴定事项", "委托事项", "鉴定项目"]
    for heading in headings:
        match = re.search(rf"{heading}[：:；;]?(.*?)(?=(?:{stop_words}|$))", compact)
        if match:
            phrase = _clean_extracted_phrase(match.group(1))
            # 案由常以“一案”结尾，不应作为委托事项；继续寻找更具体的“鉴定要求”。
            if phrase and not phrase.endswith("一案"):
                return phrase
    return ""


def _extract_appraisal_application_items(text: str) -> str:
    """从鉴定申请书中抽取“申请事项”，仅在委托书缺少鉴定要求时作兜底。"""
    compact = _plain_ocr_text(text, remove_whitespace=True)
    if not compact:
        return ""

    match = re.search(r"申请事项[：:]?(.*?)(?=(?:事实与理由|此致|申请人[：:]|[0-9]{4}年|$))", compact)
    if not match:
        return ""

    raw_items = match.group(1)
    items = re.findall(r"(?:\d+[、.．])(.+?)(?=\d+[、.．]|$)", raw_items)
    if not items:
        items = [raw_items]

    cleaned = [_clean_extracted_phrase(item) for item in items]
    cleaned = [item for item in cleaned if item]
    return "；".join(cleaned)


def _get_treatment_hospitals(case_id: int, db: Session) -> list[str]:
    """获取基本案情里的就诊医院，结构化病历优先，分组和 OCR 兜底。"""
    hospitals = []

    records = db.query(HospitalRecord).filter(
        HospitalRecord.case_id == case_id,
    ).order_by(HospitalRecord.admission_date).all()
    hospitals.extend(r.hospital_name for r in records if r.hospital_name)

    groups = db.query(MaterialGroup).filter(
        MaterialGroup.case_id == case_id,
        MaterialGroup.material_type == MaterialType.MEDICAL_RECORD,
    ).order_by(MaterialGroup.sort_order, MaterialGroup.id).all()
    hospitals.extend(g.group_name for g in groups if g.group_name)

    if not hospitals:
        materials = db.query(Material).filter(
            Material.case_id == case_id,
            Material.material_type == MaterialType.MEDICAL_RECORD,
            Material.ocr_text.isnot(None),
            Material.ocr_text != "",
        ).order_by(Material.page_number, Material.id).limit(8).all()
        for material in materials:
            text = _plain_ocr_text(material.ocr_text)
            hospitals.extend(re.findall(r"[\u4e00-\u9fa5]{2,30}(?:人民医院|中心医院|中医院|医院|卫生院)", text))

    invalid = {"医院", "某医院", "未知医院", "未分组"}
    return [h for h in _ordered_unique(hospitals) if h not in invalid]


@router.post("/cases/{case_id}/extract-all")
def extract_all_materials(case_id: int, db: Session = Depends(get_db)):
    """
    对案件所有已 OCR 识别的材料进行 LLM 字段提取
    提取结果写入对应的数据库表（Person, HospitalRecord, ImagingReport 等）
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    # 获取所有已完成 OCR 的材料
    materials = db.query(Material).filter(
        Material.case_id == case_id,
        Material.ocr_status == OcrStatus.COMPLETED,
        Material.ocr_text.isnot(None),
        Material.ocr_text != "",
    ).all()

    if not materials:
        raise HTTPException(status_code=400, detail="没有已识别的材料，请先进行 OCR 识别")

    results = []

    for mat in materials:
        try:
            result = _extract_and_save(material=mat, case=case, db=db)
            results.append(result)
        except Exception as e:
            logger.error(f"材料 {mat.id} 提取失败: {str(e)}")
            results.append({
                "material_id": mat.id,
                "filename": mat.original_filename,
                "material_type": mat.material_type,
                "status": "failed",
                "error": str(e),
            })

    # 统计
    success_count = sum(1 for r in results if r.get("status") == "completed")
    failed_count = sum(1 for r in results if r.get("status") == "failed")
    skipped_count = sum(1 for r in results if r.get("status") == "skipped")

    return {
        "message": f"提取完成：成功 {success_count} 份，跳过 {skipped_count} 份，失败 {failed_count} 份",
        "total": len(results),
        "success": success_count,
        "skipped": skipped_count,
        "failed": failed_count,
        "results": results,
    }


@router.post("/materials/{material_id}/extract")
def extract_single_material(material_id: int, db: Session = Depends(get_db)):
    """对单份材料进行 LLM 字段提取"""
    material = db.query(Material).filter(Material.id == material_id).first()
    if not material:
        raise HTTPException(status_code=404, detail="材料不存在")

    if material.ocr_status != OcrStatus.COMPLETED or not material.ocr_text:
        raise HTTPException(status_code=400, detail="材料尚未完成 OCR 识别")

    case = db.query(Case).filter(Case.id == material.case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="关联案件不存在")

    try:
        result = _extract_and_save(material=material, case=case, db=db)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LLM 提取失败: {str(e)}")


@router.post("/cases/{case_id}/extract-basic-info")
def extract_basic_info(case_id: int, db: Session = Depends(get_db)):
    """
    只提取「基本情况」页面相关字段：
    - 从委托书提取：委托单位、委托事项（唯一来源，不做化简）
    - 从身份证提取：被鉴定人姓名、性别、出生日期、身份证号、住址
    - 从鉴定申请书提取：委托单位（补充，委托书优先）、申请人、案件简介
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    # 收集相关材料
    target_types = [MaterialType.ENTRUSTMENT_LETTER, MaterialType.ID_CARD, MaterialType.APPRAISAL_APPLICATION]
    materials = db.query(Material).filter(
        Material.case_id == case_id,
        Material.ocr_status == OcrStatus.COMPLETED,
        Material.ocr_text.isnot(None),
        Material.ocr_text != "",
        Material.material_type.in_(target_types),
    ).all()

    if not materials:
        raise HTTPException(status_code=400, detail="没有已识别的委托书/身份证/鉴定申请书材料")

    results = []
    for mat in materials:
        try:
            result = _extract_and_save(material=mat, case=case, db=db)
            results.append(result)
        except Exception as e:
            logger.error(f"材料 {mat.id} 提取失败: {str(e)}")
            results.append({"material_id": mat.id, "status": "failed", "error": str(e)})

    # 自动生成材料清单
    _generate_material_list(case_id, case, db)

    # 重新加载案件和人员数据返回
    db.refresh(case)
    person = db.query(Person).filter(Person.case_id == case_id).first()

    success_count = sum(1 for r in results if r.get("status") == "completed")

    return {
        "message": f"基本情况提取完成，成功 {success_count}/{len(results)} 份",
        "results": results,
        "case": {
            "entrusting_unit": case.entrusting_unit,
            "entrustment_matter": case.entrustment_matter,
            "material_list": case.material_list,
        },
        "person": {
            "name": person.name if person else None,
            "gender": person.gender if person else None,
            "birth_date": person.birth_date if person else None,
            "id_number": person.id_number if person else None,
            "address": person.address if person else None,
        } if person else None,
    }


@router.post("/cases/{case_id}/extract-case-facts")
def extract_case_facts_api(case_id: int, db: Session = Depends(get_db)):
    """
    只提取「基本案情」：
    - 从委托书/交通事故认定书/鉴定申请书生成 case_facts 文本
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    # 收集相关材料（委托书、交通事故认定书、鉴定申请书）
    target_types = [MaterialType.ENTRUSTMENT_LETTER, MaterialType.TRAFFIC_ACCIDENT_CERT, MaterialType.APPRAISAL_APPLICATION]
    materials = db.query(Material).filter(
        Material.case_id == case_id,
        Material.ocr_status == OcrStatus.COMPLETED,
        Material.ocr_text.isnot(None),
        Material.ocr_text != "",
        Material.material_type.in_(target_types),
    ).all()

    if not materials:
        raise HTTPException(status_code=400, detail="没有已识别的委托书/交通事故认定书/鉴定申请书材料")

    # 先提取各材料的结构化字段（确保委托书等的基本信息已入库）
    for mat in materials:
        try:
            _extract_and_save(material=mat, case=case, db=db)
        except Exception as e:
            logger.error(f"材料 {mat.id} 提取失败: {str(e)}")

    # 然后生成基本案情文本
    result = extract_case_facts_text(case_id, db)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "生成失败"))

    # 写入报告（已确认字段不覆盖）
    report = db.query(Report).filter(Report.case_id == case_id).first()
    if not report:
        report = Report(case_id=case_id)
        db.add(report)
        db.flush()

    overwritten = _write_report_field(report, "case_facts", result["case_facts"])
    db.commit()
    db.refresh(report)

    msg = "基本案情已生成" if overwritten else "基本案情已生成（您手动修改的内容已保留，未覆盖）"
    return {
        "message": msg,
        "case_facts": result["case_facts"],
        "model": result.get("model", ""),
    }


@router.get("/cases/{case_id}/medical-groups")
def get_medical_groups(case_id: int, db: Session = Depends(get_db)):
    """
    获取案件的病历医院分组信息（用于前端展示提取按钮）
    不调用LLM，纯数据库查询，瞬间返回
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    # 查找所有病历类型的 MaterialGroup
    groups = db.query(MaterialGroup).filter(
        MaterialGroup.case_id == case_id,
        MaterialGroup.material_type == MaterialType.MEDICAL_RECORD,
    ).order_by(MaterialGroup.sort_order).all()

    if not groups:
        # 没有分组，看看是否有未分组的病历材料
        ungrouped = db.query(Material).filter(
            Material.case_id == case_id,
            Material.material_type == MaterialType.MEDICAL_RECORD,
            Material.group_id.is_(None),
        ).count()
        if ungrouped > 0:
            return {
                "groups": [{
                    "group_id": None,
                    "group_name": "未分组的病历",
                    "material_count": ungrouped,
                    "completed_count": db.query(Material).filter(
                        Material.case_id == case_id,
                        Material.material_type == MaterialType.MEDICAL_RECORD,
                        Material.group_id.is_(None),
                        Material.ocr_status == OcrStatus.COMPLETED,
                    ).count(),
                }],
                "total_groups": 1,
            }
        return {"groups": [], "total_groups": 0}

    result_groups = []
    for g in groups:
        all_mats = db.query(Material).filter(
            Material.group_id == g.id,
        ).all()
        completed_mats = [m for m in all_mats if m.ocr_status == OcrStatus.COMPLETED and m.ocr_text]

        # 检查是否已有住院记录（按 group_id 精确匹配）
        existing_record = db.query(HospitalRecord).filter(
            HospitalRecord.case_id == case_id,
            HospitalRecord.group_id == g.id,
        ).first()

        result_groups.append({
            "group_id": g.id,
            "group_name": g.group_name,
            "material_count": len(all_mats),
            "completed_count": len(completed_mats),
            "has_record": existing_record is not None,
            "record_id": existing_record.id if existing_record else None,
        })

    return {
        "groups": result_groups,
        "total_groups": len(result_groups),
    }


@router.post("/cases/{case_id}/extract-medical-group/{group_id}")
def extract_medical_group(case_id: int, group_id: int, db: Session = Depends(get_db)):
    """
    按医院分组提取病历：同一医院的所有页面OCR文本拼接，一次LLM调用生成一条完整住院记录
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    group = db.query(MaterialGroup).filter(
        MaterialGroup.id == group_id,
        MaterialGroup.case_id == case_id,
    ).first()
    if not group:
        raise HTTPException(status_code=404, detail="分组不存在")

    # 获取该分组下所有已完成OCR的材料，按页码排序
    materials = db.query(Material).filter(
        Material.group_id == group_id,
        Material.ocr_status == OcrStatus.COMPLETED,
        Material.ocr_text.isnot(None),
        Material.ocr_text != "",
    ).order_by(Material.page_number, Material.id).all()

    if not materials:
        raise HTTPException(status_code=400, detail=f"{group.group_name} 没有已识别的病历材料")

    # 拼接OCR文本
    combined_text = ""
    for i, mat in enumerate(materials, 1):
        combined_text += f"\n\n===== 第{i}页 ({mat.original_filename}) =====\n"
        combined_text += mat.ocr_text or ""

    combined_text = combined_text.strip()

    # 限制总长度（Qwen3-8B上下文约32K，输入留12K字符安全）
    if len(combined_text) > 12000:
        combined_text = combined_text[:12000] + "\n\n[... 文本过长，已截断 ...]"

    # 调用LLM提取（用新的合并提取Prompt）
    result = _extract_medical_group_llm(group.group_name, combined_text)

    if not result.get("success"):
        return {
            "group_id": group_id,
            "group_name": group.group_name,
            "status": "failed",
            "error": result.get("error", "提取失败"),
        }

    fields = result["fields"]

    # 按 group_id 查找是否已有记录（同一group重新生成时覆盖）
    existing_record = db.query(HospitalRecord).filter(
        HospitalRecord.case_id == case.id,
        HospitalRecord.group_id == group.id,
    ).first()

    if existing_record:
        record = existing_record
        logger.info(f"重新生成住院记录: {group.group_name} group_id={group.id}, record_id={record.id}")
    else:
        record = HospitalRecord(case_id=case.id, group_id=group.id)
        db.add(record)
        db.flush()
        logger.info(f"新建住院记录: {group.group_name} group_id={group.id}, record_id={record.id}")

    # 更新字段（只更新有值的，空值不覆盖已有数据）
    field_map = {
        "hospital_name": "hospital_name",
        "admission_number": "admission_number",
        "chief_complaint": "chief_complaint",
        "present_illness_history": "present_illness_history",
        "past_history": "past_history",
        "physical_examination": "physical_examination",
        "admission_diagnosis": "admission_diagnosis",
        "treatment_process": "treatment_process",
        "medication": "medication",
        "discharge_diagnosis": "discharge_diagnosis",
        "discharge_orders": "discharge_orders",
        "admission_date": "admission_date",
        "discharge_date": "discharge_date",
    }

    for json_key, db_key in field_map.items():
        value = fields.get(json_key)
        if value is not None and value != "" and value != "null":
            setattr(record, db_key, value)

    # 住院天数
    if fields.get("hospital_days") is not None:
        try:
            record.hospital_days = int(fields["hospital_days"])
        except (ValueError, TypeError):
            pass

    db.commit()
    db.refresh(record)

    # 更新材料清单
    _generate_material_list(case_id, case, db)

    return {
        "group_id": group_id,
        "group_name": group.group_name,
        "status": "completed",
        "record_id": record.id,
        "hospital_name": record.hospital_name,
        "admission_number": record.admission_number,
        "fields": fields,
        "model": result.get("model", ""),
        "usage": result.get("usage", {}),
    }


@router.post("/cases/{case_id}/extract-medical-records")
def extract_medical_records_api(case_id: int, db: Session = Depends(get_db)):
    """
    一键提取所有医院的病历（逐个分组调用，每个医院一次LLM）
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    # 获取所有病历分组
    groups = db.query(MaterialGroup).filter(
        MaterialGroup.case_id == case_id,
        MaterialGroup.material_type == MaterialType.MEDICAL_RECORD,
    ).order_by(MaterialGroup.sort_order).all()

    if not groups:
        raise HTTPException(status_code=400, detail="没有病历分组，请先上传病历材料")

    results = []
    for group in groups:
        # 获取该分组下已OCR的材料
        materials = db.query(Material).filter(
            Material.group_id == group.id,
            Material.ocr_status == OcrStatus.COMPLETED,
            Material.ocr_text.isnot(None),
            Material.ocr_text != "",
        ).order_by(Material.page_number, Material.id).all()

        if not materials:
            results.append({
                "group_id": group.id,
                "group_name": group.group_name,
                "status": "skipped",
                "error": "没有已识别的材料",
            })
            continue

        # 拼接OCR文本
        combined_text = ""
        for i, mat in enumerate(materials, 1):
            combined_text += f"\n\n===== 第{i}页 ({mat.original_filename}) =====\n"
            combined_text += mat.ocr_text or ""

        combined_text = combined_text.strip()
        if len(combined_text) > 12000:
            combined_text = combined_text[:12000] + "\n\n[... 文本过长，已截断 ...]"

        # 调用LLM
        result = _extract_medical_group_llm(group.group_name, combined_text)

        if not result.get("success"):
            results.append({
                "group_id": group.id,
                "group_name": group.group_name,
                "status": "failed",
                "error": result.get("error", "提取失败"),
            })
            continue

        fields = result["fields"]

        # 按 group_id 查找是否已有记录（重新生成时覆盖，否则新建）
        existing_record = db.query(HospitalRecord).filter(
            HospitalRecord.case_id == case.id,
            HospitalRecord.group_id == group.id,
        ).first()

        if existing_record:
            record = existing_record
            logger.info(f"重新生成住院记录: {group.group_name} group_id={group.id}, record_id={record.id}")
        else:
            record = HospitalRecord(case_id=case.id, group_id=group.id)
            db.add(record)
            db.flush()
            logger.info(f"新建住院记录: {group.group_name} group_id={group.id}, record_id={record.id}")

        field_map = {
            "hospital_name": "hospital_name",
            "admission_number": "admission_number",
            "chief_complaint": "chief_complaint",
            "present_illness_history": "present_illness_history",
            "past_history": "past_history",
            "physical_examination": "physical_examination",
            "admission_diagnosis": "admission_diagnosis",
            "treatment_process": "treatment_process",
            "medication": "medication",
            "discharge_diagnosis": "discharge_diagnosis",
            "discharge_orders": "discharge_orders",
            "admission_date": "admission_date",
            "discharge_date": "discharge_date",
        }

        for json_key, db_key in field_map.items():
            value = fields.get(json_key)
            if value is not None and value != "" and value != "null":
                setattr(record, db_key, value)

        if fields.get("hospital_days") is not None:
            try:
                record.hospital_days = int(fields["hospital_days"])
            except (ValueError, TypeError):
                pass

        db.commit()

        results.append({
            "group_id": group.id,
            "group_name": group.group_name,
            "status": "completed",
            "record_id": record.id,
            "hospital_name": record.hospital_name,
        })

    # 生成资料摘要
    summary_result = _generate_material_summary(case_id, db)
    if summary_result.get("success"):
        report = db.query(Report).filter(Report.case_id == case_id).first()
        if not report:
            report = Report(case_id=case_id)
            db.add(report)
            db.flush()
        _write_report_field(report, "material_summary", summary_result["material_summary"])
        db.commit()

    # 更新材料清单
    _generate_material_list(case_id, case, db)

    # 重新加载住院记录
    records = db.query(HospitalRecord).filter(HospitalRecord.case_id == case_id).all()

    success_count = sum(1 for r in results if r.get("status") == "completed")

    return {
        "message": f"病历提取完成，成功 {success_count}/{len(results)} 家医院",
        "results": results,
        "material_summary": summary_result.get("material_summary", ""),
        "hospital_records": [
            {
                "id": r.id,
                "hospital_name": r.hospital_name,
                "admission_number": r.admission_number,
                "chief_complaint": r.chief_complaint,
                "present_illness_history": r.present_illness_history,
                "past_history": r.past_history,
                "physical_examination": r.physical_examination,
                "admission_diagnosis": r.admission_diagnosis,
                "treatment_process": r.treatment_process,
                "medication": r.medication,
                "discharge_diagnosis": r.discharge_diagnosis,
                "discharge_orders": r.discharge_orders,
                "admission_date": r.admission_date,
                "discharge_date": r.discharge_date,
                "hospital_days": r.hospital_days,
            }
            for r in records
        ],
    }


@router.post("/cases/{case_id}/extract-imaging-reports")
def extract_imaging_reports_api(case_id: int, db: Session = Depends(get_db)):
    """
    只提取影像学报告数据到 ImagingReport 表：
    - 从影像学报告材料调用 LLM 提取各字段
    - 不自动生成鉴定过程，用户需手动点击「生成鉴定过程」
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    # 收集影像学材料
    materials = db.query(Material).filter(
        Material.case_id == case_id,
        Material.ocr_status == OcrStatus.COMPLETED,
        Material.ocr_text.isnot(None),
        Material.ocr_text != "",
        Material.material_type == MaterialType.IMAGING_REPORT,
    ).all()

    if not materials:
        raise HTTPException(status_code=400, detail="没有已识别的影像学报告材料")

    results = []
    for mat in materials:
        try:
            result = _extract_and_save(material=mat, case=case, db=db)
            results.append(result)
        except Exception as e:
            logger.error(f"材料 {mat.id} 提取失败: {str(e)}")
            results.append({"material_id": mat.id, "status": "failed", "error": str(e)})

    # 更新材料清单（可能有新的影像报告）
    _generate_material_list(case_id, case, db)

    # 重新加载影像学报告
    reports = db.query(ImagingReport).filter(ImagingReport.case_id == case_id).all()

    success_count = sum(1 for r in results if r.get("status") == "completed")

    return {
        "message": f"影像学报告提取完成，成功 {success_count}/{len(results)} 份",
        "results": results,
        "imaging_reports": [
            {
                "id": r.id,
                "report_date": r.report_date,
                "hospital_name": r.hospital_name,
                "exam_type": r.exam_type,
                "exam_part": r.exam_part,
                "film_number": r.film_number,
                "film_count": r.film_count,
                "report_content": r.report_content,
            }
            for r in reports
        ],
    }


@router.post("/cases/{case_id}/generate-material-summary")
def generate_material_summary_api(case_id: int, db: Session = Depends(get_db)):
    """
    独立生成「资料摘要」：
    - 从已提取的住院记录调用LLM生成资料摘要
    - 与病历提取分离，可单独执行
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    # 检查是否有住院记录
    records = db.query(HospitalRecord).filter(HospitalRecord.case_id == case_id).all()
    if not records:
        raise HTTPException(status_code=400, detail="请先提取病历（住院记录为空）")

    # 生成资料摘要
    summary_result = _generate_material_summary(case_id, db)

    if summary_result.get("success"):
        # 写入报告（已确认字段不覆盖）
        report = db.query(Report).filter(Report.case_id == case_id).first()
        if not report:
            report = Report(case_id=case_id)
            db.add(report)
            db.flush()
        overwritten = _write_report_field(report, "material_summary", summary_result["material_summary"])
        db.commit()

        msg = "资料摘要生成成功" if overwritten else "资料摘要生成成功（您手动修改的内容已保留，未覆盖）"
        return {
            "message": msg,
            "material_summary": summary_result["material_summary"],
            "model": summary_result.get("model", ""),
        }
    else:
        raise HTTPException(status_code=400, detail=summary_result.get("error", "生成失败"))


@router.post("/cases/{case_id}/generate-appraisal-process")
def generate_appraisal_process_api(case_id: int, db: Session = Depends(get_db)):
    """
    独立生成「鉴定过程」：
    - 使用 LLM 根据法医临床检查和影像学报告生成规范格式文本
    - 包含套话、法医检查信息、影像复阅等内容
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    # 生成鉴定过程
    result = _generate_appraisal_process(case_id, db)

    if result.get("success"):
        # 写入报告（已确认字段不覆盖）
        report = db.query(Report).filter(Report.case_id == case_id).first()
        if not report:
            report = Report(case_id=case_id)
            db.add(report)
            db.flush()
        overwritten = _write_report_field(report, "appraisal_process", result["appraisal_process"])
        db.commit()

        msg = "鉴定过程生成成功" if overwritten else "鉴定过程生成成功（您手动修改的内容已保留，未覆盖）"
        return {
            "message": "鉴定过程生成成功",
            "appraisal_process": result["appraisal_process"],
            "method": result.get("method", "unknown"),
        }
    else:
        raise HTTPException(status_code=400, detail=result.get("error", "生成失败"))


@router.post("/cases/{case_id}/generate-analysis")
def generate_analysis_api(case_id: int, db: Session = Depends(get_db)):
    """
    生成「分析说明」：
    - 基于住院记录和影像学报告综合分析
    - 生成 analysis 文本
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    # 检查是否有住院记录或影像学报告
    records = db.query(HospitalRecord).filter(HospitalRecord.case_id == case_id).all()
    img_reports = db.query(ImagingReport).filter(ImagingReport.case_id == case_id).all()

    if not records and not img_reports:
        raise HTTPException(status_code=400, detail="请先提取病历和影像学报告")

    result = _generate_analysis(case_id, db)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "生成失败"))

    report = db.query(Report).filter(Report.case_id == case_id).first()
    if not report:
        report = Report(case_id=case_id)
        db.add(report)
        db.flush()

    overwritten = _write_report_field(report, "analysis", result["analysis"])
    db.commit()
    db.refresh(report)

    msg = "分析说明已生成" if overwritten else "分析说明已生成（您手动修改的内容已保留，未覆盖）"
    return {
        "message": msg,
        "analysis": result["analysis"],
        "method": result.get("method", "unknown"),
        "standard_references": result.get("standard_references", []),
        "analysis_warnings": result.get("warnings", []),
    }


@router.get("/cases/{case_id}/analysis-harness")
def get_analysis_harness_api(case_id: int, db: Session = Depends(get_db)):
    """
    生成分析说明前的护栏材料：
    - 案件基础事实
    - 底层病历/检查证据
    - 伤残及三期候选清单
    - 精准规范条款映射
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")
    return build_analysis_harness_payload(case_id, db)


@router.get("/cases/{case_id}/analysis-candidates")
def list_analysis_candidates_api(case_id: int, db: Session = Depends(get_db)):
    """读取已落库的分析候选；如尚未生成，则自动生成一版。"""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")
    candidates = list_saved_analysis_candidates(case_id, db)
    if not candidates:
        payload = sync_analysis_candidates(case_id, db)
        return {
            "candidates": payload.get("candidates", []),
            "warnings": payload.get("warnings", []),
            "standard_references": payload.get("standard_references", []),
        }
    return {"candidates": candidates, "warnings": [], "standard_references": []}


@router.post("/cases/{case_id}/analysis-candidates/refresh")
def refresh_analysis_candidates_api(case_id: int, db: Session = Depends(get_db)):
    """重新计算候选并落库，保留医生已采信/排除状态。"""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")
    payload = sync_analysis_candidates(case_id, db)
    return {
        "candidates": payload.get("candidates", []),
        "warnings": payload.get("warnings", []),
        "standard_references": payload.get("standard_references", []),
    }


@router.put("/analysis-candidates/{candidate_id}")
def update_analysis_candidate_api(candidate_id: int, payload: dict, db: Session = Depends(get_db)):
    """更新分析候选核验状态。"""
    try:
        candidate = update_saved_analysis_candidate(
            candidate_id,
            db,
            status=payload.get("status"),
            review_note=payload.get("review_note"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not candidate:
        raise HTTPException(status_code=404, detail="分析候选不存在")
    return candidate


@router.post("/cases/{case_id}/generate-opinion")
def generate_opinion_api(case_id: int, db: Session = Depends(get_db)):
    """
    生成「鉴定意见」：
    - 基于分析说明和委托事项生成
    - 生成 opinion 文本
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    report = db.query(Report).filter(Report.case_id == case_id).first()
    if not report or not report.analysis:
        raise HTTPException(status_code=400, detail="请先生成分析说明")

    result = _generate_opinion(case_id, db)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "生成失败"))

    overwritten = _write_report_field(report, "opinion", result["opinion"])
    db.commit()
    db.refresh(report)

    msg = "鉴定意见已生成" if overwritten else "鉴定意见已生成（您手动修改的内容已保留，未覆盖）"
    return {
        "message": msg,
        "opinion": result["opinion"],
        "model": result.get("model", ""),
    }


def _extract_and_save(material: Material, case: Case, db) -> dict:
    """
    对单份材料调用 LLM 提取并保存结果到对应数据库表
    """
    material_type = material.material_type
    ocr_text = material.ocr_text

    if material_type == MaterialType.LITIGATION_MATERIAL:
        return {
            "material_id": material.id,
            "filename": material.original_filename,
            "material_type": material_type,
            "status": "skipped",
            "message": "诉讼材料暂作为背景材料保存，不做结构化提取",
        }

    # 调用 LLM 提取
    result = extract_fields(material_type, ocr_text)

    if not result.get("success"):
        return {
            "material_id": material.id,
            "filename": material.original_filename,
            "material_type": material_type,
            "status": "failed",
            "error": result.get("error", "提取失败"),
        }

    fields = result["fields"]

    # 根据材料类型保存到不同表
    saved_info = {}

    if material_type == MaterialType.ENTRUSTMENT_LETTER:
        # 委托书 → 更新 Case 基本情况（已确认字段不覆盖）
        case_confirmed = _get_confirmed_fields(case)
        person_name = _get_person_name(case, db, fields.get("person_name") or "")
        requirement = _extract_entrustment_requirement(ocr_text)
        if requirement:
            fields["entrustment_matter"] = _normalize_entrustment_matter(requirement, person_name)

        if fields.get("entrusting_unit"):
            _safe_setattr(case, "entrusting_unit", fields["entrusting_unit"], case_confirmed)
        if fields.get("entrustment_matter"):
            _safe_setattr(case, "entrustment_matter", fields["entrustment_matter"], case_confirmed)
        if fields.get("person_name"):
            _safe_setattr(case, "person_name", fields["person_name"], case_confirmed)
        saved_info = {
            "entrusting_unit": fields.get("entrusting_unit"),
            "entrustment_matter": fields.get("entrustment_matter"),
            "person_name": fields.get("person_name"),
        }

    elif material_type == MaterialType.ID_CARD:
        # 身份证 → 更新/创建 Person（已确认字段不覆盖）
        person = db.query(Person).filter(Person.case_id == case.id).first()
        if not person:
            person = Person(case_id=case.id)
            db.add(person)
            db.flush()

        person_confirmed = _get_confirmed_fields(person)
        if fields.get("name"):
            _safe_setattr(person, "name", fields["name"], person_confirmed)
            _safe_setattr(case, "person_name", fields["name"], _get_confirmed_fields(case))
        if fields.get("gender"):
            _safe_setattr(person, "gender", fields["gender"], person_confirmed)
        if fields.get("birth_date"):
            _safe_setattr(person, "birth_date", fields["birth_date"], person_confirmed)
        if fields.get("id_number"):
            _safe_setattr(person, "id_number", fields["id_number"], person_confirmed)
        if fields.get("address"):
            _safe_setattr(person, "address", fields["address"], person_confirmed)
        saved_info = {
            "name": fields.get("name"),
            "gender": fields.get("gender"),
            "id_number": fields.get("id_number"),
        }

    elif material_type == MaterialType.TRAFFIC_ACCIDENT_CERT:
        # 交通事故认定书 → 更新 Case 事故信息（已确认字段不覆盖）
        case_confirmed = _get_confirmed_fields(case)
        if fields.get("accident_date"):
            _safe_setattr(case, "accident_date", fields["accident_date"], case_confirmed)
        if fields.get("accident_location"):
            _safe_setattr(case, "accident_location", fields["accident_location"], case_confirmed)
        if fields.get("accident_description"):
            _safe_setattr(case, "accident_description", fields["accident_description"], case_confirmed)
        saved_info = {
            "accident_date": fields.get("accident_date"),
            "accident_location": fields.get("accident_location"),
            "accident_description": fields.get("accident_description"),
            "responsibility": fields.get("responsibility"),
        }

    elif material_type == MaterialType.MEDICAL_RECORD:
        # 病历 → 创建/更新 HospitalRecord
        # 按住院号查找是否已有记录（同一住院号合并）
        admission_number = fields.get("admission_number", "")
        existing_record = None
        if admission_number:
            existing_record = db.query(HospitalRecord).filter(
                HospitalRecord.case_id == case.id,
                HospitalRecord.admission_number == admission_number,
            ).first()

        if existing_record:
            # 合并到已有记录（出院记录补充入院记录）
            record = existing_record
        else:
            record = HospitalRecord(case_id=case.id, material_id=material.id)
            db.add(record)
            db.flush()

        # 只更新有值的字段
        field_map = {
            "hospital_name": "hospital_name",
            "admission_number": "admission_number",
            "chief_complaint": "chief_complaint",
            "present_illness_history": "present_illness_history",
            "past_history": "past_history",
            "physical_examination": "physical_examination",
            "admission_diagnosis": "admission_diagnosis",
            "treatment_process": "treatment_process",
            "medication": "medication",
            "discharge_diagnosis": "discharge_diagnosis",
            "discharge_orders": "discharge_orders",
            "admission_date": "admission_date",
            "discharge_date": "discharge_date",
        }

        for json_key, db_key in field_map.items():
            value = fields.get(json_key)
            if value is not None and value != "" and value != "null":
                setattr(record, db_key, value)

        # 住院天数特殊处理
        if fields.get("hospital_days") is not None:
            try:
                record.hospital_days = int(fields["hospital_days"])
            except (ValueError, TypeError):
                pass

        saved_info = {
            "hospital_name": fields.get("hospital_name"),
            "admission_number": admission_number,
            "record_id": record.id,
        }

    elif material_type == MaterialType.IMAGING_REPORT:
        # 影像学报告 → 查找已有记录（同 material_id），有则覆盖，无则新建
        report = db.query(ImagingReport).filter(
            ImagingReport.case_id == case.id,
            ImagingReport.material_id == material.id,
        ).first()

        if not report:
            report = ImagingReport(case_id=case.id, material_id=material.id)
            db.add(report)
            db.flush()

        field_map = {
            "report_date": "report_date",
            "hospital_name": "hospital_name",
            "exam_type": "exam_type",
            "exam_part": "exam_part",
            "film_number": "film_number",
            "report_content": "report_content",
        }

        for json_key, db_key in field_map.items():
            value = fields.get(json_key)
            if value is not None and value != "" and value != "null":
                setattr(report, db_key, value)

        # film_count 特殊处理（字符串→整数）
        if fields.get("film_count") is not None:
            try:
                report.film_count = int(fields["film_count"])
            except (ValueError, TypeError):
                report.film_count = 1
        else:
            report.film_count = 1

        saved_info = {
            "hospital_name": fields.get("hospital_name"),
            "exam_type": fields.get("exam_type"),
            "report_id": report.id,
        }

    elif material_type == MaterialType.APPRAISAL_APPLICATION:
        # 鉴定申请书 → 补充委托单位（委托书优先，申请书可补充）
        # 注意：委托事项优先从「司法鉴定委托书」的鉴定要求提取；申请书仅在委托书缺失时兜底。
        case_confirmed = _get_confirmed_fields(case)
        person_name = _get_person_name(case, db, fields.get("person_name") or "")
        if fields.get("entrusting_unit"):
            _safe_setattr(case, "entrusting_unit", fields["entrusting_unit"], case_confirmed)

        fallback_items = fields.get("appraisal_items") or _extract_appraisal_application_items(ocr_text)
        if fallback_items and not case.entrustment_matter:
            _safe_setattr(
                case,
                "entrustment_matter",
                _normalize_entrustment_matter(fallback_items, person_name),
                case_confirmed,
            )
        saved_info = {
            "applicant": fields.get("applicant"),
            "entrusting_unit": fields.get("entrusting_unit"),
            "appraisal_items": fields.get("appraisal_items"),
            "case_brief": fields.get("case_brief"),
        }

    db.commit()

    return {
        "material_id": material.id,
        "filename": material.original_filename,
        "material_type": material_type,
        "status": "completed",
        "fields": fields,
        "saved_info": saved_info,
        "model": result.get("model", ""),
        "usage": result.get("usage", {}),
    }


# ==================== 辅助生成函数 ====================

# ==================== 辅助函数 ====================

def _extract_medical_group_llm(hospital_name: str, combined_text: str) -> dict:
    """调用LLM按医院分组提取病历字段"""
    return extract_medical_group_fields(hospital_name, combined_text)


def _generate_material_list(case_id: int, case: Case, db) -> None:
    """
    根据案件已上传的所有材料，自动生成材料清单文本
    规则：
    1. 每类材料默认"壹份"，不按图片张数算
    2. 数字用大写（壹、贰、叁...）
    3. 委托书=原件，其他=复印件（影像除外）
    4. 身份证格式：XXX居民身份证复印件壹份
    5. 影像学资料：被鉴定人带胶片给法医查看，从ImagingReport读exam_type+film_count，
       如"CT贰张、MRI叁张"，不写原件/复印件
    """
    all_materials = db.query(Material).filter(
        Material.case_id == case_id,
    ).all()

    if not all_materials:
        return

    # 大写数字映射
    num_cn = {0: "零", 1: "壹", 2: "贰", 3: "叁", 4: "肆", 5: "伍",
              6: "陆", 7: "柒", 8: "捌", 9: "玖", 10: "拾"}

    def to_cn(n: int) -> str:
        if n in num_cn:
            return num_cn[n]
        return str(n)

    # 按材料类型分组统计（是否有上传即可，不按图片数）
    uploaded_types = set()
    for mat in all_materials:
        uploaded_types.add(mat.material_type)

    # 获取被鉴定人姓名
    person = db.query(Person).filter(Person.case_id == case_id).first()
    person_name = person.name if person and person.name else ""

    # 获取住院记录（用于按医院列病历）
    hospital_records = db.query(HospitalRecord).filter(
        HospitalRecord.case_id == case_id,
    ).all()

    # 获取影像学报告（用于列影像资料详情）
    imaging_reports = db.query(ImagingReport).filter(
        ImagingReport.case_id == case_id,
    ).all()

    # 生成清单
    list_items = []
    idx = 1

    litigation_materials = [
        m for m in all_materials
        if m.material_type == MaterialType.LITIGATION_MATERIAL
    ]
    litigation_text = " ".join(
        filter(None, [
            *(m.description or "" for m in litigation_materials),
            *(m.original_filename or "" for m in litigation_materials),
            *(m.ocr_text or "" for m in litigation_materials),
        ])
    )
    has_civil_complaint = "民事起诉状" in litigation_text

    # 1. 司法鉴定委托书（原件）
    if MaterialType.ENTRUSTMENT_LETTER in uploaded_types:
        list_items.append(f"{idx}. 司法鉴定委托书原件壹份")
        idx += 1

    # 2. 身份证复印件
    if MaterialType.ID_CARD in uploaded_types:
        name_prefix = f"{person_name}" if person_name else ""
        list_items.append(f"{idx}. {name_prefix}居民身份证复印件壹份")
        idx += 1

    # 3. 交通事故认定书复印件
    if MaterialType.TRAFFIC_ACCIDENT_CERT in uploaded_types:
        list_items.append(f"{idx}. 道路交通事故认定书复印件壹份")
        idx += 1

    # 4. 鉴定申请书与民事起诉状通常合并列入同一项
    has_appraisal_application = MaterialType.APPRAISAL_APPLICATION in uploaded_types
    if has_appraisal_application and has_civil_complaint:
        list_items.append(f"{idx}. 民事起诉状、鉴定申请书复印件各壹份")
        idx += 1
    elif has_appraisal_application:
        list_items.append(f"{idx}. 鉴定申请书复印件壹份")
        idx += 1
    elif has_civil_complaint:
        list_items.append(f"{idx}. 民事起诉状复印件壹份")
        idx += 1

    # 5. 其他诉讼材料，作为背景材料单独列入清单
    if MaterialType.LITIGATION_MATERIAL in uploaded_types and not has_civil_complaint:
        list_items.append(f"{idx}. 诉讼材料复印件壹份")
        idx += 1

    # 6. 住院病历复印件（按医院分组合并，同一家医院多次住院合并为一条）
    if hospital_records:
        # 按医院名分组统计住院次数
        hospital_count = {}
        for r in hospital_records:
            hospital = r.hospital_name or "某医院"
            hospital_count[hospital] = hospital_count.get(hospital, 0) + 1
        for hospital, count in hospital_count.items():
            count_str = to_cn(count)
            list_items.append(f"{idx}. {hospital}住院病历复印件{count_str}份")
            idx += 1
    elif MaterialType.MEDICAL_RECORD in uploaded_types:
        # 有上传但还没提取出住院记录
        list_items.append(f"{idx}. 住院病历复印件壹份")
        idx += 1

    # 7. 影像学资料（被鉴定人带胶片给法医查看，按检查类型+张数列，不写原件/复印件）
    if imaging_reports:
        # 按exam_type汇总张数
        exam_type_counts = {}
        for r in imaging_reports:
            et = r.exam_type or "影像学检查"
            fc = r.film_count or 1
            exam_type_counts[et] = exam_type_counts.get(et, 0) + fc

        # 按医院分组
        hospital_exams = {}
        for r in imaging_reports:
            hospital = r.hospital_name or "某医院"
            if hospital not in hospital_exams:
                hospital_exams[hospital] = {}
            et = r.exam_type or "影像学检查"
            fc = r.film_count or 1
            hospital_exams[hospital][et] = hospital_exams[hospital].get(et, 0) + fc

        # 如果只有一个医院或没医院名，直接列检查类型
        if len(hospital_exams) == 1:
            hospital = list(hospital_exams.keys())[0]
            exams = hospital_exams[hospital]
            exam_parts = []
            for et, count in exams.items():
                exam_parts.append(f"{et}{to_cn(count)}张")
            exam_str = "、".join(exam_parts)
            if hospital and hospital != "某医院":
                list_items.append(f"{idx}. {hospital}{exam_str}")
            else:
                list_items.append(f"{idx}. 影像学资料{exam_str}")
            idx += 1
        else:
            # 多个医院，每个医院一行
            for hospital, exams in hospital_exams.items():
                exam_parts = []
                for et, count in exams.items():
                    exam_parts.append(f"{et}{to_cn(count)}张")
                exam_str = "、".join(exam_parts)
                list_items.append(f"{idx}. {hospital}{exam_str}")
                idx += 1
    elif MaterialType.IMAGING_REPORT in uploaded_types:
        # 有上传但还没提取出影像报告，暂列占位
        list_items.append(f"{idx}. 影像学资料（待提取详情）")
        idx += 1

    material_list_text = "；".join(list_items) + "。"

    case.material_list = material_list_text
    db.commit()
    logger.info(f"案件 {case_id} 材料清单已自动生成: {material_list_text}")


def _generate_material_summary(case_id: int, db) -> dict:
    """从住院记录生成资料摘要全文（拼接方式，不调用LLM）"""
    records = db.query(HospitalRecord).filter(HospitalRecord.case_id == case_id).all()
    if not records:
        return {"success": False, "error": "没有住院记录"}

    # 中文序号列表
    cn_nums = ["（一）", "（二）", "（三）", "（四）", "（五）", "（六）", "（七）", "（八）", "（九）", "（十）"]

    # 拼接住院记录信息
    record_parts = []
    for idx, r in enumerate(records):
        prefix = cn_nums[idx] if idx < len(cn_nums) else f"（{idx + 1}）"
        part = f"{prefix}据{r.hospital_name or '某医院'}住院病案（住院号：{r.admission_number or '-'})记载："
        if r.admission_date:
            part += f"患者{r.admission_date}入院，"
        if r.discharge_date:
            part += f"{r.discharge_date}出院，"
        if r.hospital_days:
            part += f"住院{r.hospital_days}天。"
        if r.chief_complaint:
            part += f"主诉：{r.chief_complaint}。"
        if r.present_illness_history:
            part += f"现病史：{r.present_illness_history}。"
        if r.past_history:
            part += f"既往史：{r.past_history}。"
        if r.physical_examination:
            part += f"体格检查：{r.physical_examination}。"
        if r.admission_diagnosis:
            part += f"入院诊断：{r.admission_diagnosis}。"
        if r.treatment_process:
            part += f"治疗过程：{r.treatment_process}。"
        if r.medication:
            part += f"用药情况：{r.medication}。"
        if r.discharge_diagnosis:
            part += f"出院诊断：{r.discharge_diagnosis}。"
        if r.discharge_orders:
            part += f"出院医嘱：{r.discharge_orders}。"
        record_parts.append(part)

    summary_text = "\n\n".join(record_parts)
    logger.info(f"案件 {case_id} 资料摘要拼接完成，长度: {len(summary_text)} 字符，{len(records)} 条记录")

    return {"success": True, "material_summary": summary_text}


def _generate_imaging_review(imaging_data: list[dict], person_name: str,
                              entrustment: str, summary_context: str) -> str:
    """一次 LLM 调用：根据委托事项和资料摘要，从检查事实中选相关条目，写法医风格影像复阅"""
    if not imaging_data:
        return ""

    facts = []
    for r in imaging_data:
        facts.append({
            "日期": r.get("报告日期", ""),
            "医院": r.get("医院名称", ""),
            "部位": r.get("检查部位", ""),
            "类型": r.get("检查类型", ""),
            "片子编号": r.get("片子编号", ""),
            "报告内容": (r.get("报告内容") or "")[:200],
        })

    system_prompt = """你是法医临床学鉴定人。根据委托事项和资料摘要，从检查事实中选出相关条目，以真实司法鉴定意见书影像复阅的写法逐条输出。

以下是真实鉴定意见书"鉴定过程"中影像复阅部分的写法，请严格参照这种格式和语言风格：

【参考样本A——颅脑损伤案件】
复阅2021年1月7日淇县人民医院杨豫臻颅脑CT平扫片（号CT011566）示：左额部颅骨内板下可见新月形高密度影，双侧额叶可见小片状高密度影，周围见低密度影。大脑纵裂池可见高密度影，余脑实质内未见明显异常密度影。脑室系统未见扩大，脑沟、裂、池未见增宽、加深，中线结构居中。后顶部软组织显示肿胀，其内可见积气影，额骨及顶骨见线样低密度影。
复阅2021年1月8日淇县人民医院杨豫臻颅脑CT平扫片（号CT011638）示：左额部颅骨内板下可见新月形高密度影，双侧额叶及右侧颞叶可见小片状高密度影，周围可见低密度影。大脑纵裂池可见高密度影，余脑实质内未见明显异常密度影。脑室系统未见扩大，脑沟、裂、池未见增宽、加深，中线结构居中。
复阅2021年3月8日新乡医学院第一附属医院杨豫臻颅脑CT平扫片（号CT676078）示：颅脑呈术后改变，两侧额部局部骨质缺如，双侧额叶、右侧颞叶见大片状低密度影，边界清晰，余脑实质内未见明显异常密度灶。脑室系统大小、形态如常；脑沟、裂不宽，中线结构居中。
复阅2021年10月25日新乡医学院第三附属医院杨豫臻颅脑CT平扫片（号CT1272459）示：额骨见透亮线及内固定影，局部骨质局限性缺损，右额部头皮局限性增厚，呈术后改变；双侧额叶、颞叶见大片状低密度影，边界清晰，余脑实质内未见明显异常密度灶。

【参考样本B——多发骨折案件】
复阅2021年4月16日新乡县龙华医院张金遂头颅、胸部CT片（号CT212122）示：右侧额叶见斑点状高密度影，余脑实质未见明显异常密度影，脑沟裂结构未见明显异常，双侧侧脑室后角见高密度影，中线结构居中。左侧锁骨中段见骨质连续性中断，断端错位，并可见碎骨块游离。
复阅2021年4月29日陆军第八十三集团军医院张金遂左锁骨正位X线片（号90711866）示：左锁骨内固定牢固在位，骨折断端对位对线良好，骨折线模糊。
复阅2021年8月25日新乡医学院第三附属医院张金遂头颅MRI片（号MR258260）示：右侧额叶见条状长T1长T2信号，FLAIR像呈低信号，周围呈高信号，边界清楚。右侧额叶见斑点状低信号影。印象：右侧额叶软化灶，周围多发含铁血黄素沉积。

注意：每条1-2句只写阳性发现，不写"影像所见""影像诊断"等标签和医师姓名。不相关的检查跳过不写。法医鉴定前最近复查的片子反映了当前状态，必须保留。不要输出思考过程，直接输出复阅行。"""

    prompt = f"""【委托事项】{entrustment}
【被鉴定人】{person_name}
【资料摘要】{summary_context}

检查事实：
{json.dumps(facts, ensure_ascii=False, indent=2)}

逐条输出影像复阅，每条一行，按时间顺序排列。"""
    result = call_llm_text_harness(
        task_name="generate_imaging_review",
        system_prompt=system_prompt,
        instructions=prompt,
        temperature=0.0,
        max_tokens=4000,
        max_input_chars=12000,
        max_retries=1,
    )
    if result.get("success"):
        content = result["content"].strip()
        lines = [ln.strip() for ln in content.split("\n") if ln.strip().startswith("复阅")]
        if lines:
            return "\n".join(lines)
    # Fallback
    lines = []
    for r in imaging_data:
        prefix = f"复阅{r.get('报告日期','')}{r.get('医院名称','')}{person_name}{r.get('检查部位','')}{r.get('检查类型','')}片"
        if r.get("片子编号"):
            prefix += f"（号{r['片子编号']}）"
        prefix += "示："
        lines.append(f"{prefix}{(r.get('报告内容') or '')[:150]}。")
    return "\n".join(lines)


def _generate_appraisal_process(case_id: int, db) -> dict:
    """生成鉴定过程：临床检查医生写了直接用，没写LLM根据最新影像推断；
    影像复阅一次LLM调用，根据委托事项+资料摘要从检查事实中选相关条目并写法医风格。"""
    img_reports = db.query(ImagingReport).filter(ImagingReport.case_id == case_id).all()
    case = db.query(Case).filter(Case.id == case_id).first()
    person = db.query(Person).filter(Person.case_id == case_id).first()
    records = db.query(HospitalRecord).filter(HospitalRecord.case_id == case_id).all()

    if not case:
        return {"success": False, "error": "案件不存在"}

    person_name = person.name if person and person.name else "被鉴定人"
    exam_date = case.examination_date or ""
    clinical_examination = case.clinical_examination or ""
    accident_date = case.accident_date or ""
    entrustment = case.entrustment_matter or ""

    # 资料摘要缩编（出院诊断 + 关键手术经过，600字以内）
    summary_parts = []
    for r in records:
        if r.discharge_diagnosis:
            summary_parts.append(f"【{r.hospital_name or ''}出院诊断】{r.discharge_diagnosis[:200]}")
        if r.treatment_process and any(kw in (r.treatment_process or "") for kw in
                                       ("手术", "探查", "切除", "修补", "置换", "内固定")):
            summary_parts.append(f"【{r.hospital_name or ''}手术经过】{(r.treatment_process or '')[:300]}")
    summary_context = "\n".join(summary_parts)[:600]

    # 构建影像数据（粗筛去血管超声/肌电/鼻咽喉镜/未完成审核/纯正常）
    _SKIP_TYPES = {"emg_report", "nasopharyngoscopy", "hearing_test", "eeg", "nerve_conduction"}
    _VASCULAR_KW = ("静脉", "动脉", "动静脉", "血栓", "深静脉", "肌间静脉", "腓静脉")
    _POSITIVE_KW = ("骨折", "损伤", "出血", "血肿", "断裂", "置换", "术后", "内固定", "缺如", "缺失")
    _PENDING_KW = ("未完成审核", "尚未上传", "尚未完成", "待审核", "未审核")
    _NORMAL_KW = ("未见明显异常", "未见异常", "未见明确")

    imaging_data = []
    for r in img_reports:
        rtype = (r.exam_type or "").lower().strip()
        ep = (r.exam_part or "").lower()
        rc = (r.report_content or "").lower()
        if rtype in _SKIP_TYPES:
            continue
        if any(m in ep for m in _VASCULAR_KW) or any(m in rc[:120] for m in _VASCULAR_KW):
            continue
        if any(m in rtype for m in ("超声", "彩超", "ultrasound")) and \
           not any(m in ep for m in ("肝", "胆", "脾", "胰", "肾", "腹", "心脏", "心")):
            continue
        if any(m in rc for m in _PENDING_KW) and not any(m in rc for m in _POSITIVE_KW):
            continue
        if any(m in rc for m in _NORMAL_KW) and not any(m in rc for m in _POSITIVE_KW):
            continue
        imaging_data.append({
            "报告日期": r.report_date or "",
            "医院名称": r.hospital_name or "",
            "检查类型": r.exam_type or "",
            "检查部位": r.exam_part or "",
            "片子编号": r.film_number or "",
            "报告内容": r.report_content or "",
        })

    # 伤后时间
    from datetime import datetime as dt
    time_since = ""
    if accident_date and exam_date:
        try:
            d1 = dt.strptime(accident_date[:10], "%Y-%m-%d" if "-" in accident_date[:10] else "%Y年%m月%d日")
            d2 = dt.strptime(exam_date[:10], "%Y-%m-%d" if "-" in exam_date[:10] else "%Y年%m月%d日")
            delta = (d2 - d1).days
            if delta >= 0:
                if delta < 30:
                    time_since = f"伤后{delta}日"
                else:
                    months = delta // 30
                    time_since = f"伤后{months}个月余" if delta % 30 < 25 else f"伤后{months + 1}个月"
        except (ValueError, IndexError):
            pass

    has_imaging = len(imaging_data) > 0
    has_clinical = bool(clinical_examination.strip())

    opening_standard = (
        "按照《法医临床检验规范》（SF/Z JD0103003-2011）和《法医临床影像学检验实施规范》（SF/Z JD0103006-2014）"
        if has_imaging else
        "按照《法医临床检验规范》（SF/Z JD0103003-2011）"
    )
    opening = f"{opening_standard}对{person_name}进行法医临床学检查。"

    display_date = exam_date
    if exam_date and "-" in exam_date:
        parts_date = exam_date.split(" ")[0].split("-")
        if len(parts_date) == 3:
            display_date = f"{parts_date[0]}年{parts_date[1]}月{parts_date[2]}日"
    date_line = ""
    if display_date or time_since:
        date_part = f"{display_date}，" if display_date else ""
        time_part = f"即被鉴定人{person_name}{time_since}。" if time_since else ""
        date_line = f"{date_part}{time_part}"

    # 影像复阅
    imaging_review = _generate_imaging_review(imaging_data, person_name, entrustment, summary_context)

    # 临床检查
    if has_clinical:
        clinical_text = clinical_examination.strip()
    else:
        clinical_text = ""
        if imaging_data:
            # 找到最近日期的检查（通常是法医鉴定前专门做的）
            latest = max(imaging_data, key=lambda x: x.get("报告日期", ""))
            prompt = f"""你是法医临床学鉴定人。请根据以下信息撰写鉴定过程里的临床检查部分。

被鉴定人：{person_name}，检查日期：{display_date}，伤后时间：{time_since}

法医鉴定前最近日期的复查结果（据此推断当前查体所见）：
{latest.get('医院名称','')} {latest.get('检查部位','')}{latest.get('检查类型','')}: {(latest.get('报告内容') or '')[:300]}

此前已知的损伤和治疗概况：
{summary_context[:400]}

要求：写"{person_name}自行步入诊室。神志清晰，查体合作。"写"自诉："后1-2句核心症状。写"查体："后5-6句简述手术瘢痕、畸形、压痛、活动受限等阳性体征，不写关节度数，不写特殊试验名称。以"四肢肌力及肌张力正常，余查体未见明显异常。"收尾。连续行文，只输出临床检查文本。"""
            result = call_llm_text_harness(
                task_name="clinical_exam",
                system_prompt="你是法医临床学鉴定人。只输出临床检查文本。",
                instructions=prompt,
                temperature=0.0,
                max_tokens=1500,
                max_input_chars=6000,
                max_retries=1,
            )
            if result.get("success"):
                clinical_text = result["content"].strip()

    # 组装
    parts = [opening]
    if date_line:
        parts.append(date_line)
    if clinical_text:
        parts.append(clinical_text)
    if imaging_review:
        parts.append(imaging_review)
    process_text = "\n\n".join(parts)

    logger.info(f"案件 {case_id} 鉴定过程生成成功（影像 {len(img_reports)}→{len(imaging_data)} 条）")
    return {"success": True, "appraisal_process": process_text, "method": "llm"}


def _select_analysis_standards(case_id: int, db, entrustment: str, diagnoses_text: str) -> list[dict]:
    """三层检索：规则映射 → LLM 选条款 → 精准检索，为分析说明找到对口的标准条款"""
    items = _parse_entrustment_items(entrustment)
    item_to_docs = _map_entrustment_to_standard_ids(items, db)
    all_doc_ids = set().union(*item_to_docs.values())
    if not all_doc_ids:
        return []

    # 构建标准目录供 LLM 导航
    toc_docs = build_standard_toc(db, all_doc_ids)
    toc_compact = []
    for doc in toc_docs:
        sections_compact = [f"{s['code']} {s['title']}".strip() for s in doc["sections"] if (s["code"] or s["title"])]
        toc_compact.append({"standard_name": doc["standard_name"], "sections": sections_compact[:60]})
    toc_text = "\n".join(f"【{d['standard_name']}】\n" + "\n".join(f"  · {s}" for s in d["sections"]) for d in toc_compact)

    prompt = f"""你是法医临床学鉴定人。根据案件信息和委托事项，从标准目录中选出应引用的条款。

委托事项：{entrustment}
关键诊断（出院诊断/手术证实）：{diagnoses_text[:600]}

标准目录：
{toc_text}

返回JSON，每个委托事项选出最相关的条款标题和检索关键词。条款标题必须从目录中选择，不可编造。"""

    result = call_llm_json_harness(
        task_name="select_analysis_standards",
        system_prompt="你是法医临床学鉴定人。只输出JSON。",
        instructions=prompt,
        input_text=toc_text,
        output_schema='{"selections": [{"entrustment_item": "伤残等级", "relevant_sections": ["条款标题"], "keywords": "检索词"}]}',
        required_fields=["selections"],
        temperature=0.0,
        max_tokens=2000,
        max_input_chars=8000,
        max_retries=1,
    )

    selections = result.get("data", {}).get("selections", []) if result.get("success") else []
    all_keywords = " ".join(s.get("keywords", "") for s in selections)
    section_codes = set()
    for s in selections:
        for section in s.get("relevant_sections", []):
            m = re.match(r"^([\d.]+)", str(section))
            if m:
                section_codes.add(m.group(1))

    if all_keywords and all_doc_ids:
        refs = search_clauses_in_documents(db, all_doc_ids, all_keywords)
        if section_codes:
            extra = db.query(StandardChunk).filter(
                StandardChunk.document_id.in_(list(all_doc_ids)),
                StandardChunk.section_code.in_(list(section_codes)),
            ).all()
            extra_ids = {r["id"] for r in refs}
            for chunk in extra:
                if chunk.id not in extra_ids:
                    refs.append(chunk_to_reference(chunk, 10.0))
            refs.sort(key=lambda r: r.get("score", 0), reverse=True)
        return refs
    return []


def _analysis_entrustment_flags(entrustment: str) -> dict[str, bool]:
    text = entrustment or ""
    return {
        "disability": any(term in text for term in ("伤残", "残疾", "致残", "伤残等级")),
        "work_loss": "误工" in text,
        "nursing": "护理" in text and "护理依赖" not in text,
        "nutrition": "营养" in text,
        "nursing_dependency": "护理依赖" in text,
        "followup_cost": any(term in text for term in ("后续治疗", "后期治疗", "医疗费", "治疗费")),
        "causation": any(term in text for term in ("因果", "参与度", "关系")),
    }


def _reference_identity(ref: dict) -> tuple:
    text = re.sub(r"\s+", "", ref.get("text") or ref.get("snippet") or "")
    return (
        ref.get("id"),
        ref.get("standard_name") or "",
        ref.get("section_code") or "",
        ref.get("section_title") or "",
        text[:80],
    )


def _classify_analysis_references(refs: list[dict], entrustment: str, context: str) -> list[dict]:
    """把规范检索结果分为拟引用、辅助判断和低置信候选，避免把候选依据当成正式依据。"""
    flags = _analysis_entrustment_flags(entrustment)
    context_text = f"{entrustment}\n{context}"
    seen = set()
    grouped: dict[str, list[dict]] = {"primary": [], "supporting": [], "candidate": []}

    for ref in refs:
        ident = _reference_identity(ref)
        if ident in seen:
            continue
        seen.add(ident)

        doc = ref.get("standard_name") or ""
        section = " ".join(filter(None, [ref.get("section_code"), ref.get("section_title")]))
        text = f"{doc}\n{section}\n{ref.get('text') or ref.get('snippet') or ''}"
        role = "candidate"
        reason = "关键词命中，需人工判断是否采信"

        if "人体损伤致残程度分级" in doc and flags["disability"]:
            role = "primary"
            reason = "委托事项包含伤残等级，可作为伤残结论拟引用依据"
        elif "误工期" in doc and any(flags[k] for k in ("work_loss", "nursing", "nutrition")):
            role = "primary"
            reason = "委托事项包含误工期、护理期或营养期，可作为三期结论拟引用依据"
        elif "护理依赖" in doc and flags["nursing_dependency"]:
            role = "primary"
            reason = "委托事项包含护理依赖，可作为护理依赖结论拟引用依据"
        elif "因果关系" in doc and flags["causation"]:
            role = "primary"
            reason = "委托事项包含因果关系，可作为因果关系分析拟引用依据"
        elif any(name in doc for name in ("法医影像学", "影像学检验", "法医临床检验")):
            role = "supporting"
            reason = "用于影像复核或检验过程判断，不直接作为伤残/三期结论条款"
        elif any(name in doc for name in ("听觉功能障碍", "视觉功能障碍")):
            if any(term in context_text for term in ("听觉", "听力", "耳", "视觉", "视力", "眼")):
                role = "supporting"
                reason = "与专科功能障碍相关，可作辅助判断"
            else:
                reason = "专科功能障碍候选，与当前委托事项关联较弱"

        annotated = dict(ref)
        annotated["reference_role"] = role
        annotated["reference_role_label"] = {
            "primary": "拟引用依据",
            "supporting": "辅助判断依据",
            "candidate": "低置信候选依据",
        }[role]
        annotated["match_reason"] = reason
        annotated["display_section"] = section
        grouped[role].append(annotated)

    limits = {"primary": 10, "supporting": 5, "candidate": 4}
    ordered: list[dict] = []
    for role in ("primary", "supporting", "candidate"):
        rows = sorted(grouped[role], key=lambda r: r.get("score", 0), reverse=True)
        ordered.extend(rows[:limits[role]])
    return ordered


def _format_analysis_reference_context(refs: list[dict]) -> str:
    primary = [r for r in refs if r.get("reference_role") == "primary"]
    supporting = [r for r in refs if r.get("reference_role") == "supporting"]
    candidate = [r for r in refs if r.get("reference_role") == "candidate"]

    parts = [
        "【拟引用规范依据】（正式结论只能引用本栏条款，不得引用未列出的条款号）",
        format_selected_clauses(primary, max_chars=4200) if primary else "未检索到拟引用条款，正式结论应采用审慎表述。",
    ]
    if supporting:
        parts.extend([
            "\n【辅助判断依据】（只用于影像复核、事实判断，不得直接写成伤残等级或三期条款依据）",
            format_selected_clauses(supporting, max_chars=1600),
        ])
    if candidate:
        parts.extend([
            "\n【低置信候选依据】（仅提示人工核对，正文不得直接引用）",
            format_selected_clauses(candidate, max_chars=900),
        ])
    return "\n".join(parts)


def _merge_analysis_references(*groups: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[str] = set()
    for group in groups:
        for ref in group or []:
            key = str(ref.get("clause_key") or ref.get("id") or ref.get("clause_label") or ref.get("snippet"))
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(ref)
    return merged


def _clean_generated_report_text(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"<think\b[^>]*>.*?</think>", "", text, flags=re.S | re.I).strip()
    if re.search(r"<think\b", text, flags=re.I):
        final_match = re.search(r"(根据委托单位提供的现有材料|1\.\s*被鉴定人)", text)
        text = text[final_match.start():].strip() if final_match else ""
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    text = re.sub(r"(?m)^\s*[-*]\s+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _remove_uncommissioned_analysis(text: str, entrustment: str) -> tuple[str, list[str]]:
    flags = _analysis_entrustment_flags(entrustment)
    warnings: list[str] = []
    if flags["followup_cost"]:
        return text, warnings

    followup_terms = ("后续治疗费", "后续治疗费用", "后期医疗费用", "后期治疗费", "医疗费用", "后续治疗项目")
    if not any(term in text for term in followup_terms):
        return text, warnings

    blocks = re.split(r"(?m)(?=^\s*\d+\.\s*)", text)
    kept = []
    removed = 0
    for block in blocks:
        stripped = block.strip()
        if not stripped:
            continue
        number_match = re.match(r"^(\d+)\.", stripped)
        if number_match and int(number_match.group(1)) >= 3 and any(term in stripped for term in followup_terms):
            removed += 1
            continue
        kept.append(block.strip())
    cleaned = "\n\n".join(kept).strip() if kept else text
    if removed:
        warnings.append("已移除未在委托事项中的后续治疗费/后期医疗费用段落。")
    return cleaned, warnings


def _generate_analysis(case_id: int, db) -> dict:
    """使用 LLM 生成分析说明：三层智能检索 + 真实鉴定意见书格式"""
    case = db.query(Case).filter(Case.id == case_id).first()
    records = db.query(HospitalRecord).filter(HospitalRecord.case_id == case_id).all()
    img_reports = db.query(ImagingReport).filter(ImagingReport.case_id == case_id).all()
    person = db.query(Person).filter(Person.case_id == case_id).first()

    if not case:
        return {"success": False, "error": "案件不存在"}

    person_name = person.name if person and person.name else "被鉴定人"
    entrustment = case.entrustment_matter or ""

    hospital_data = []
    for r in records:
        hospital_data.append({
            "医院名称": r.hospital_name or "", "住院号": r.admission_number or "",
            "入院日期": r.admission_date or "", "出院日期": r.discharge_date or "",
            "住院天数": r.hospital_days or "", "入院诊断": r.admission_diagnosis or "",
            "出院诊断": r.discharge_diagnosis or "", "治疗经过": r.treatment_process or "",
            "出院医嘱": r.discharge_orders or "",
        })

    imaging_data = []
    for r in img_reports:
        imaging_data.append({
            "报告日期": r.report_date or "", "医院名称": r.hospital_name or "",
            "检查类型": r.exam_type or "", "检查部位": r.exam_part or "",
            "片子编号": r.film_number or "", "报告内容": r.report_content or "",
        })

    # 分析说明护栏：先形成案件事实、伤残候选和精准条款，再交给 LLM 写正文
    harness_payload = sync_analysis_candidates(case_id, db)
    harness_context = format_analysis_harness_for_prompt(harness_payload)

    # 三层智能检索（作为补充，不再直接支配伤残候选）
    diagnoses_parts = [f"【{r.hospital_name or ''}出院诊断】{r.discharge_diagnosis}" for r in records if r.discharge_diagnosis]
    for r in records:
        if r.treatment_process and any(kw in (r.treatment_process or "") for kw in ("手术", "探查", "修补", "置换", "内固定")):
            diagnoses_parts.append(f"【{r.hospital_name or ''}手术经过】{(r.treatment_process or '')[:300]}")
    diagnoses_text = "\n".join(diagnoses_parts)

    raw_standard_refs = _select_analysis_standards(case_id, db, entrustment, diagnoses_text)
    supplemental_refs = _classify_analysis_references(raw_standard_refs, entrustment, diagnoses_text)
    standard_refs = _merge_analysis_references(harness_payload.get("standard_references", []), supplemental_refs)
    standard_context = _format_analysis_reference_context(standard_refs) if standard_refs else "未检索到可用规范依据。"
    entrusted_items = "、".join(_parse_entrustment_items(entrustment)) or "未明确"

    # LLM 生成
    system_prompt = """你是法医临床学鉴定人。请参照真实司法鉴定意见书"分析说明"的写法，撰写分析说明。

以下是一个真实案例的分析说明写法，请参照其结构和语言风格：

案件A——颅脑损伤：
1. 被鉴定人杨豫臻外伤史明确，系交通事故所致。
2. 被鉴定人杨豫臻外伤致闭合性颅脑损伤（多发脑挫裂伤、头皮血肿、头皮裂伤）等诊断明确（经CT、临床检查和手术等证实）。
3. 被鉴定人杨豫臻颅脑损伤较重...现其伤后9个月余，自诉烦躁、头晕...有鉴于此，参照《人体损伤致残程度分级》第5.9.1条3）项（脑叶部分切除术后）之规定，被鉴定人杨豫臻颅脑损伤致脑叶部分切除术后应评为九级伤残。
4. 被鉴定人杨豫臻，女，32岁。2021年1月7日外伤致闭合性颅脑损伤...有鉴于此，参照中华人民共和国公共安全行业标准（GA/T 1193-2014）《人身损害误工期、护理期、营养期评定规范》...结合其年龄、自身情况、损伤情况、临床治疗、伤情恢复实际状况，其误工期拟定至伤残评定前一日；其住院期间护理人数建议为2人；其出院后护理期拟定为90日、护理人数拟定为1人。

案件B——多发骨折：
1. 被鉴定人张金遂外伤史确切，系道路交通事故所致。
2. 被鉴定人张金遂外伤致左锁骨中段粉碎性骨折、右肩胛骨骨折、右侧额叶脑挫裂伤等诊断明确（经X线、CT、临床检查及手术证实）。
3. 被鉴定人张金遂左锁骨中段粉碎性骨折、右肩胛骨骨折，经临床积极行"左锁骨骨折切开复位内固定术"及对症治疗，伤情稳定恢复。其右侧额叶脑挫裂伤经临床对症治疗，伤情稳定。复阅其2021年8月25日头颅MRI片提示右侧额叶软化灶，周围多发含铁血黄素沉积。有鉴于此，参照《人体损伤致残程度分级》第5.10.1条2）项（颅脑损伤后遗脑软化灶形成，伴有神经系统症状或者体征）之规定，被鉴定人张金遂右侧额叶脑挫裂伤后遗脑软化灶形成应评为十级伤残。
4. 被鉴定人张金遂，男，现年54岁，2021年4月16日外伤致左锁骨中段粉碎性骨折等，经临床积极行"左锁骨骨折切开复位内固定术"及对症治疗，于2021年6月15日伤情稳定出院。因此，根据其损伤情况，结合其年龄、自身情况、临床治疗经过、目前恢复情况，参照中华人民共和国公共安全行业标准(GA/T 1193-2014)《人身损害误工期、护理期、营养期评定规范》有关规定，其出院后的误工期拟定为60日。
5. 被鉴定人张金遂目前左锁骨骨折处内固定物在位，日后还需二次手术取出上述内固定物。手术费用构成一般包括入院检查检验费、麻醉费、手术费、住院费、相关预防感染药物及输液、消肿、止血等药物费。参考河南省地市级三级医院收费情况，后续治疗费用约需人民币捌仟圆（¥8000.0元）。

要求：
- 开头固定："根据委托单位提供的现有材料，结合本鉴定中心检验所见，现分析如下："
- 第1条确认外伤来源，第2条确认所有伤情诊断
- 第3条起只逐一分析【委托鉴定事项】列明的项目，不得增加未委托事项
- 必须先遵守【分析说明护栏】；案件基础事实以护栏为准，不得套用示例中的姓名、性别、年龄
- 伤残等级只能对护栏中 status=accepted 且 decision=met 的候选写正式结论；needs_review/pending/excluded 候选只能写为需核对或辅助事实，不得直接定级
- 必须覆盖所有 status=accepted 且 decision=met 的候选；不得把未被采信的候选写成正式伤残结论
- 条款号和条款内容必须优先使用护栏给出的规范；不得把髋关节置换误写成其他不相干条款
- 引用条款时，只能引用【拟引用规范依据】中的具体条款号和条款内容；【辅助判断依据】只能用于事实判断，不得写成结论依据
- 如果拟引用依据不足以支撑某个具体条款号，应写"参照相关规定并结合临床资料综合评定"，不要编造条款号
- 三期分析固定句式："因此，根据其损伤情况，结合其年龄、自身情况、临床治疗经过、目前恢复情况，参照...有关规定，其...拟定为X日"
- 三期天数必须落在护栏或规范依据列出的范围内；多发损伤只能综合考虑，不能简单相加
- 只有委托事项包含后续治疗费/治疗费时才分析费用，金额需同时写大写和小写
- 不要输出ICD编码、Markdown项目符号、思考过程或解释说明
- 只输出分析说明正文，不要标题"""

    user_prompt = f"""【被鉴定人】{person_name}
【委托鉴定事项】{entrustment}
【本次只允许分析的委托项目】{entrusted_items}

【分析说明护栏】
{harness_context}

住院记录数据（共{len(records)}份）：
{json.dumps(hospital_data, ensure_ascii=False, indent=2)}

影像学报告数据（共{len(img_reports)}份）：
{json.dumps(imaging_data, ensure_ascii=False, indent=2)}

【规范依据】
{standard_context}"""

    llm_result = call_llm_text_harness(
        task_name="generate_analysis",
        system_prompt=system_prompt,
        instructions=user_prompt,
        temperature=0.0,
        max_tokens=5000,
        max_input_chars=12000,
        max_retries=1,
    )

    if llm_result.get("success"):
        analysis_text = _clean_generated_report_text(llm_result["content"])
        analysis_text, warnings = _remove_uncommissioned_analysis(analysis_text, entrustment)
        warnings.extend(validate_analysis_text(analysis_text, harness_payload))
        warnings.extend(harness_payload.get("warnings", []))
        warnings = list(dict.fromkeys(warnings))
        logger.info(f"案件 {case_id} 分析说明 LLM 生成成功（检索到 {len(standard_refs)} 条标准条款）")
        return {
            "success": True,
            "analysis": analysis_text,
            "method": "llm",
            "standard_references": standard_refs,
            "warnings": warnings,
        }

    # Fallback
    logger.warning(f"案件 {case_id} 分析说明 LLM 生成失败（{llm_result.get('error')}），回退到拼接")
    parts = [f"根据委托单位提供的现有材料，结合本鉴定中心检验所见，现分析如下：", ""]
    parts.append(f"1. 被鉴定人{person_name}外伤史确切，系事故所致。")
    diag_list = "；".join(r.discharge_diagnosis or "" for r in records)
    parts.append(f"2. 被鉴定人{person_name}外伤致{diag_list}诊断明确。")
    parts.append(f"3. 综合分析认为，被鉴定人{person_name}的损伤与本次外伤之间存在直接因果关系。")
    fallback_text, warnings = _remove_uncommissioned_analysis("\n".join(parts), entrustment)
    warnings.extend(harness_payload.get("warnings", []))
    warnings = list(dict.fromkeys(warnings))
    return {
        "success": True,
        "analysis": fallback_text,
        "method": "fallback",
        "standard_references": standard_refs,
        "warnings": warnings,
    }


def _generate_opinion(case_id: int, db) -> dict:
    """基于分析说明生成鉴定意见"""
    case = db.query(Case).filter(Case.id == case_id).first()
    report = db.query(Report).filter(Report.case_id == case_id).first()
    person = db.query(Person).filter(Person.case_id == case_id).first()

    if not report or not report.analysis:
        return {"success": False, "error": "请先生成分析说明"}

    person_name = person.name if person else "被鉴定人"
    entrustment = case.entrustment_matter if case else ""

    # 基于分析说明自动提取关键结论
    prompt = f"""请根据以下"分析说明"内容，提炼出鉴定意见。

【权威信息】（已由鉴定人确认，必须以此为准）
被鉴定人：{person_name}
委托事项：{entrustment}

【写作要求】
鉴定意见是分析说明的简化版，只给结论，不写理由。

【核心原则】
1. 分析说明的结构通常是：第1条确认外伤来源，第2条确认伤情诊断，第3条起才是委托事项的结论
2. 鉴定意见只需提炼第3条及之后的内容（委托事项结论），前两条（外伤来源确认、伤情确认）不写入鉴定意见
3. 分析说明中有的结论才写入，没有的不可凭空编造
4. 如果委托事项中包含某项但分析说明未涉及该项，则不生成该条意见

【格式规范】
- 每条意见对应分析说明中第3条起的一个分析结论，必须用阿拉伯数字编号（1. 2. 3. 等）
- 不要加分类标签（如"伤残等级""误工期""护理期"等），不要用箭头，直接写结论
- 不要加粗体、标题等Markdown格式
- 示例格式：
  "1. 被鉴定人{person_name}右侧额叶脑挫裂伤后遗脑软化灶形成评为十级伤残。"
  "2. 被鉴定人{person_name}出院后的误工期拟定为60日。"
  "3. 被鉴定人{person_name}后续治疗费用约需人民币捌仟圆（¥8000.0元）。"
- 以上仅为格式参考，实际只输出分析说明中存在的结论，编号连续即可

其他要求：
1. 只给结论，不写分析过程和理由（理由在分析说明中已有）
2. 数字金额需同时写大写和小写，如"捌仟圆（¥8000.0元）"
3. 语言简洁、明确、断句干脆
4. 输出纯文本，不要JSON，不要Markdown格式，不要加"六、鉴定意见"标题

分析说明内容：
{report.analysis[:3000]}"""

    result = call_llm(
        system_prompt="你是一个司法鉴定意见书撰写助手。请根据分析说明提炼鉴定意见。",
        user_prompt=prompt,
        temperature=0.2,
    )

    if not result.get("success"):
        return {"success": False, "error": result.get("error", "生成失败")}

    opinion_text = _clean_generated_report_text(result["content"])
    return {
        "success": True,
        "opinion": opinion_text,
        "model": result.get("model", ""),
    }


def extract_case_facts_text(case_id: int, db) -> dict:
    """从数据库已有字段拼接生成基本案情文本（纯拼接，不调用LLM）

    基本案情结构：
    1. 事故经过：照抄交通事故认定书中"道路交通事故发生经过"原文
    2. 就医经过：XXX伤后就诊于XXXX。
    3. 委托语句：现为处理案件需要，{委托单位}特委托我鉴定中心{规范化委托事项}。
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        return {"success": False, "error": "案件不存在"}

    person = db.query(Person).filter(Person.case_id == case_id).first()
    person_name = person.name if person and person.name else "被鉴定人"

    parts = []

    # === 第一段：事故经过（认定书原文） ===
    if case.accident_description:
        parts.append(case.accident_description)

    # === 第二段：就医经过 ===
    hospital_names = _get_treatment_hospitals(case_id, db)
    if hospital_names:
        hospital_str = "、".join(hospital_names)
        if len(hospital_names) == 1:
            parts.append(f"{person_name}伤后就诊于{hospital_str}。")
        else:
            parts.append(f"{person_name}伤后先后就诊于{hospital_str}。")

    # === 第三段：委托语句（固定格式） ===
    entrusting_unit = case.entrusting_unit or "委托单位"
    entrustment_matter = _normalize_entrustment_matter(case.entrustment_matter or "", person_name)
    parts.append(f"现为处理案件需要，{entrusting_unit}特委托我鉴定中心{entrustment_matter}。")

    case_facts = "".join(parts)

    if not case_facts.strip():
        return {"success": False, "error": "缺少事故信息和委托信息，请先提取委托书和交通事故认定书"}

    return {
        "success": True,
        "case_facts": case_facts,
        "model": "拼接（无LLM）",
    }
