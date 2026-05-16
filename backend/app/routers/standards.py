"""法医临床学标准规范库 API"""
import json
import threading
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal, get_db
from app.models.case import ProofreadStatus, StandardDocument, StandardPage
from app.utils.standards import (
    case_standard_references,
    import_standard_library,
    ocr_standard_document,
    proofread_standard_document,
    rebuild_standard_chunks_from_pages,
    search_standards,
    standard_document_progress,
)

router = APIRouter(prefix="/api/standards", tags=["规范依据库"])
_standard_ocr_task: dict | None = None
_standard_ocr_lock = threading.Lock()
_standard_proofread_task: dict | None = None
_standard_proofread_lock = threading.Lock()


def _standard_page_image_url(page: StandardPage) -> str | None:
    if not page.image_path:
        return None
    try:
        path = Path(page.image_path).resolve()
        upload_root = Path(settings.UPLOAD_DIR).resolve()
        rel_path = path.relative_to(upload_root)
        return f"/uploads/{rel_path.as_posix()}"
    except Exception:
        return None


def _parse_quality_flags(raw_flags: str | None) -> list[str]:
    if not raw_flags:
        return []
    try:
        flags = json.loads(raw_flags)
        return flags if isinstance(flags, list) else [str(flags)]
    except Exception:
        return [raw_flags]


def _standard_page_payload(page: StandardPage, document: StandardDocument, include_text: bool = True) -> dict:
    payload = {
        "id": page.id,
        "document_id": page.document_id,
        "document_title": document.title,
        "filename": document.filename,
        "page_number": page.page_number,
        "image_url": _standard_page_image_url(page),
        "ocr_status": page.ocr_status,
        "char_count": page.char_count,
        "proofread_status": page.proofread_status,
        "proofread_confidence": page.proofread_confidence,
        "proofread_notes": page.proofread_notes,
        "quality_flags": _parse_quality_flags(page.quality_flags),
        "proofread_at": page.proofread_at,
        "updated_at": page.updated_at,
    }
    if include_text:
        payload.update({
            "ocr_text": page.ocr_text or "",
            "proofread_text": page.proofread_text or "",
            "current_text": page.proofread_text or page.ocr_text or "",
        })
    return payload


def _select_pilot_documents(db: Session) -> list[int]:
    docs = db.query(StandardDocument).filter(
        StandardDocument.import_status.in_(["needs_ocr", "partial_failed", "failed"])
    ).all()
    wanted = []
    for doc in docs:
        text = f"{doc.title} {doc.filename}"
        if "误工期" in text or "护理期" in text or "营养期" in text or "护理依赖" in text:
            wanted.append(doc.id)
    return wanted


def _select_proofread_documents(db: Session) -> list[int]:
    rows = db.query(StandardPage.document_id).filter(
        StandardPage.ocr_status == "completed",
        StandardPage.ocr_text.isnot(None),
    ).distinct().all()
    return [row[0] for row in rows]


def _run_standard_ocr_task(document_ids: list[int], retry_failed: bool):
    global _standard_ocr_task
    db = SessionLocal()
    try:
        with _standard_ocr_lock:
            if _standard_ocr_task:
                _standard_ocr_task.update({
                    "status": "running",
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                    "finished_at": None,
                    "total": len(document_ids),
                    "completed_documents": 0,
                    "failed_documents": 0,
                    "current_document_id": None,
                    "current_title": None,
                    "results": [],
                    "error": None,
                })

        for document_id in document_ids:
            doc = db.query(StandardDocument).filter(StandardDocument.id == document_id).first()
            with _standard_ocr_lock:
                if _standard_ocr_task:
                    _standard_ocr_task["current_document_id"] = document_id
                    _standard_ocr_task["current_title"] = doc.title if doc else None

            try:
                result = ocr_standard_document(document_id, db, retry_failed=retry_failed)
                with _standard_ocr_lock:
                    if _standard_ocr_task:
                        _standard_ocr_task["completed_documents"] += 1
                        _standard_ocr_task.setdefault("results", []).append(result)
            except Exception as exc:
                if doc:
                    doc.import_status = "failed"
                    doc.error_message = str(exc)
                    db.commit()
                with _standard_ocr_lock:
                    if _standard_ocr_task:
                        _standard_ocr_task["failed_documents"] += 1
                        _standard_ocr_task.setdefault("results", []).append({
                            "document_id": document_id,
                            "status": "failed",
                            "error": str(exc),
                        })

        with _standard_ocr_lock:
            if _standard_ocr_task:
                _standard_ocr_task["status"] = "completed"
                _standard_ocr_task["finished_at"] = datetime.now().isoformat(timespec="seconds")
                _standard_ocr_task["current_document_id"] = None
                _standard_ocr_task["current_title"] = None
    except Exception as exc:
        with _standard_ocr_lock:
            if _standard_ocr_task:
                _standard_ocr_task["status"] = "failed"
                _standard_ocr_task["finished_at"] = datetime.now().isoformat(timespec="seconds")
                _standard_ocr_task["error"] = str(exc)
    finally:
        db.close()


def _run_standard_proofread_task(document_ids: list[int], retry_failed: bool, retry_review: bool, use_llm: bool):
    global _standard_proofread_task
    db = SessionLocal()
    try:
        with _standard_proofread_lock:
            if _standard_proofread_task:
                _standard_proofread_task.update({
                    "status": "running",
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                    "finished_at": None,
                    "total": len(document_ids),
                    "completed_documents": 0,
                    "failed_documents": 0,
                    "current_document_id": None,
                    "current_title": None,
                    "results": [],
                    "error": None,
                })

        for document_id in document_ids:
            doc = db.query(StandardDocument).filter(StandardDocument.id == document_id).first()
            with _standard_proofread_lock:
                if _standard_proofread_task:
                    _standard_proofread_task["current_document_id"] = document_id
                    _standard_proofread_task["current_title"] = doc.title if doc else None

            try:
                result = proofread_standard_document(
                    document_id,
                    db,
                    retry_failed=retry_failed,
                    retry_review=retry_review,
                    use_llm=use_llm,
                )
                with _standard_proofread_lock:
                    if _standard_proofread_task:
                        _standard_proofread_task["completed_documents"] += 1
                        _standard_proofread_task.setdefault("results", []).append(result)
            except Exception as exc:
                with _standard_proofread_lock:
                    if _standard_proofread_task:
                        _standard_proofread_task["failed_documents"] += 1
                        _standard_proofread_task.setdefault("results", []).append({
                            "document_id": document_id,
                            "status": "failed",
                            "error": str(exc),
                        })

        with _standard_proofread_lock:
            if _standard_proofread_task:
                _standard_proofread_task["status"] = "completed"
                _standard_proofread_task["finished_at"] = datetime.now().isoformat(timespec="seconds")
                _standard_proofread_task["current_document_id"] = None
                _standard_proofread_task["current_title"] = None
    except Exception as exc:
        with _standard_proofread_lock:
            if _standard_proofread_task:
                _standard_proofread_task["status"] = "failed"
                _standard_proofread_task["finished_at"] = datetime.now().isoformat(timespec="seconds")
                _standard_proofread_task["error"] = str(exc)
    finally:
        db.close()


@router.get("")
def list_standard_documents(db: Session = Depends(get_db)):
    """列出已导入的规范文档"""
    docs = db.query(StandardDocument).order_by(StandardDocument.id).all()
    return [
        {
            "id": doc.id,
            "title": doc.title,
            "filename": doc.filename,
            "file_path": doc.file_path,
            "file_type": doc.file_type,
            "page_count": doc.page_count,
            "char_count": doc.char_count,
            "chunk_count": doc.chunk_count,
            "import_status": doc.import_status,
            "needs_ocr": doc.needs_ocr,
            "error_message": doc.error_message,
            "imported_at": doc.imported_at,
            "ocr_progress": standard_document_progress(doc, db),
        }
        for doc in docs
    ]


@router.post("/import")
def import_standards(
    source_dir: str | None = Body(None),
    force: bool = Body(False),
    db: Session = Depends(get_db),
):
    """从本地目录导入规范文档。扫描版 PDF 会先标记为 needs_ocr。"""
    try:
        return import_standard_library(db, source_dir=source_dir, force=force)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"规范库导入失败: {exc}")


@router.get("/search")
def search_standard_references(
    q: str = Query(..., min_length=1),
    limit: int = Query(8, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """按关键词检索规范依据"""
    return {"query": q, "references": search_standards(db, q, limit=limit)}


@router.get("/case/{case_id}/references")
def get_case_standard_references(
    case_id: int,
    limit: int = Query(8, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """根据案件材料自动检索相关规范依据"""
    return {"case_id": case_id, "references": case_standard_references(case_id, db, limit=limit)}


@router.post("/ocr/start")
def start_standard_ocr(
    document_ids: list[int] | None = Body(None),
    pilot_only: bool = Body(True),
    retry_failed: bool = Body(True),
    db: Session = Depends(get_db),
):
    """启动扫描版规范后台OCR任务。默认只跑三期和护理依赖两个高优先级规范。"""
    global _standard_ocr_task
    with _standard_ocr_lock:
        if _standard_ocr_task and _standard_ocr_task.get("status") == "running":
            return {"message": "规范 OCR 已在后台运行", "task": dict(_standard_ocr_task)}

    if document_ids:
        target_ids = document_ids
    elif pilot_only:
        target_ids = _select_pilot_documents(db)
    else:
        target_ids = [
            doc.id for doc in db.query(StandardDocument).filter(
                StandardDocument.import_status.in_(["needs_ocr", "partial_failed", "failed"])
            ).order_by(StandardDocument.id).all()
        ]

    if not target_ids:
        return {"message": "没有需要OCR的规范文档", "task": None}

    docs = db.query(StandardDocument).filter(StandardDocument.id.in_(target_ids)).all()
    found_ids = {doc.id for doc in docs}
    missing = [doc_id for doc_id in target_ids if doc_id not in found_ids]
    if missing:
        raise HTTPException(status_code=404, detail=f"规范文档不存在: {missing}")

    with _standard_ocr_lock:
        _standard_ocr_task = {
            "status": "queued",
            "document_ids": target_ids,
            "total": len(target_ids),
            "completed_documents": 0,
            "failed_documents": 0,
            "current_document_id": None,
            "current_title": None,
            "started_at": None,
            "finished_at": None,
            "results": [],
            "error": None,
        }

    thread = threading.Thread(
        target=_run_standard_ocr_task,
        args=(target_ids, retry_failed),
        daemon=True,
    )
    thread.start()

    return {
        "message": f"已启动规范 OCR 后台任务，共 {len(target_ids)} 个文档",
        "task": dict(_standard_ocr_task),
        "documents": [
            {"id": doc.id, "title": doc.title, "filename": doc.filename, "page_count": doc.page_count}
            for doc in docs
        ],
    }


@router.get("/ocr/status")
def get_standard_ocr_status(db: Session = Depends(get_db)):
    """查看规范OCR后台任务和各文档页级进度"""
    with _standard_ocr_lock:
        task = dict(_standard_ocr_task) if _standard_ocr_task else None
    docs = db.query(StandardDocument).order_by(StandardDocument.id).all()
    return {
        "task": task,
        "documents": [standard_document_progress(doc, db) for doc in docs],
    }


@router.post("/proofread/start")
def start_standard_proofread(
    document_ids: list[int] | None = Body(None),
    retry_failed: bool = Body(True),
    retry_review: bool = Body(False),
    use_llm: bool = Body(True),
    db: Session = Depends(get_db),
):
    """启动规范 OCR 文本后台校对任务。默认校对所有已有页级OCR结果。"""
    global _standard_proofread_task
    with _standard_proofread_lock:
        if _standard_proofread_task and _standard_proofread_task.get("status") == "running":
            return {"message": "规范 OCR 校对已在后台运行", "task": dict(_standard_proofread_task)}

    target_ids = document_ids or _select_proofread_documents(db)
    if not target_ids:
        return {"message": "没有可校对的规范 OCR 结果", "task": None}

    docs = db.query(StandardDocument).filter(StandardDocument.id.in_(target_ids)).all()
    found_ids = {doc.id for doc in docs}
    missing = [doc_id for doc_id in target_ids if doc_id not in found_ids]
    if missing:
        raise HTTPException(status_code=404, detail=f"规范文档不存在: {missing}")

    with _standard_proofread_lock:
        _standard_proofread_task = {
            "status": "queued",
            "document_ids": target_ids,
            "total": len(target_ids),
            "completed_documents": 0,
            "failed_documents": 0,
            "current_document_id": None,
            "current_title": None,
            "started_at": None,
            "finished_at": None,
            "results": [],
            "error": None,
            "use_llm": use_llm,
        }

    thread = threading.Thread(
        target=_run_standard_proofread_task,
        args=(target_ids, retry_failed, retry_review, use_llm),
        daemon=True,
    )
    thread.start()

    return {
        "message": f"已启动规范 OCR 校对后台任务，共 {len(target_ids)} 个文档",
        "task": dict(_standard_proofread_task),
        "documents": [
            {"id": doc.id, "title": doc.title, "filename": doc.filename, "page_count": doc.page_count}
            for doc in docs
        ],
    }


@router.get("/proofread/status")
def get_standard_proofread_status(db: Session = Depends(get_db)):
    """查看规范 OCR 校对后台任务和各文档校对进度"""
    with _standard_proofread_lock:
        task = dict(_standard_proofread_task) if _standard_proofread_task else None
    docs = db.query(StandardDocument).order_by(StandardDocument.id).all()
    return {
        "task": task,
        "documents": [standard_document_progress(doc, db) for doc in docs],
    }


@router.get("/review-pages")
def list_standard_review_pages(
    status: str = Query(ProofreadStatus.NEEDS_REVIEW, description="needs_review/completed/failed/pending/processing/all"),
    include_text: bool = Query(True),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """列出需要人工核验的规范页。默认只返回 needs_review。"""
    if status != "all" and status not in ProofreadStatus.ALL:
        raise HTTPException(status_code=400, detail="不支持的校对状态")

    query = db.query(StandardPage, StandardDocument).join(
        StandardDocument, StandardDocument.id == StandardPage.document_id
    )
    if status != "all":
        query = query.filter(StandardPage.proofread_status == status)

    rows = query.order_by(StandardPage.document_id, StandardPage.page_number).limit(limit).all()

    status_rows = db.query(StandardPage.proofread_status).all()
    status_counts = {key: 0 for key in ProofreadStatus.ALL}
    for row in status_rows:
        key = row[0] or ProofreadStatus.PENDING
        status_counts[key] = status_counts.get(key, 0) + 1

    return {
        "status": status,
        "total": len(rows),
        "status_counts": status_counts,
        "pages": [
            _standard_page_payload(page, document, include_text=include_text)
            for page, document in rows
        ],
    }


@router.put("/pages/{page_id}/proofread")
def update_standard_page_proofread(
    page_id: int,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
):
    """保存人工核验后的规范页文本，并重建该文档的检索索引。"""
    page = db.query(StandardPage).filter(StandardPage.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="规范页面不存在")
    document = db.query(StandardDocument).filter(StandardDocument.id == page.document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="规范文档不存在")

    proofread_status = payload.get("proofread_status") or ProofreadStatus.COMPLETED
    if proofread_status not in ProofreadStatus.ALL:
        raise HTTPException(status_code=400, detail="不支持的校对状态")

    if "proofread_text" in payload:
        page.proofread_text = payload.get("proofread_text") or ""
        page.char_count = len(page.proofread_text or page.ocr_text or "")
    page.proofread_status = proofread_status
    if "proofread_confidence" in payload:
        page.proofread_confidence = payload.get("proofread_confidence")
    elif proofread_status == ProofreadStatus.COMPLETED:
        page.proofread_confidence = max(page.proofread_confidence or 0, 95)
    if "proofread_notes" in payload:
        page.proofread_notes = payload.get("proofread_notes")
    elif proofread_status == ProofreadStatus.COMPLETED:
        page.proofread_notes = "人工核验通过"
    page.proofread_at = datetime.now()
    db.commit()

    rebuild = rebuild_standard_chunks_from_pages(document, db)
    db.refresh(page)
    return {
        "message": "规范页核验结果已保存",
        "page": _standard_page_payload(page, document, include_text=True),
        "rebuild": rebuild,
    }


@router.get("/{document_id}/pages")
def list_standard_pages(document_id: int, db: Session = Depends(get_db)):
    """列出某规范文档的页级OCR缓存"""
    doc = db.query(StandardDocument).filter(StandardDocument.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="规范文档不存在")
    pages = db.query(StandardPage).filter(
        StandardPage.document_id == document_id
    ).order_by(StandardPage.page_number).all()
    return {
        "document": standard_document_progress(doc, db),
        "pages": [
            {
                "id": page.id,
                "document_id": page.document_id,
                "page_number": page.page_number,
                "image_path": page.image_path,
                "ocr_status": page.ocr_status,
                "char_count": page.char_count,
                "error_message": page.error_message,
                "proofread_status": page.proofread_status,
                "proofread_confidence": page.proofread_confidence,
                "proofread_notes": page.proofread_notes,
                "quality_flags": page.quality_flags,
                "proofread_at": page.proofread_at,
                "updated_at": page.updated_at,
            }
            for page in pages
        ],
    }
