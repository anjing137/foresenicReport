"""分析说明正文质量护栏。

本模块把「LLM 输出是否可作为正式正文」和「不可采用时如何保守回退」
集中在一个接口后面，避免路由层知道候选状态、委托事项和诊断拼接细节。
"""
from __future__ import annotations

import re
from typing import Any

from app.models.case import Case, HospitalRecord


def analysis_output_has_work_trace(text: str) -> bool:
    """判断输出是否混入了护栏、候选清单或模型工作过程。"""
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return True
    bad_terms = (
        "根据任务要求",
        "让我先整理",
        "我需要生成",
        "被鉴定人信息：",
        "委托单位：",
        "委托鉴定事项：",
        "伤残/三期候选清单",
        "候选清单分析",
        "[met;status=",
        "[uncertain;status=",
        "建议/范围：",
        "风险提示：",
        "**",
    )
    if any(term in compact for term in bad_terms):
        return True
    return not compact.startswith("根据委托单位提供的现有材料")


def build_analysis_fallback_from_harness(
    case: Case,
    person_name: str,
    records: list[HospitalRecord],
    harness_payload: dict[str, Any],
    entrustment: str,
) -> str:
    """用已采信候选生成保守版正式分析说明。"""
    flags = _entrustment_flags(entrustment)
    lines = ["根据委托单位提供的现有材料，结合本鉴定中心检验所见，现分析如下："]
    accident_source = "道路交通事故" if any(term in _join_text(case.accident_description, entrustment) for term in ("交通事故", "道路交通")) else "本次事故"
    lines.append(f"1. 被鉴定人{person_name}外伤史确切，系{accident_source}所致。")

    diagnosis_summary = _diagnosis_summary(records, harness_payload)
    if diagnosis_summary:
        lines.append(f"2. 被鉴定人{person_name}外伤致{diagnosis_summary}等诊断明确（经病历、影像学检查及手术等证实）。")
    else:
        lines.append(f"2. 被鉴定人{person_name}本次外伤后相关损伤诊断明确（经病历及检查材料证实）。")

    number = 3
    accepted_met = [
        item
        for item in harness_payload.get("candidates") or []
        if item.get("decision") == "met" and item.get("status") == "accepted"
    ]
    if flags["disability"]:
        for item in accepted_met:
            if item.get("category") != "伤残等级":
                continue
            standard = _standard_sentence(item)
            title = item.get("title") or "相关损伤"
            reason = (item.get("reason") or "").rstrip("。")
            reason_text = f"{reason}。" if reason else ""
            lines.append(
                f"{number}. 被鉴定人{person_name}{title}。{reason_text}"
                f"有鉴于此，参照{standard}，被鉴定人{person_name}{title}应评为{item.get('grade') or '相应等级'}伤残。"
            )
            number += 1

    if any(flags[key] for key in ("work_loss", "nursing", "nutrition")):
        for item in accepted_met:
            if item.get("category") != "三期":
                continue
            standard = _standard_sentence(item)
            suggestion = (item.get("suggestion") or item.get("grade") or "在相应期限范围内综合评定").rstrip("。")
            lines.append(
                f"{number}. 被鉴定人{person_name}因本次外伤接受临床治疗，伤情稳定恢复。"
                f"因此，根据其损伤情况，结合其年龄、自身情况、临床治疗经过、目前恢复情况，"
                f"参照{standard}有关规定，{suggestion}。"
            )
            number += 1

    if number == 3:
        lines.append("3. 现有已采信事实尚不足以直接形成伤残等级或三期结论，需结合临床检查及规范条款进一步核对。")
    return "\n\n".join(lines)


def _entrustment_flags(entrustment: str) -> dict[str, bool]:
    text = entrustment or ""
    return {
        "disability": any(term in text for term in ("伤残", "残疾", "致残", "伤残等级")),
        "work_loss": "误工" in text,
        "nursing": "护理" in text and "护理依赖" not in text,
        "nutrition": "营养" in text,
    }


def _standard_sentence(item: dict[str, Any]) -> str:
    refs = item.get("standards") or []
    if not refs:
        return "相关规定并结合临床资料综合评定"
    ref = refs[0]
    name = ref.get("standard_name") or "相关标准"
    clause = ref.get("clause_label") or ref.get("section_title") or ref.get("snippet") or ""
    grade = f"（{ref.get('grade')}）" if ref.get("grade") else ""
    if clause:
        return f"《{name}》{grade}中关于“{clause}”的规定"
    return f"《{name}》{grade}相关规定"


def _diagnosis_summary(records: list[HospitalRecord], harness_payload: dict[str, Any]) -> str:
    pieces: list[str] = []
    for record in records:
        for part in (record.discharge_diagnosis, record.admission_diagnosis):
            for item in re.split(r"[；;。\n]+", part or ""):
                item = item.strip(" ，,、")
                if item and item not in pieces:
                    pieces.append(item)
    if not pieces:
        for candidate in harness_payload.get("candidates") or []:
            for ev in candidate.get("evidence") or []:
                summary = (ev.get("summary") or ev.get("quote") or "").strip()
                if summary and summary not in pieces:
                    pieces.append(summary)
    return "、".join(pieces[:8])


def _join_text(*parts: Any) -> str:
    return "\n".join(str(part or "") for part in parts)
