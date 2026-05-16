"""Helpers for exposing source material images on structured records."""
import os
import re
from typing import Optional

from sqlalchemy.orm import Session

from app.models.case import Material


PDF_PAGE_RE = re.compile(r"^(?P<prefix>.+)-(?P<number>\d+)\.(?:png|jpg|jpeg)$", re.IGNORECASE)


def material_pdf_page_info(material: Optional[Material]) -> tuple[Optional[str], Optional[int]]:
    """Return the original PDF page prefix and page number encoded in converted image names."""
    if not material:
        return None, None
    candidates = [
        material.original_filename or "",
        os.path.basename(material.file_path or ""),
        material.description or "",
    ]
    for value in candidates:
        match = PDF_PAGE_RE.match(value)
        if match:
            return match.group("prefix"), int(match.group("number"))
    return None, None


def material_original_page_number(material: Optional[Material]) -> Optional[int]:
    """Best-effort original PDF/image page number."""
    _, page_number = material_pdf_page_info(material)
    return page_number


def material_sequence_key(material: Material) -> tuple:
    """Stable source order: original PDF sequence first, then group page number."""
    prefix, original_page = material_pdf_page_info(material)
    if original_page is not None:
        return (0, prefix or "", original_page, material.id or 0)
    return (1, "", material.page_number is None, material.page_number or 0, material.id or 0)


def material_uploads_path(material: Optional[Material]) -> Optional[str]:
    """Return the public /uploads/... path for a stored material file."""
    if not material or not material.file_path:
        return None
    normalized = material.file_path.replace("\\", "/")
    marker = "/uploads/"
    idx = normalized.find(marker)
    if idx >= 0:
        return normalized[idx:]
    if normalized.startswith("uploads/"):
        return f"/{normalized}"
    return None


def source_material_payload(material: Optional[Material], source_material_count: Optional[int] = None) -> dict:
    """Build fields shared by hospital record and imaging report responses."""
    return {
        "source_material_id": material.id if material else None,
        "source_material_filename": material.original_filename if material else None,
        "source_material_description": material.description if material else None,
        "source_material_page_number": material.page_number if material else None,
        "source_material_original_page_number": material_original_page_number(material),
        "source_material_file_path": material.file_path if material else None,
        "source_material_image_url": material_uploads_path(material),
        "source_material_count": source_material_count,
    }


def material_page_payload(material: Material) -> dict:
    """Build a compact source-page payload for source-page viewers."""
    return {
        "id": material.id,
        "material_type": material.material_type,
        "material_subtype": material.material_subtype,
        "group_id": material.group_id,
        "filename": material.original_filename,
        "description": material.description,
        "page_number": material.page_number,
        "original_page_number": material_original_page_number(material),
        "file_path": material.file_path,
        "image_url": material_uploads_path(material),
        "ocr_status": material.ocr_status,
    }


def first_group_material(db: Session, group_id: Optional[int]) -> Optional[Material]:
    """Find the first page in a material group for group-level extracted records."""
    if not group_id:
        return None
    materials = db.query(Material).filter(Material.group_id == group_id).all()
    if not materials:
        return None
    return sorted(materials, key=material_sequence_key)[0]


def source_pages_for_record(db: Session, material_id: Optional[int], group_id: Optional[int] = None) -> list[Material]:
    """Resolve all source pages for a structured fact record."""
    if group_id:
        materials = db.query(Material).filter(Material.group_id == group_id).all()
        return sorted(materials, key=material_sequence_key)

    if material_id:
        material = db.query(Material).filter(Material.id == material_id).first()
        return [material] if material else []

    return []


def source_for_record(db: Session, material_id: Optional[int], group_id: Optional[int] = None) -> tuple[Optional[Material], Optional[int]]:
    """Resolve a structured record's best source material and source page count."""
    if material_id:
        material = db.query(Material).filter(Material.id == material_id).first()
        return material, 1 if material else None

    if group_id:
        count = db.query(Material).filter(Material.group_id == group_id).count()
        return first_group_material(db, group_id), count

    return None, None
