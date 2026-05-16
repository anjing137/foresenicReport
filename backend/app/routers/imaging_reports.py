"""
影像学报告管理路由
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.models.case import ImagingReport, Case, Material
from app.schemas.case import ImagingReportCreate, ImagingReportUpdate, ImagingReportResponse
from app.utils.source_material import (
    material_page_payload,
    material_sequence_key,
    source_for_record,
    source_material_payload,
    source_pages_for_record,
)

router = APIRouter(prefix="/api/imaging-reports", tags=["检查报告"])


def _safe_int_list(value) -> list[int]:
    try:
        values = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(values, list):
        return []
    result = []
    for item in values:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(result))


def _report_source_pages(report: ImagingReport, db: Session) -> list[Material]:
    source_ids = _safe_int_list(report.source_material_ids)
    if source_ids:
        materials = db.query(Material).filter(Material.id.in_(source_ids)).all()
        by_id = {material.id: material for material in materials}
        return [by_id[source_id] for source_id in source_ids if source_id in by_id]
    return source_pages_for_record(db, report.material_id, report.group_id)


def _report_to_response(report: ImagingReport, db: Session) -> dict:
    """Attach source image metadata to an imaging report response."""
    data = ImagingReportResponse.model_validate(report).model_dump()
    pages = sorted(_report_source_pages(report, db), key=material_sequence_key)
    if pages:
        source_material, source_count = pages[0], len(pages)
    else:
        source_material, source_count = source_for_record(db, report.material_id, report.group_id)
    data.update(source_material_payload(source_material, source_count))
    return data


@router.get("/case/{case_id}", response_model=List[ImagingReportResponse])
def list_imaging_reports(case_id: int, db: Session = Depends(get_db)):
    """获取案件的所有影像学报告"""
    reports = db.query(ImagingReport).filter(ImagingReport.case_id == case_id).all()
    return [_report_to_response(report, db) for report in reports]


@router.get("/{report_id}", response_model=ImagingReportResponse)
def get_imaging_report(report_id: int, db: Session = Depends(get_db)):
    """获取单条影像学报告"""
    report = db.query(ImagingReport).filter(ImagingReport.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="检查报告不存在")
    return _report_to_response(report, db)


@router.get("/{report_id}/source-pages")
def get_imaging_report_source_pages(report_id: int, db: Session = Depends(get_db)):
    """获取检查报告对应的来源页。检查报告通常是一页，影像片/组合页也可返回多页。"""
    report = db.query(ImagingReport).filter(ImagingReport.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="检查报告不存在")

    pages = _report_source_pages(report, db)
    return {
        "report_id": report.id,
        "material_id": report.material_id,
        "count": len(pages),
        "pages": [material_page_payload(page) for page in pages],
    }


@router.post("", response_model=ImagingReportResponse)
def create_imaging_report(data: ImagingReportCreate, db: Session = Depends(get_db)):
    """创建影像学报告"""
    case = db.query(Case).filter(Case.id == data.case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    report = ImagingReport(**data.model_dump())
    db.add(report)
    db.commit()
    db.refresh(report)
    return _report_to_response(report, db)


@router.put("/{report_id}", response_model=ImagingReportResponse)
def update_imaging_report(report_id: int, data: ImagingReportUpdate, db: Session = Depends(get_db)):
    """更新影像学报告"""
    report = db.query(ImagingReport).filter(ImagingReport.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="检查报告不存在")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(report, key, value)

    db.commit()
    db.refresh(report)
    return _report_to_response(report, db)


@router.delete("/{report_id}")
def delete_imaging_report(report_id: int, db: Session = Depends(get_db)):
    """删除影像学报告"""
    report = db.query(ImagingReport).filter(ImagingReport.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="检查报告不存在")
    db.delete(report)
    db.commit()
    return {"message": "删除成功"}
