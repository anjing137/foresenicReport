"""
案件管理路由
"""
import os
import threading
import time
import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional

from app.database import get_db, SessionLocal
from app.models.case import Case, CaseStatus, MaterialType, MaterialGroup, Person, Material, OcrStatus

# 停止请求存储：key=case_id, value="stop"(等当前完)|"force"(强制立刻停)
_stop_flags: dict[int, str] = {}
_flags_lock = threading.Lock()
_ocr_tasks: dict[int, dict] = {}
_tasks_lock = threading.Lock()
from app.schemas.case import (
    CaseCreate, CaseUpdate, CaseResponse, CaseListResponse, PersonResponse,
    MaterialGroupResponse
)
from app.utils.ocr import run_ocr, extract_text_from_result
from app.config import settings

router = APIRouter(prefix="/api/cases", tags=["案件管理"])
logger = logging.getLogger(__name__)


def get_status_label(status: str) -> str:
    return CaseStatus.LABELS.get(status, status)


def get_material_type_label(material_type: str) -> str:
    return MaterialType.LABELS.get(material_type, material_type)


def _material_counts(db: Session, case_id: int) -> dict:
    materials = db.query(Material).filter(Material.case_id == case_id).all()
    return {
        "total": len(materials),
        "pending": sum(1 for m in materials if m.ocr_status == OcrStatus.PENDING),
        "processing": sum(1 for m in materials if m.ocr_status == OcrStatus.PROCESSING),
        "completed": sum(1 for m in materials if m.ocr_status == OcrStatus.COMPLETED),
        "failed": sum(1 for m in materials if m.ocr_status == OcrStatus.FAILED),
    }


def _public_task_snapshot(case_id: int, db: Session = None) -> Optional[dict]:
    with _tasks_lock:
        task = _ocr_tasks.get(case_id)
        snapshot = dict(task) if task else None
    if snapshot and db:
        snapshot["counts"] = _material_counts(db, case_id)
    return snapshot


def _update_task(case_id: int, **updates) -> None:
    with _tasks_lock:
        task = _ocr_tasks.get(case_id)
        if task:
            task.update(updates)


def _append_task_result(case_id: int, result: dict) -> None:
    with _tasks_lock:
        task = _ocr_tasks.get(case_id)
        if not task:
            return
        results = task.setdefault("recent_results", [])
        results.append(result)
        if len(results) > 20:
            del results[:-20]


def _finish_task(case_id: int, **updates) -> None:
    updates.setdefault("finished_at", datetime.now().isoformat(timespec="seconds"))
    updates.setdefault("current_material_id", None)
    updates.setdefault("current_filename", None)
    _update_task(case_id, **updates)


def _run_ocr_task(case_id: int, material_ids: List[int], task_id: str) -> None:
    db = SessionLocal()
    stopped = False
    stop_type = None
    try:
        case = db.query(Case).filter(Case.id == case_id).first()
        if not case:
            _finish_task(case_id, status="failed", message="案件不存在")
            return

        case.status = CaseStatus.RECOGNIZING
        db.commit()

        save_dir = os.path.join(str(settings.UPLOAD_DIR), str(case_id), "ocr_result")
        for material_id in material_ids:
            with _flags_lock:
                flag = _stop_flags.get(case_id)
            if flag == "force":
                stopped = True
                stop_type = "force"
                break

            material = db.query(Material).filter(
                Material.id == material_id,
                Material.case_id == case_id,
            ).first()
            if not material:
                continue

            _update_task(
                case_id,
                current_material_id=material.id,
                current_filename=material.original_filename,
                message=f"正在识别 {material.original_filename or material.id}",
            )

            if not material.file_path or not os.path.exists(material.file_path):
                material.ocr_status = OcrStatus.FAILED
                material.ocr_text = "文件不存在"
                db.commit()
                _append_task_result(case_id, {
                    "material_id": material.id,
                    "filename": material.original_filename,
                    "status": "failed",
                    "error": "文件不存在",
                })
                _increment_task_progress(case_id, failed=1)
                continue

            material.ocr_status = OcrStatus.PROCESSING
            db.commit()

            try:
                result = run_ocr(material.file_path, save_dir=save_dir)
                text = extract_text_from_result(result)

                with _flags_lock:
                    flag = _stop_flags.get(case_id)
                if flag == "force":
                    material.ocr_status = OcrStatus.PENDING
                    material.ocr_text = ""
                    db.commit()
                    stopped = True
                    stop_type = "force"
                    break

                if text:
                    material.ocr_text = text
                    material.ocr_status = OcrStatus.COMPLETED
                    if result.get("md_path"):
                        material.ocr_file_path = result["md_path"]
                    db.commit()
                    _append_task_result(case_id, {
                        "material_id": material.id,
                        "filename": material.original_filename,
                        "status": "completed",
                        "text_length": len(text),
                    })
                    _increment_task_progress(case_id, completed=1)
                else:
                    material.ocr_text = ""
                    material.ocr_status = OcrStatus.FAILED
                    db.commit()
                    _append_task_result(case_id, {
                        "material_id": material.id,
                        "filename": material.original_filename,
                        "status": "failed",
                        "error": result.get("error", "无识别结果"),
                    })
                    _increment_task_progress(case_id, failed=1)
            except Exception as e:
                logger.exception("OCR 后台任务识别材料失败: case_id=%s material_id=%s", case_id, material.id)
                material.ocr_text = f"OCR错误: {str(e)}"
                material.ocr_status = OcrStatus.FAILED
                db.commit()
                _append_task_result(case_id, {
                    "material_id": material.id,
                    "filename": material.original_filename,
                    "status": "failed",
                    "error": str(e),
                })
                _increment_task_progress(case_id, failed=1)

            with _flags_lock:
                flag = _stop_flags.get(case_id)
            if flag == "stop":
                stopped = True
                stop_type = "stop"
                break

        case = db.query(Case).filter(Case.id == case_id).first()
        if case:
            if stopped:
                case.status = CaseStatus.PENDING_UPLOAD
            else:
                case.status = CaseStatus.PENDING_REVIEW
            for m in db.query(Material).filter(
                Material.case_id == case_id,
                Material.ocr_status == OcrStatus.PROCESSING,
            ).all():
                m.ocr_status = OcrStatus.PENDING
            db.commit()

        with _flags_lock:
            _stop_flags.pop(case_id, None)

        if stopped:
            _finish_task(
                case_id,
                status="stopped",
                stop_type=stop_type,
                message=f"识别已停止（{'强制' if stop_type == 'force' else '正常'}停止）",
            )
        else:
            _finish_task(case_id, status="completed", message="识别完成")
    except Exception as e:
        logger.exception("OCR 后台任务失败: case_id=%s task_id=%s", case_id, task_id)
        try:
            for m in db.query(Material).filter(
                Material.case_id == case_id,
                Material.ocr_status == OcrStatus.PROCESSING,
            ).all():
                m.ocr_status = OcrStatus.PENDING
            case = db.query(Case).filter(Case.id == case_id).first()
            if case and case.status == CaseStatus.RECOGNIZING:
                case.status = CaseStatus.PENDING_UPLOAD
            db.commit()
        except Exception:
            db.rollback()
        _finish_task(case_id, status="failed", message=f"OCR任务失败: {str(e)}")
    finally:
        db.close()


def _increment_task_progress(case_id: int, completed: int = 0, failed: int = 0) -> None:
    with _tasks_lock:
        task = _ocr_tasks.get(case_id)
        if not task:
            return
        task["processed"] = task.get("processed", 0) + completed + failed
        task["completed"] = task.get("completed", 0) + completed
        task["failed"] = task.get("failed", 0) + failed


def _start_ocr_background_task(case_id: int, material_ids: List[int], mode: str) -> dict:
    with _tasks_lock:
        existing = _ocr_tasks.get(case_id)
        if existing and existing.get("status") == "running":
            return dict(existing)

        task_id = f"{case_id}-{int(time.time())}"
        task = {
            "task_id": task_id,
            "case_id": case_id,
            "mode": mode,
            "status": "running",
            "total": len(material_ids),
            "processed": 0,
            "completed": 0,
            "failed": 0,
            "current_material_id": None,
            "current_filename": None,
            "stop_type": None,
            "message": "识别任务已启动",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": None,
            "recent_results": [],
        }
        _ocr_tasks[case_id] = task

    with _flags_lock:
        _stop_flags.pop(case_id, None)

    worker = threading.Thread(
        target=_run_ocr_task,
        args=(case_id, material_ids, task_id),
        daemon=True,
        name=f"ocr-case-{case_id}",
    )
    worker.start()
    return task


@router.get("", response_model=List[CaseListResponse])
def list_cases(skip: int = 0, limit: int = 50, status: str = None, db: Session = Depends(get_db)):
    """获取案件列表，支持状态筛选"""
    query = db.query(Case).order_by(Case.created_at.desc())
    if status:
        if status == 'draft':
            # draft = 所有非 completed 状态
            query = query.filter(Case.status != CaseStatus.COMPLETED)
        else:
            query = query.filter(Case.status == status)
    cases = query.offset(skip).limit(limit).all()
    result = []
    for c in cases:
        item = CaseListResponse(
            id=c.id,
            case_number=c.case_number,
            entrusting_unit=c.entrusting_unit,
            status=c.status,
            status_label=get_status_label(c.status),
            person_name=c.person.name if c.person else None,
            created_at=c.created_at,
            updated_at=c.updated_at,
        )
        result.append(item)
    return result


@router.get("/{case_id}", response_model=CaseResponse)
def get_case(case_id: int, db: Session = Depends(get_db)):
    """获取案件详情"""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    # 构建响应
    person_data = None
    if case.person:
        person_data = PersonResponse(
            id=case.person.id,
            case_id=case.person.case_id,
            name=case.person.name,
            gender=case.person.gender,
            birth_date=case.person.birth_date,
            id_number=case.person.id_number,
            address=case.person.address,
        )

    materials_data = []
    for m in case.materials:
        materials_data.append({
            "id": m.id,
            "case_id": m.case_id,
            "material_type": m.material_type,
            "material_type_label": get_material_type_label(m.material_type),
            "group_id": m.group_id,
            "description": m.description,
            "page_number": m.page_number,
            "file_path": m.file_path,
            "original_filename": m.original_filename,
            "ocr_text": m.ocr_text,
            "ocr_status": m.ocr_status,
            "created_at": m.created_at,
        })

    material_groups_data = []
    for mg in case.material_groups:
        material_groups_data.append({
            "id": mg.id,
            "case_id": mg.case_id,
            "material_type": mg.material_type,
            "group_name": mg.group_name,
            "sort_order": mg.sort_order,
            "created_at": mg.created_at,
            "materials": [md for md in materials_data if md.get("group_id") == mg.id],
        })

    hospital_records_data = []
    for hr in case.hospital_records:
        hospital_records_data.append({
            "id": hr.id,
            "case_id": hr.case_id,
            "material_id": hr.material_id,
            "hospital_name": hr.hospital_name,
            "admission_number": hr.admission_number,
            "chief_complaint": hr.chief_complaint,
            "present_illness_history": hr.present_illness_history,
            "past_history": hr.past_history,
            "physical_examination": hr.physical_examination,
            "admission_diagnosis": hr.admission_diagnosis,
            "treatment_process": hr.treatment_process,
            "medication": hr.medication,
            "discharge_diagnosis": hr.discharge_diagnosis,
            "discharge_orders": hr.discharge_orders,
            "admission_date": hr.admission_date,
            "discharge_date": hr.discharge_date,
            "hospital_days": hr.hospital_days,
        })

    imaging_reports_data = []
    for ir in case.imaging_reports:
        imaging_reports_data.append({
            "id": ir.id,
            "case_id": ir.case_id,
            "material_id": ir.material_id,
            "report_date": ir.report_date,
            "hospital_name": ir.hospital_name,
            "exam_type": ir.exam_type,
            "exam_part": ir.exam_part,
            "film_number": ir.film_number,
            "film_count": ir.film_count,
            "report_content": ir.report_content,
        })

    report_data = None
    if case.report:
        report_data = {
            "id": case.report.id,
            "case_id": case.report.case_id,
            "case_facts": case.report.case_facts,
            "material_summary": case.report.material_summary,
            "appraisal_process": case.report.appraisal_process,
            "analysis": case.report.analysis,
            "opinion": case.report.opinion,
            "opinion_confirmed": case.report.opinion_confirmed,
            "generated_at": case.report.generated_at,
            "created_at": case.report.created_at,
            "updated_at": case.report.updated_at,
        }

    return CaseResponse(
        id=case.id,
        case_number=case.case_number,
        entrusting_unit=case.entrusting_unit,
        entrustment_matter=case.entrustment_matter,
        accident_date=case.accident_date,
        accident_location=case.accident_location,
        accident_description=case.accident_description,
        acceptance_date=case.acceptance_date,
        appraisal_date=case.appraisal_date,
        appraisal_location=case.appraisal_location,
        on_site_personnel=case.on_site_personnel,
        material_list=case.material_list,
        person_name=case.person_name,
        examination_date=case.examination_date,
        clinical_examination=case.clinical_examination,
        status=case.status,
        status_label=get_status_label(case.status),
        created_at=case.created_at,
        updated_at=case.updated_at,
        person=person_data,
        materials=materials_data,
        material_groups=material_groups_data,
        hospital_records=hospital_records_data,
        imaging_reports=imaging_reports_data,
        report=report_data,
    )


@router.post("", response_model=CaseResponse)
def create_case(data: CaseCreate, db: Session = Depends(get_db)):
    """创建案件"""
    case = Case(
        case_number=data.case_number,
        status=CaseStatus.PENDING_UPLOAD,
    )
    db.add(case)
    db.commit()
    db.refresh(case)
    return get_case(case.id, db)


@router.put("/{case_id}", response_model=CaseResponse)
def update_case(case_id: int, data: CaseUpdate, db: Session = Depends(get_db)):
    """更新案件"""
    import json as _json
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    update_data = data.model_dump(exclude_unset=True)

    # 如果更新了受理日期，自动更新鉴定日期
    if "acceptance_date" in update_data and update_data["acceptance_date"]:
        update_data["appraisal_date"] = update_data["acceptance_date"]

    # 标记用户确认的字段（排除 status 等非用户编辑字段）
    user_editable_fields = {
        "entrusting_unit", "entrustment_matter", "accident_date",
        "accident_location", "accident_description",
        "acceptance_date",
        "appraisal_location", "on_site_personnel", "material_list",
        "person_name", "examination_date", "clinical_examination",
    }
    confirmed = set()
    try:
        confirmed = set(_json.loads(case.confirmed_fields or "[]"))
    except (ValueError, TypeError):
        pass
    for key, value in update_data.items():
        if key in user_editable_fields and value:  # 只有非空值才标记为已确认
            confirmed.add(key)
    case.confirmed_fields = _json.dumps(list(confirmed), ensure_ascii=False)

    edited_user_fields = any(key in user_editable_fields for key in update_data)

    for key, value in update_data.items():
        setattr(case, key, value)

    if (
        edited_user_fields
        and "status" not in update_data
        and case.status in (CaseStatus.PENDING_REVIEW, CaseStatus.PENDING_CONFIRM)
    ):
        case.status = CaseStatus.REVIEWING

    # 如果 person_name 被更新，直接同步
    # 如果没有显式传 person_name 但 Person 关联存在且 name 不为空，也确保同步
    if case.person and case.person.name:
        case.person_name = case.person.name

    db.commit()
    db.refresh(case)
    return get_case(case.id, db)


@router.delete("/{case_id}")
def delete_case(case_id: int, db: Session = Depends(get_db)):
    """删除案件"""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")
    db.delete(case)
    db.commit()
    return {"message": "删除成功"}


@router.post("/{case_id}/start-recognition")
def start_recognition(case_id: int, db: Session = Depends(get_db)):
    """后台批量 OCR 识别所有材料（兼容旧入口）"""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")
    if not case.materials:
        raise HTTPException(status_code=400, detail="请先上传材料")

    material_ids = [m.id for m in case.materials]
    case.status = CaseStatus.RECOGNIZING
    db.commit()
    task = _start_ocr_background_task(case_id, material_ids, mode="all")
    return {
        "message": f"已开始后台识别，共 {len(material_ids)} 张材料",
        "status": task["status"],
        "task": _public_task_snapshot(case_id, db),
    }


@router.post("/{case_id}/stop-recognize")
def stop_recognize(case_id: int, db: Session = Depends(get_db)):
    """停止识别（等当前这张识别完再停，剩余材料保持 pending）"""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")
    with _flags_lock:
        _stop_flags[case_id] = "stop"
    _update_task(case_id, message="已请求停止，当前材料完成后停止")
    return {"message": "已发出停止请求，当前这张识别完后将停止", "case_id": case_id}


@router.post("/{case_id}/force-stop-recognize")
def force_stop_recognize(case_id: int, db: Session = Depends(get_db)):
    """强制停止识别（立刻中断，不等当前这张）"""
    import sys
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")
    with _flags_lock:
        _stop_flags[case_id] = "force"
    _update_task(case_id, message="已请求强制停止，正在收尾")
    # 将未完成的材料状态重置为 pending
    for m in case.materials:
        if m.ocr_status in (OcrStatus.PROCESSING, OcrStatus.PENDING):
            m.ocr_status = OcrStatus.PENDING
    if case.status == CaseStatus.RECOGNIZING:
        case.status = CaseStatus.PENDING_UPLOAD
    db.commit()
    return {"message": "已强制停止，进程将立刻中断", "case_id": case_id}


@router.get("/{case_id}/recognize-status")
def get_recognize_status(case_id: int, db: Session = Depends(get_db)):
    """查询当前识别状态和停止请求状态"""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")
    with _flags_lock:
        stop_flag = _stop_flags.get(case_id, None)
    return {
        "case_id": case_id,
        "case_status": str(case.status) if case.status else None,
        "stop_requested": stop_flag,
        "task": _public_task_snapshot(case_id, db),
        "counts": _material_counts(db, case_id),
    }


@router.post("/{case_id}/recognize-all")
def recognize_all_pending(case_id: int, db: Session = Depends(get_db)):
    """后台批量 OCR 识别所有待识别材料（支持增量识别）"""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    pending_materials = [m for m in case.materials if m.ocr_status in (OcrStatus.PENDING, OcrStatus.FAILED)]
    if not pending_materials:
        return {"message": "没有待识别的材料", "status": "skipped", "task": None, "counts": _material_counts(db, case_id)}

    case.status = CaseStatus.RECOGNIZING
    db.commit()
    material_ids = [m.id for m in pending_materials]
    task = _start_ocr_background_task(case_id, material_ids, mode="pending")
    return {
        "message": f"已开始后台识别，共 {len(material_ids)} 张待识别材料",
        "status": task["status"],
        "task": _public_task_snapshot(case_id, db),
    }


@router.post("/{case_id}/materials/{material_id}/recognize")
def recognize_single_material(case_id: int, material_id: int, db: Session = Depends(get_db)):
    """单张材料 OCR 识别（同步执行）"""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    material = db.query(Material).filter(
        Material.id == material_id,
        Material.case_id == case_id
    ).first()
    if not material:
        raise HTTPException(status_code=404, detail="材料不存在")

    if not material.file_path or not os.path.exists(material.file_path):
        raise HTTPException(status_code=400, detail="材料文件不存在")

    save_dir = os.path.join(str(settings.UPLOAD_DIR), str(case_id), "ocr_result")

    try:
        result = run_ocr(material.file_path, save_dir=save_dir)
        text = extract_text_from_result(result)

        if text:
            material.ocr_text = text
            material.ocr_status = OcrStatus.COMPLETED
            if result.get("md_path"):
                material.ocr_file_path = result["md_path"]
            db.commit()
            return {
                "message": f"识别完成: {material.original_filename}",
                "material_id": material_id,
                "status": "completed",
                "text_length": len(text),
                "text_preview": text[:200],
            }
        else:
            material.ocr_text = ""
            material.ocr_status = OcrStatus.FAILED
            db.commit()
            return {
                "message": f"识别失败: {material.original_filename}",
                "material_id": material_id,
                "status": "failed",
                "error": result.get("error", "无识别结果"),
            }
    except Exception as e:
        material.ocr_text = f"OCR错误: {str(e)}"
        material.ocr_status = OcrStatus.FAILED
        db.commit()
        raise HTTPException(status_code=500, detail=f"OCR识别失败: {str(e)}")


@router.post("/{case_id}/submit-review")
def submit_review(case_id: int, db: Session = Depends(get_db)):
    """提交审核（修正中 → 待确认）"""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    if case.status != CaseStatus.REVIEWING:
        raise HTTPException(status_code=400, detail="当前状态不允许提交审核")

    case.status = CaseStatus.PENDING_CONFIRM
    db.commit()
    return {"message": "已提交审核", "status": case.status}


@router.post("/{case_id}/confirm")
def confirm_case(case_id: int, db: Session = Depends(get_db)):
    """确认完成（任意状态 → 已完成）"""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    if case.status == CaseStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="案件已完成")

    # 检查鉴定意见是否已确认
    if case.report and not case.report.opinion_confirmed:
        raise HTTPException(status_code=400, detail="鉴定意见尚未确认")

    case.status = CaseStatus.COMPLETED
    db.commit()
    return {"message": "案件已完成", "status": case.status}


@router.post("/{case_id}/reopen")
def reopen_case(case_id: int, db: Session = Depends(get_db)):
    """重新打开（已完成 → 进行中）"""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    if case.status != CaseStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="当前状态不允许重新打开")

    case.status = CaseStatus.PENDING_UPLOAD
    db.commit()
    return {"message": "案件已重新打开", "status": case.status}
