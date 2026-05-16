"""
病历事件型事实管理路由
"""
import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.models.case import MedicalEvent, Material, Case
from app.schemas.case import MedicalEventCreate, MedicalEventUpdate, MedicalEventResponse
from app.utils.source_material import material_page_payload, material_sequence_key

router = APIRouter(prefix="/api/medical-events", tags=["病历事件型事实"])

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


def _event_source_ids(event: MedicalEvent) -> list[int]:
    try:
        values = json.loads(event.source_material_ids or "[]")
    except (TypeError, json.JSONDecodeError):
        values = []
    return [int(value) for value in values if str(value).isdigit()]


def _event_sort_key(event: MedicalEvent) -> tuple:
    raw_date = event.event_date or "9999-99-99 99:99"
    return (
        raw_date[:10],
        EVENT_TYPE_ORDER.get(event.event_type or "other", 100),
        raw_date,
        event.id or 0,
    )


@router.get("/case/{case_id}", response_model=List[MedicalEventResponse])
def list_medical_events(case_id: int, db: Session = Depends(get_db)):
    """获取案件的病历事件型事实"""
    events = db.query(MedicalEvent).filter(
        MedicalEvent.case_id == case_id,
    ).all()
    return sorted(events, key=_event_sort_key)


@router.get("/{event_id}", response_model=MedicalEventResponse)
def get_medical_event(event_id: int, db: Session = Depends(get_db)):
    """获取单条病历事件型事实"""
    event = db.query(MedicalEvent).filter(MedicalEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="病历事件不存在")
    return event


@router.get("/{event_id}/source-pages")
def get_medical_event_source_pages(event_id: int, db: Session = Depends(get_db)):
    """获取病历事件对应的来源页"""
    event = db.query(MedicalEvent).filter(MedicalEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="病历事件不存在")

    ids = _event_source_ids(event)
    pages = []
    if ids:
        pages = db.query(Material).filter(Material.id.in_(ids)).all()
        pages = sorted(pages, key=material_sequence_key)

    return {
        "event_id": event.id,
        "count": len(pages),
        "pages": [material_page_payload(page) for page in pages],
    }


@router.post("", response_model=MedicalEventResponse)
def create_medical_event(data: MedicalEventCreate, db: Session = Depends(get_db)):
    """手动创建病历事件型事实"""
    case = db.query(Case).filter(Case.id == data.case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    event = MedicalEvent(**data.model_dump())
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


@router.put("/{event_id}", response_model=MedicalEventResponse)
def update_medical_event(event_id: int, data: MedicalEventUpdate, db: Session = Depends(get_db)):
    """更新病历事件型事实"""
    event = db.query(MedicalEvent).filter(MedicalEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="病历事件不存在")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(event, key, value)

    db.commit()
    db.refresh(event)
    return event


@router.delete("/{event_id}")
def delete_medical_event(event_id: int, db: Session = Depends(get_db)):
    """删除病历事件型事实"""
    event = db.query(MedicalEvent).filter(MedicalEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="病历事件不存在")
    db.delete(event)
    db.commit()
    return {"message": "删除成功"}
