"""
住院记录管理路由
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.models.case import HospitalRecord, Case
from app.schemas.case import HospitalRecordCreate, HospitalRecordUpdate, HospitalRecordResponse
from app.utils.source_material import (
    material_page_payload,
    source_for_record,
    source_material_payload,
    source_pages_for_record,
)

router = APIRouter(prefix="/api/hospital-records", tags=["住院记录"])


def _record_to_response(record: HospitalRecord, db: Session) -> dict:
    """Attach source image metadata to a hospital record response."""
    data = HospitalRecordResponse.model_validate(record).model_dump()
    source_material, source_count = source_for_record(db, record.material_id, record.group_id)
    data.update(source_material_payload(source_material, source_count))
    return data


@router.get("/case/{case_id}", response_model=List[HospitalRecordResponse])
def list_hospital_records(case_id: int, db: Session = Depends(get_db)):
    """获取案件的所有住院记录"""
    records = db.query(HospitalRecord).filter(HospitalRecord.case_id == case_id).all()
    return [_record_to_response(record, db) for record in records]


@router.get("/{record_id}", response_model=HospitalRecordResponse)
def get_hospital_record(record_id: int, db: Session = Depends(get_db)):
    """获取单条住院记录"""
    record = db.query(HospitalRecord).filter(HospitalRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="住院记录不存在")
    return _record_to_response(record, db)


@router.get("/{record_id}/source-pages")
def get_hospital_record_source_pages(record_id: int, db: Session = Depends(get_db)):
    """获取住院记录对应的全部来源页。按医院分组提取的记录通常对应多页。"""
    record = db.query(HospitalRecord).filter(HospitalRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="住院记录不存在")

    pages = source_pages_for_record(db, record.material_id, record.group_id)
    return {
        "record_id": record.id,
        "group_id": record.group_id,
        "material_id": record.material_id,
        "count": len(pages),
        "pages": [material_page_payload(page) for page in pages],
    }


@router.post("", response_model=HospitalRecordResponse)
def create_hospital_record(data: HospitalRecordCreate, db: Session = Depends(get_db)):
    """创建住院记录"""
    case = db.query(Case).filter(Case.id == data.case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    record = HospitalRecord(**data.model_dump())
    db.add(record)
    db.commit()
    db.refresh(record)
    return _record_to_response(record, db)


@router.put("/{record_id}", response_model=HospitalRecordResponse)
def update_hospital_record(record_id: int, data: HospitalRecordUpdate, db: Session = Depends(get_db)):
    """更新住院记录"""
    record = db.query(HospitalRecord).filter(HospitalRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="住院记录不存在")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(record, key, value)

    db.commit()
    db.refresh(record)
    return _record_to_response(record, db)


@router.delete("/{record_id}")
def delete_hospital_record(record_id: int, db: Session = Depends(get_db)):
    """删除住院记录"""
    record = db.query(HospitalRecord).filter(HospitalRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="住院记录不存在")
    db.delete(record)
    db.commit()
    return {"message": "删除成功"}
