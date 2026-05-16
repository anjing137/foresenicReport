"""
材料子类型识别工具

第一版采用规则分类：上传时仍只分大类，OCR 后根据文本给病历/影像页打内部子类型标签。
"""
import re
from typing import Any, Optional

from app.config import settings
from app.models.case import MaterialType
from app.utils.llm import call_llm_json_harness


CLASSIFIER_VERSION = "2026-05-13.3"


MATERIAL_SUBTYPE_LABELS = {
    # 医院病历
    "medical_home_page": "病案首页",
    "admission_record": "入院记录",
    "discharge_record": "出院记录",
    "operation_record": "手术记录",
    "progress_note": "病程记录",
    "consultation_record": "会诊记录",
    "medical_order_nursing": "医嘱/护理记录",
    "lab_report": "检验报告",
    "other_medical_record": "其他病历材料",
    # 影像/辅助检查
    "ct_report": "CT报告",
    "xray_report": "X线/DR报告",
    "mri_report": "MRI报告",
    "ultrasound_report": "超声报告",
    "emg_report": "肌电图/神经电生理",
    "imaging_film": "影像片/图像页",
    "other_imaging_report": "其他影像/检查报告",
}


def get_material_subtype_label(subtype: Optional[str]) -> Optional[str]:
    if not subtype:
        return None
    return MATERIAL_SUBTYPE_LABELS.get(subtype, subtype)


def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("<nl>", "\n")
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " 图片 ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"#+", " ", text)
    return re.sub(r"\s+", "", text)


def _has_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _matched(text: str, keywords: tuple[str, ...]) -> list[str]:
    return [kw for kw in keywords if kw in text]


def extract_hospital_name(ocr_text: str = "") -> Optional[str]:
    text = _clean_text(ocr_text)
    if not text:
        return None
    matches = re.findall(r"[\u4e00-\u9fa5]{2,30}(?:人民医院|中心医院|中医院|医院|卫生院)", text)
    invalid = {"人民医院", "中心医院", "中医院", "医院", "卫生院"}
    for match in matches:
        if match == "阳县人民医院" and "原阳县" in text:
            return "原阳县人民医院"
        if match not in invalid:
            return match
    return None


def classify_material_subtype(material_type: str, ocr_text: str = "") -> Optional[str]:
    """根据材料大类和 OCR 文本返回内部子类型。"""
    text = _clean_text(ocr_text)
    if not text:
        return None

    if material_type == MaterialType.MEDICAL_RECORD:
        if _has_any(text, ("住院病案首页", "病案首页", "医疗付费方式", "病案号")):
            return "medical_home_page"
        if _has_any(text, ("出院记录", "出院小结", "出院（小结）记录", "出院(小结)记录", "出院诊断", "出院医嘱")):
            return "discharge_record"
        if _has_any(text, ("手术记录", "手术日期", "手术名称", "术中诊断", "手术经过", "麻醉方式")):
            return "operation_record"
        if _has_any(text, ("会诊申请单", "会诊记录", "会诊目的", "会诊科室", "会诊医师", "会诊意见")):
            return "consultation_record"
        if _has_any(text, ("入院记录", "入院时间", "入院情况", "主诉", "现病史")):
            return "admission_record"
        if _has_any(text, ("首次病程记录", "日常病程记录", "病程记录", "主任医师查房记录", "上级医师查房记录")):
            return "progress_note"
        if _has_any(text, ("长期医嘱", "临时医嘱", "医嘱单", "护理记录", "体温单", "护理评估")):
            return "medical_order_nursing"
        if _has_any(text, ("检验报告", "检验项目", "参考范围", "血常规", "尿常规", "生化", "凝血", "D-二聚体")):
            return "lab_report"
        return "other_medical_record"

    if material_type == MaterialType.IMAGING_REPORT:
        if _looks_like_lab_report(text):
            return "lab_report"
        if _has_any(text, ("肌电图", "神经电图", "神经肌电图", "运动传导", "感觉传导", "F波", "H反射", "肌电图测定")):
            return "emg_report"
        if _has_any(text, ("MRI", "磁共振", "核磁共振")):
            return "mri_report"
        if _has_any(text, ("彩超", "超声", "B超")):
            return "ultrasound_report"
        if _has_any(text, ("CT", "计算机断层", "三维重建")):
            return "ct_report"
        if _has_any(text, ("DR", "CR", "X线", "X光", "摄片", "平片")):
            return "xray_report"
        if _has_any(text, ("影像表现", "诊断意见", "报告医师", "审核医师", "检查项目", "检查时间")):
            return "other_imaging_report"
        if _has_any(text, ("图片", "运动曲线", "感觉曲线", "曲线")):
            return "imaging_film"
        return "other_imaging_report"

    return None


def classify_pdf_page(ocr_text: str = "") -> dict:
    """对 PDF 单页 OCR 文本进行大类、子类型和分组预测。"""
    text = _clean_text(ocr_text)
    if not text:
        return {
            "material_type": None,
            "material_type_label": None,
            "material_subtype": None,
            "material_subtype_label": None,
            "group_name": None,
            "confidence": 0.0,
            "reason": "无 OCR 文本",
            "matched_keywords": [],
            "is_continuation": False,
        }

    head = text[:320]
    medical_header_subtype = _medical_header_subtype(text)
    if medical_header_subtype:
        return _classification_result(
            MaterialType.MEDICAL_RECORD,
            medical_header_subtype,
            extract_hospital_name(ocr_text),
            0.94,
            _matched_medical_keywords(text),
            text,
        )

    if _looks_like_lab_report(text):
        return _classification_result(
            MaterialType.IMAGING_REPORT,
            "lab_report",
            extract_hospital_name(ocr_text),
            0.86,
            _matched_lab_keywords(text),
            text,
        )

    entrustment_keywords = ("司法鉴定委托书", "委托贵机构予以鉴定", "鉴定要求")
    matched = _matched(text, entrustment_keywords)
    if "司法鉴定委托书" in head or len(matched) >= 2:
        return _classification_result(MaterialType.ENTRUSTMENT_LETTER, None, None, 0.98, matched, text)

    id_keywords = ("居民身份证", "公民身份号码", "签发机关", "中华人民共和国")
    matched = _matched(text, id_keywords)
    if "居民身份证" in text and _has_any(text, ("公民身份号码", "签发机关", "中华人民共和国")):
        return _classification_result(MaterialType.ID_CARD, None, None, 0.98, matched, text)

    litigation_keywords = (
        "民事起诉状", "诉讼请求", "原告", "被告", "具状人", "证据清单",
        "残疾赔偿金", "精神损害赔偿金", "交通事故发生情况",
    )
    matched = _matched(text, litigation_keywords)
    litigation_has_party_structure = "诉讼请求" in text and _has_any(text, ("原告", "被告", "具状人"))
    if "民事起诉状" in head or litigation_has_party_structure or len(matched) >= 3:
        return _classification_result(MaterialType.LITIGATION_MATERIAL, None, None, 0.96 if "民事起诉状" in head else 0.88, matched, text)

    traffic_keywords = ("道路交通事故认定书", "交通事故发生经过", "道路交通事故基本事实", "事故形成原因分析", "当事人导致交通事故")
    matched = _matched(text, traffic_keywords)
    traffic_title_near_head = "道路交通事故认定书" in text[:120]
    traffic_has_cert_structure = _has_any(text, ("道路交通事故基本事实", "事故形成原因分析", "交通事故发生经过")) and _has_any(text, ("当事人导致交通事故", "交通事故成因", "道路交通安全法"))
    if traffic_title_near_head or traffic_has_cert_structure:
        return _classification_result(MaterialType.TRAFFIC_ACCIDENT_CERT, None, None, 0.98 if traffic_title_near_head else 0.92, matched, text)

    appraisal_keywords = ("鉴定申请书", "申请事项", "事实与理由", "申请人")
    matched = _matched(text, appraisal_keywords)
    if "鉴定申请书" in head or (text[:100].startswith("申请") and "申请人" in text):
        return _classification_result(MaterialType.APPRAISAL_APPLICATION, None, None, 0.96 if "鉴定申请书" in head else 0.86, matched, text)
    if len(matched) >= 3 and "诉讼请求" not in text[:500]:
        return _classification_result(MaterialType.APPRAISAL_APPLICATION, None, None, 0.88, matched, text)

    medical_subtype = classify_material_subtype(MaterialType.MEDICAL_RECORD, ocr_text)
    imaging_subtype = classify_material_subtype(MaterialType.IMAGING_REPORT, ocr_text)
    hospital_name = extract_hospital_name(ocr_text)

    medical_strong = {
        "medical_home_page", "admission_record", "discharge_record",
        "operation_record", "progress_note", "consultation_record",
        "medical_order_nursing", "lab_report",
    }
    imaging_strong = {
        "ct_report", "xray_report", "mri_report", "ultrasound_report",
        "emg_report", "other_imaging_report", "imaging_film",
    }

    if medical_subtype in medical_strong and imaging_subtype in (None, "imaging_film", "other_imaging_report"):
        return _classification_result(
            MaterialType.MEDICAL_RECORD,
            medical_subtype,
            hospital_name,
            0.88,
            _matched_medical_keywords(text),
            text,
        )

    if medical_subtype in medical_strong and medical_subtype != "lab_report":
        return _classification_result(
            MaterialType.MEDICAL_RECORD,
            medical_subtype,
            hospital_name,
            0.88,
            _matched_medical_keywords(text),
            text,
        )

    if (
        imaging_subtype in imaging_strong
        and imaging_subtype != "other_imaging_report"
        and _looks_like_imaging_report(text, imaging_subtype)
    ):
        return _classification_result(
            MaterialType.IMAGING_REPORT,
            imaging_subtype,
            hospital_name,
            0.90 if imaging_subtype != "imaging_film" else 0.72,
            _matched_imaging_keywords(text),
            text,
        )

    if imaging_subtype == "other_imaging_report":
        return _classification_result(
            MaterialType.IMAGING_REPORT,
            imaging_subtype,
            hospital_name,
            0.70,
            _matched_imaging_keywords(text),
            text,
        )

    if medical_subtype == "other_medical_record":
        return _classification_result(
            MaterialType.MEDICAL_RECORD,
            medical_subtype,
            hospital_name,
            0.60,
            _matched_medical_keywords(text),
            text,
        )

    return {
        "material_type": None,
        "material_type_label": None,
        "material_subtype": None,
        "material_subtype_label": None,
        "group_name": hospital_name,
        "confidence": 0.0,
        "reason": "未命中明确分类关键词",
        "matched_keywords": [],
        "is_continuation": _looks_like_continuation(text),
    }


def classify_pdf_page_with_llm(ocr_text: str = "", rule_prediction: Optional[dict] = None) -> Optional[dict]:
    """调用 LLM 对 PDF 页面做材料大类兜底判断。

    仅建议在规则分类低置信度或未识别时调用，避免每页都产生模型费用。
    """
    text = (ocr_text or "").strip()
    if not text:
        return None

    allowed_types = ", ".join(MaterialType.ALL)
    subtype_values = ", ".join(sorted(MATERIAL_SUBTYPE_LABELS.keys()))
    rule_hint = _compact_prediction(rule_prediction)
    instructions = f"""请根据 OCR 文本判断这页司法鉴定材料属于哪一类。

可选 material_type 只能是以下值之一：
{allowed_types}

如果 material_type 是 medical_record 或 imaging_report，可选 material_subtype：
{subtype_values}

分类原则：
1. 优先判断页面本身是什么材料，不要因为正文引用其他材料名称就改类。
2. 民事起诉状、诉讼请求、原告/被告、具状人等通常归入 litigation_material。
3. 道路交通事故认定书只有页面标题或完整认定书结构明确时才归入 traffic_accident_cert；诉状正文里引用“事故认定书”不算。
4. 入院记录、手术记录、出院记录、病程记录、会诊记录等归入 medical_record。
5. CT、DR、X线、MRI、超声、肌电图、检验报告等独立检查/检验报告归入 imaging_report。
6. 返回严格 JSON，不要解释。

规则分类初判：
{rule_hint}
"""
    output_schema = """{
  "material_type": "上述 material_type 之一，无法判断填 null",
  "material_subtype": "上述 material_subtype 之一，无法判断填 null",
  "group_name": "医院名称，无法判断填 null",
  "confidence": 0.0,
  "reason": "一句话说明依据",
  "matched_keywords": ["关键词1", "关键词2"],
  "is_continuation": false
}"""

    result = call_llm_json_harness(
        task_name="classify_pdf_page",
        system_prompt="你是司法鉴定材料分类助手，只能根据 OCR 原文分类，不得编造。",
        instructions=instructions,
        input_text=text,
        output_schema=output_schema,
        required_fields=("material_type", "material_subtype", "group_name", "confidence", "reason", "matched_keywords", "is_continuation"),
        model=settings.LLM_CLASSIFIER_MODEL or None,
        temperature=0.0,
        max_tokens=900,
        max_input_chars=4000,
        max_retries=1,
    )
    if not result.get("success"):
        return None

    normalized = _normalize_llm_classification(result.get("data") or {})
    if normalized:
        normalized["harness"] = result.get("harness", {})
        normalized["model"] = result.get("model", "")
        normalized["provider"] = result.get("provider", "")
    return normalized


def _classification_result(
    material_type: str,
    subtype: Optional[str],
    group_name: Optional[str],
    confidence: float,
    matched_keywords: list[str],
    text: str,
) -> dict:
    material_label = MaterialType.LABELS.get(material_type, material_type)
    subtype_label = get_material_subtype_label(subtype)
    reason_bits = []
    if matched_keywords:
        reason_bits.append("命中关键词：" + "、".join(matched_keywords[:5]))
    if subtype_label:
        reason_bits.append(f"判断子类型为{subtype_label}")
    return {
        "material_type": material_type,
        "material_type_label": material_label,
        "material_subtype": subtype,
        "material_subtype_label": subtype_label,
        "group_name": group_name,
        "confidence": confidence,
        "reason": "；".join(reason_bits) or f"判断为{material_label}",
        "matched_keywords": matched_keywords[:8],
        "is_continuation": _looks_like_continuation(text),
    }


def _compact_prediction(prediction: Optional[dict]) -> str:
    if not prediction:
        return "无"
    return (
        f"material_type={prediction.get('material_type')}, "
        f"material_subtype={prediction.get('material_subtype')}, "
        f"confidence={prediction.get('confidence')}, "
        f"reason={prediction.get('reason')}"
    )


def _normalize_llm_classification(data: dict[str, Any]) -> Optional[dict]:
    material_type = data.get("material_type")
    if material_type in ("", "null", "None"):
        material_type = None
    if material_type is not None and material_type not in MaterialType.ALL:
        return None

    subtype = data.get("material_subtype")
    if subtype in ("", "null", "None"):
        subtype = None
    if subtype is not None and subtype not in MATERIAL_SUBTYPE_LABELS:
        subtype = None

    confidence_raw = data.get("confidence", 0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence > 1:
        confidence = confidence / 100
    confidence = max(0.0, min(confidence, 0.98))

    matched_keywords = data.get("matched_keywords") or []
    if isinstance(matched_keywords, str):
        matched_keywords = [matched_keywords]
    if not isinstance(matched_keywords, list):
        matched_keywords = []

    group_name = data.get("group_name")
    group_name = str(group_name).strip() if group_name not in (None, "", "null", "None") else None

    return {
        "material_type": material_type,
        "material_type_label": MaterialType.LABELS.get(material_type) if material_type else None,
        "material_subtype": subtype,
        "material_subtype_label": get_material_subtype_label(subtype),
        "group_name": group_name,
        "confidence": confidence,
        "reason": str(data.get("reason") or "LLM 兜底分类").strip(),
        "matched_keywords": [str(kw) for kw in matched_keywords[:8]],
        "is_continuation": bool(data.get("is_continuation", False)),
        "llm_classified": True,
    }


def _matched_medical_keywords(text: str) -> list[str]:
    keywords = (
        "住院病案首页", "病案首页", "入院记录", "出院记录", "出院小结",
        "出院（小结）记录", "手术记录", "病程记录", "会诊申请单", "会诊记录", "医嘱单",
        "护理记录", "检验报告", "主诉", "现病史", "出院诊断",
    )
    return [kw for kw in keywords if kw in text]


def _matched_imaging_keywords(text: str) -> list[str]:
    keywords = (
        "CT", "DR", "CR", "X线", "MRI", "磁共振", "超声", "彩超",
        "肌电图", "神经电图", "运动传导", "感觉传导", "影像表现",
        "诊断意见", "检查项目", "报告医师",
    )
    return [kw for kw in keywords if kw in text]


def _matched_lab_keywords(text: str) -> list[str]:
    keywords = (
        "检验报告", "检验报告单", "标本类型", "标本类别", "样本类型",
        "参考范围", "血常规", "尿常规", "生化", "凝血", "D-二聚体",
    )
    return [kw for kw in keywords if kw in text]


def _medical_header_subtype(text: str) -> Optional[str]:
    head = text[:220]
    header_rules = (
        ("medical_home_page", ("住院病案首页", "病案首页")),
        ("admission_record", ("入院记录",)),
        ("discharge_record", ("出院记录", "出院小结", "出院（小结）记录", "出院(小结)记录")),
        ("operation_record", ("手术记录",)),
        ("consultation_record", ("会诊申请单", "会诊记录")),
        ("progress_note", ("首次病程记录", "日常病程记录", "病程记录")),
        ("medical_order_nursing", ("长期医嘱", "临时医嘱", "医嘱单", "护理记录")),
    )
    for subtype, keywords in header_rules:
        if _has_any(head, keywords):
            return subtype
    return None


def _looks_like_lab_report(text: str) -> bool:
    if not text:
        return False
    lab_title = _has_any(text[:260], ("检验报告", "检验报告单"))
    lab_evidence = _has_any(text, ("标本类型", "标本类别", "样本类型", "参考范围", "送检科室", "样本号", "标本号"))
    lab_items = _has_any(text, ("血常规", "尿常规", "生化", "凝血", "D-二聚体", "白细胞", "红细胞", "血红蛋白"))
    return lab_title or (lab_evidence and lab_items)


def _looks_like_imaging_report(text: str, subtype: Optional[str]) -> bool:
    if not text or not subtype:
        return False
    if subtype == "imaging_film":
        return True

    head = text[:360]
    modality_map = {
        "ct_report": ("CT", "计算机断层", "三维重建"),
        "xray_report": ("DR", "CR", "X线", "X光", "摄片", "平片"),
        "mri_report": ("MRI", "磁共振", "核磁共振"),
        "ultrasound_report": ("彩超", "超声", "B超"),
        "emg_report": ("肌电图", "神经电图", "神经肌电图", "运动传导", "感觉传导"),
    }
    modality_keywords = modality_map.get(subtype, ("检查报告", "检查项目"))
    report_structure = (
        "影像表现", "影像诊断", "诊断意见", "检查项目", "检查部位",
        "报告医师", "审核医师", "检查号", "报告时间", "检查时间",
    )
    if _has_any(head, modality_keywords) and _has_any(text, report_structure):
        return True
    if _has_any(text, modality_keywords) and len(_matched(text, report_structure)) >= 2:
        return True
    return False


def _looks_like_continuation(text: str) -> bool:
    if not text:
        return False
    title_keywords = (
        "司法鉴定委托书", "居民身份证", "道路交通事故认定书", "鉴定申请书",
        "民事起诉状", "住院病案首页", "入院记录", "出院记录", "手术记录",
        "病程记录", "会诊", "CT", "MRI", "X线", "超声", "肌电图",
    )
    return not any(keyword in text[:120] for keyword in title_keywords)
