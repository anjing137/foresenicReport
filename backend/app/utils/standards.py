"""Forensic clinical standards library import and retrieval."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile
import json
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.case import (
    Case,
    HospitalRecord,
    ImagingReport,
    Person,
    ProofreadStatus,
    StandardChunk,
    StandardDocument,
    StandardPage,
)
from app.models.case import OcrStatus
from app.utils.llm import call_llm_json_harness
from app.utils.ocr import extract_text_from_result, run_ocr


SUPPORTED_EXTS = {".pdf", ".docx"}
SCAN_TEXT_THRESHOLD_PER_PAGE = 30
MAX_CHUNK_CHARS = 1200
MIN_CHUNK_CHARS = 80
STANDARD_OCR_DPI = 200
STANDARD_MAX_IMAGE_DIMENSION = 2500
STANDARD_PROOFREAD_MAX_CHARS = 6000
STANDARD_OCR_PAGE_TIMEOUT = 90

KEYWORD_BANK = [
    "伤残", "残疾", "致残", "损伤程度", "轻伤", "重伤", "轻微伤",
    "误工期", "护理期", "营养期", "护理依赖", "后续治疗", "医疗损害",
    "因果关系", "外伤参与度", "关节活动度", "视觉", "视力", "听觉", "听力",
    "周围神经", "神经功能", "癫痫", "影像学", "X线", "DR", "CT", "MRI",
    "骨折", "内固定", "取出内固定", "颅脑", "脑挫裂伤", "软化灶",
    "肋骨", "锁骨", "肩关节", "肘关节", "腕关节", "髋关节", "膝关节", "踝关节",
]

DOC_BOOST_RULES = [
    (("伤残", "致残", "残疾", "十级", "九级", "八级"), ("人体损伤致残程度分级",)),
    (("误工", "护理期", "营养期", "三期"), ("误工期", "护理期", "营养期")),
    (("护理依赖",), ("护理依赖",)),
    (("因果", "参与度"), ("因果关系",)),
    (("损伤程度", "轻伤", "重伤", "轻微伤"), ("人体损伤程度鉴定标准",)),
    (("影像", "CT", "MRI", "DR", "X线"), ("影像学检验", "法医影像学")),
    (("关节", "活动度"), ("关节活动度",)),
    (("听觉", "听力"), ("听觉功能障碍",)),
    (("视觉", "视力"), ("视觉功能障碍",)),
    (("神经", "周围神经"), ("周围神经功能障碍",)),
    (("癫痫",), ("外伤性癫痫",)),
    (("劳动能力", "工伤", "职业病"), ("劳动能力鉴定", "职工工伤")),
]


def normalize_standard_title(path: Path) -> str:
    title = path.stem.replace("+", " ").strip()
    title = re.sub(r"^\d+\.\s*", "", title)
    title = re.sub(r"\s*1$", "", title)
    title = title.replace("_", " ")
    return title.strip()


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def find_poppler_tool(name: str) -> Optional[str]:
    candidates = [
        shutil.which(name),
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
        f"/usr/bin/{name}",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def find_pdftoppm() -> Optional[str]:
    return find_poppler_tool("pdftoppm")


def pdf_page_count(path: Path) -> int:
    pdfinfo = find_poppler_tool("pdfinfo")
    if not pdfinfo:
        return 0
    try:
        result = _run([pdfinfo, str(path)], timeout=30)
    except Exception:
        return 0
    if result.returncode != 0:
        return 0
    match = re.search(r"^Pages:\s+(\d+)", result.stdout, flags=re.MULTILINE)
    return int(match.group(1)) if match else 0


def pdf_max_page_dimension(path: Path) -> float:
    pdfinfo = find_poppler_tool("pdfinfo")
    if not pdfinfo:
        return 0
    try:
        result = _run([pdfinfo, str(path)], timeout=30)
    except Exception:
        return 0
    if result.returncode != 0:
        return 0
    match = re.search(r"^Page size:\s+([0-9.]+)\s+x\s+([0-9.]+)\s+pts", result.stdout, flags=re.MULTILINE)
    if not match:
        return 0
    return max(float(match.group(1)), float(match.group(2)))


def adaptive_pdf_dpi(path: Path, default_dpi: int = STANDARD_OCR_DPI) -> int:
    max_pts = pdf_max_page_dimension(path)
    if not max_pts:
        return default_dpi
    capped = int((STANDARD_MAX_IMAGE_DIMENSION * 72) / max_pts)
    return max(30, min(default_dpi, capped))


def extract_pdf_text(path: Path) -> str:
    pdftotext = find_poppler_tool("pdftotext")
    if not pdftotext:
        raise RuntimeError("未找到 pdftotext，请安装 poppler 或确认 /opt/homebrew/bin/pdftotext 存在")
    result = _run([pdftotext, "-layout", str(path), "-"], timeout=180)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "pdftotext failed").strip())
    return result.stdout or ""


def extract_docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        xml_bytes = zf.read("word/document.xml")
    root = ET.fromstring(xml_bytes)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = []
    for para in root.findall(".//w:p", ns):
        texts = [node.text for node in para.findall(".//w:t", ns) if node.text]
        if texts:
            paragraphs.append("".join(texts))
    return "\n".join(paragraphs)


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"^[—\-－]\s*\d+\s*[—\-－]\s*$", "", text, flags=re.MULTILINE)
    return text.strip()


def clean_standard_ocr_artifacts(text: str) -> str:
    """Remove OCR markdown/image artifacts while preserving standard clauses."""
    text = clean_text(text or "")
    text = re.sub(r"<div[^>]*>\s*!\[[^\]]*\]\([^)]+\)\s*</div>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    text = text.replace("☐", "").replace("☑", "").replace("🔄", "")
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def standard_ocr_quality_flags(text: str) -> list[str]:
    flags = []
    raw = text or ""
    cleaned = clean_standard_ocr_artifacts(raw)
    if len(cleaned) < 80:
        flags.append("文字过少")
    if re.search(r"<div|!\[|\]\(|☐|☑|🔄", raw, flags=re.IGNORECASE):
        flags.append("存在图片或复选框残留")
    if "�" in raw:
        flags.append("存在替换字符")
    chinese_chars = re.findall(r"[\u4e00-\u9fff]", cleaned)
    if cleaned and len(chinese_chars) / max(len(cleaned), 1) < 0.2:
        flags.append("中文占比偏低")
    if re.search(r"[0-9]\s+[0-9]\s+[0-9]\s+[0-9]", cleaned):
        flags.append("疑似表格识别错位")
    return flags


def usable_standard_page_text(page: StandardPage) -> str:
    if (
        page.proofread_text
        and page.proofread_status in [ProofreadStatus.COMPLETED, ProofreadStatus.NEEDS_REVIEW]
    ):
        return page.proofread_text
    return page.ocr_text or ""


def _is_heading(line: str) -> Optional[tuple[str, str]]:
    line = line.strip()
    if not line or len(line) > 120:
        return None
    match = re.match(r"^(\d+(?:\.\d+){0,5})(?:\s+|　+)(.+)$", line)
    if match and len(match.group(2).strip()) <= 90:
        return match.group(1), match.group(2).strip()
    match = re.match(r"^(附录\s*[A-Za-zＡ-ＺA-Z一二三四五六七八九十]+|第[一二三四五六七八九十0-9]+[章节条])\s*(.+)?$", line)
    if match:
        return match.group(1), (match.group(2) or "").strip()
    return None


def split_standard_chunks(standard_name: str, text: str) -> list[dict]:
    lines = [line.strip() for line in clean_text(text).splitlines()]
    chunks: list[dict] = []
    current_code = ""
    current_title = ""
    buffer: list[str] = []

    def flush():
        nonlocal buffer
        content = "\n".join(line for line in buffer if line).strip()
        if len(content) >= MIN_CHUNK_CHARS:
            chunks.append({
                "standard_name": standard_name,
                "section_code": current_code or None,
                "section_title": current_title or None,
                "chunk_text": content[:MAX_CHUNK_CHARS * 2],
                "keywords": ",".join(extract_keywords(f"{standard_name}\n{current_title}\n{content}")),
            })
        buffer = []

    for line in lines:
        if not line:
            if buffer and len("\n".join(buffer)) > MAX_CHUNK_CHARS:
                flush()
            continue
        heading = _is_heading(line)
        if heading and buffer:
            flush()
            current_code, current_title = heading
            buffer = [line]
            continue
        if heading and not buffer:
            current_code, current_title = heading
        buffer.append(line)
        if len("\n".join(buffer)) >= MAX_CHUNK_CHARS:
            flush()

    flush()

    if not chunks and text.strip():
        raw = clean_text(text)
        for idx in range(0, len(raw), MAX_CHUNK_CHARS):
            content = raw[idx:idx + MAX_CHUNK_CHARS].strip()
            if len(content) >= MIN_CHUNK_CHARS:
                chunks.append({
                    "standard_name": standard_name,
                    "section_code": None,
                    "section_title": None,
                    "chunk_text": content,
                    "keywords": ",".join(extract_keywords(f"{standard_name}\n{content}")),
                })
    return chunks


def extract_keywords(text: str) -> list[str]:
    found = []
    for keyword in KEYWORD_BANK:
        if keyword.lower() in text.lower():
            found.append(keyword)
    return found[:30]


def iter_standard_files(source_dir: Path) -> Iterable[Path]:
    for path in sorted(source_dir.iterdir(), key=lambda p: p.name):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTS and not path.name.startswith("~$"):
            yield path


def standard_document_protection_status(document: StandardDocument, db: Session) -> dict:
    """Return whether a standard document has OCR/proofread/index work that must not be overwritten."""
    page_count = db.query(StandardPage).filter(StandardPage.document_id == document.id).count()
    ocr_completed = db.query(StandardPage).filter(
        StandardPage.document_id == document.id,
        StandardPage.ocr_status == OcrStatus.COMPLETED,
    ).count()
    proofread_count = db.query(StandardPage).filter(
        StandardPage.document_id == document.id,
        StandardPage.proofread_text.isnot(None),
        StandardPage.proofread_text != "",
    ).count()
    proofread_confirmed = db.query(StandardPage).filter(
        StandardPage.document_id == document.id,
        StandardPage.proofread_status.in_([ProofreadStatus.COMPLETED, ProofreadStatus.NEEDS_REVIEW]),
    ).count()
    chunk_count = db.query(StandardChunk).filter(StandardChunk.document_id == document.id).count()
    return {
        "protected": bool(proofread_count or proofread_confirmed or ocr_completed or chunk_count),
        "page_count": page_count,
        "ocr_completed": ocr_completed,
        "proofread_count": proofread_count,
        "proofread_confirmed": proofread_confirmed,
        "chunk_count": chunk_count,
    }


def import_standard_file(path: Path, db: Session, force: bool = False) -> dict:
    existing = db.query(StandardDocument).filter(StandardDocument.file_path == str(path)).first()
    if not existing:
        existing = db.query(StandardDocument).filter(StandardDocument.filename == path.name).first()
    if existing:
        protection = standard_document_protection_status(existing, db)
        if protection["protected"]:
            return {
                "filename": path.name,
                "status": "protected",
                "document_id": existing.id,
                "chunk_count": existing.chunk_count,
                "message": "已存在 OCR、人工核验或检索索引，已保护并跳过导入",
                **protection,
            }
    if existing and not force:
        return {
            "filename": path.name,
            "status": "skipped",
            "document_id": existing.id,
            "chunk_count": existing.chunk_count,
        }
    if existing:
        db.delete(existing)
        db.commit()

    title = normalize_standard_title(path)
    ext = path.suffix.lower().lstrip(".")
    page_count = pdf_page_count(path) if ext == "pdf" else 0

    doc = StandardDocument(
        title=title,
        filename=path.name,
        file_path=str(path),
        file_type=ext,
        page_count=page_count,
        imported_at=datetime.now(),
    )
    db.add(doc)
    db.flush()

    try:
        if ext == "pdf":
            text = extract_pdf_text(path)
        elif ext == "docx":
            text = extract_docx_text(path)
        else:
            raise RuntimeError(f"不支持的文件类型: {path.suffix}")

        text = clean_text(text)
        doc.char_count = len(text)
        doc.needs_ocr = ext == "pdf" and page_count > 0 and len(text) < page_count * SCAN_TEXT_THRESHOLD_PER_PAGE

        if doc.needs_ocr:
            doc.import_status = "needs_ocr"
            doc.chunk_count = 0
            db.commit()
            return {
                "filename": path.name,
                "status": "needs_ocr",
                "document_id": doc.id,
                "page_count": page_count,
                "char_count": doc.char_count,
                "chunk_count": 0,
            }

        chunks = split_standard_chunks(title, text)
        for chunk in chunks:
            db.add(StandardChunk(document_id=doc.id, **chunk))
        doc.chunk_count = len(chunks)
        doc.import_status = "imported" if chunks else "failed"
        if not chunks:
            doc.error_message = "未能切分出有效条款"
        db.commit()
        return {
            "filename": path.name,
            "status": doc.import_status,
            "document_id": doc.id,
            "page_count": page_count,
            "char_count": doc.char_count,
            "chunk_count": doc.chunk_count,
        }
    except Exception as exc:
        doc.import_status = "failed"
        doc.error_message = str(exc)
        db.commit()
        return {
            "filename": path.name,
            "status": "failed",
            "document_id": doc.id,
            "error": str(exc),
        }


def import_standard_library(db: Session, source_dir: Optional[str] = None, force: bool = False) -> dict:
    root = Path(source_dir or settings.STANDARD_LIBRARY_DIR).expanduser()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"规范库目录不存在: {root}")

    results = []
    for path in iter_standard_files(root):
        results.append(import_standard_file(path, db, force=force))

    return {
        "source_dir": str(root),
        "total": len(results),
        "imported": sum(1 for r in results if r["status"] == "imported"),
        "needs_ocr": sum(1 for r in results if r["status"] == "needs_ocr"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "protected": sum(1 for r in results if r["status"] == "protected"),
        "results": results,
    }


def standard_upload_root(document_id: int) -> Path:
    root = settings.UPLOAD_DIR / "standards" / str(document_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def ensure_standard_pages(document: StandardDocument, db: Session, dpi: int = STANDARD_OCR_DPI) -> list[StandardPage]:
    """Convert a scanned PDF standard into page images and ensure page rows exist."""
    existing_pages = db.query(StandardPage).filter(
        StandardPage.document_id == document.id
    ).order_by(StandardPage.page_number).all()
    if (
        existing_pages
        and len(existing_pages) >= (document.page_count or len(existing_pages))
        and all(page.image_path and os.path.exists(page.image_path) for page in existing_pages)
    ):
        return existing_pages

    if document.file_type != "pdf":
        raise ValueError("只有PDF规范文档需要页级OCR")
    if not document.file_path or not os.path.exists(document.file_path):
        raise FileNotFoundError(f"规范文件不存在: {document.file_path}")
    if not document.page_count:
        document.page_count = pdf_page_count(Path(document.file_path))
        db.commit()

    pdftoppm = find_pdftoppm()
    if not pdftoppm:
        raise RuntimeError("未找到 pdftoppm，请安装 poppler 或确认 /opt/homebrew/bin/pdftoppm 存在")

    pages_dir = standard_upload_root(document.id) / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    prefix = pages_dir / "page"

    image_paths = sorted(pages_dir.glob("page-*.png"))
    if len(image_paths) < (document.page_count or 1):
        for old in image_paths:
            old.unlink(missing_ok=True)
        db.query(StandardPage).filter(StandardPage.document_id == document.id).delete()
        db.commit()
        existing_pages = []
        dpi = adaptive_pdf_dpi(Path(document.file_path), default_dpi=dpi)
        result = _run([
            pdftoppm,
            "-png",
            "-r", str(dpi),
            document.file_path,
            str(prefix),
        ], timeout=max(600, (document.page_count or 1) * 30))
        if result.returncode != 0:
            raise RuntimeError(f"规范PDF转图片失败: {result.stderr}")

    image_paths = sorted(
        pages_dir.glob("page-*.png"),
        key=lambda p: int(re.search(r"-(\d+)\.png$", p.name).group(1)) if re.search(r"-(\d+)\.png$", p.name) else 0,
    )
    if not image_paths:
        raise RuntimeError("规范PDF转图片后未生成页面图片")

    existing_by_page = {page.page_number: page for page in existing_pages}
    for idx, image_path in enumerate(image_paths, 1):
        page = existing_by_page.get(idx)
        if page:
            page.image_path = str(image_path)
        else:
            db.add(StandardPage(
                document_id=document.id,
                page_number=idx,
                image_path=str(image_path),
                ocr_status=OcrStatus.PENDING,
                proofread_status=ProofreadStatus.PENDING,
            ))
    document.page_count = document.page_count or len(image_paths)
    db.commit()

    return db.query(StandardPage).filter(
        StandardPage.document_id == document.id
    ).order_by(StandardPage.page_number).all()


def rebuild_standard_chunks_from_pages(document: StandardDocument, db: Session) -> dict:
    pages = db.query(StandardPage).filter(
        StandardPage.document_id == document.id,
        StandardPage.ocr_status == OcrStatus.COMPLETED,
        StandardPage.ocr_text.isnot(None),
    ).order_by(StandardPage.page_number).all()
    full_text = "\n\n".join(usable_standard_page_text(page) for page in pages if usable_standard_page_text(page))

    db.query(StandardChunk).filter(StandardChunk.document_id == document.id).delete()
    chunks = split_standard_chunks(document.title, full_text)
    for chunk in chunks:
        db.add(StandardChunk(document_id=document.id, **chunk))

    failed_count = db.query(StandardPage).filter(
        StandardPage.document_id == document.id,
        StandardPage.ocr_status == OcrStatus.FAILED,
    ).count()
    pending_count = db.query(StandardPage).filter(
        StandardPage.document_id == document.id,
        StandardPage.ocr_status.in_([OcrStatus.PENDING, OcrStatus.PROCESSING]),
    ).count()

    document.char_count = len(full_text)
    document.chunk_count = len(chunks)
    document.needs_ocr = pending_count > 0 or failed_count > 0 or not chunks
    if chunks and not failed_count and not pending_count:
        document.import_status = "imported"
        document.error_message = None
    elif chunks:
        document.import_status = "partial_failed"
        document.error_message = f"部分页面未完成：失败 {failed_count} 页，待识别 {pending_count} 页"
    else:
        document.import_status = "needs_ocr" if pending_count else "failed"
        document.error_message = document.error_message or "OCR后未能生成有效规范条款"
    document.imported_at = datetime.now()
    db.commit()
    return {
        "document_id": document.id,
        "char_count": document.char_count,
        "chunk_count": document.chunk_count,
        "failed_count": failed_count,
        "pending_count": pending_count,
        "import_status": document.import_status,
    }


def ocr_standard_document(document_id: int, db: Session, retry_failed: bool = True) -> dict:
    """OCR a scanned standard PDF page by page and rebuild searchable chunks."""
    document = db.query(StandardDocument).filter(StandardDocument.id == document_id).first()
    if not document:
        raise ValueError(f"规范文档不存在: {document_id}")

    pages = ensure_standard_pages(document, db)
    document.import_status = "ocr_processing"
    document.error_message = None
    db.commit()

    statuses = [OcrStatus.PENDING, OcrStatus.PROCESSING]
    if retry_failed:
        statuses.append(OcrStatus.FAILED)

    target_pages = db.query(StandardPage).filter(
        StandardPage.document_id == document.id,
        StandardPage.ocr_status.in_(statuses),
    ).order_by(StandardPage.page_number).all()

    completed = 0
    failed = 0
    skipped = len(pages) - len(target_pages)
    save_dir = standard_upload_root(document.id) / "ocr_result"

    for page in target_pages:
        if not page.image_path or not os.path.exists(page.image_path):
            page.ocr_status = OcrStatus.FAILED
            page.error_message = "页面图片不存在"
            failed += 1
            db.commit()
            continue

        page.ocr_status = OcrStatus.PROCESSING
        page.error_message = None
        db.commit()

        try:
            result = run_standard_page_ocr(page.image_path, save_dir=str(save_dir))
            text = extract_text_from_result(result)
            if text:
                page.ocr_text = clean_text(text)
                page.char_count = len(page.ocr_text)
                page.ocr_status = OcrStatus.COMPLETED
                page.proofread_status = ProofreadStatus.PENDING
                page.proofread_text = None
                page.proofread_confidence = None
                page.proofread_notes = None
                page.quality_flags = None
                page.proofread_at = None
                page.error_message = None
                completed += 1
            else:
                page.ocr_status = OcrStatus.FAILED
                page.error_message = result.get("error", "OCR未返回文本")
                failed += 1
            db.commit()
        except Exception as exc:
            page.ocr_status = OcrStatus.FAILED
            page.error_message = str(exc)
            failed += 1
            db.commit()

    rebuild = rebuild_standard_chunks_from_pages(document, db)
    return {
        "document_id": document.id,
        "title": document.title,
        "total_pages": len(pages),
        "target_pages": len(target_pages),
        "completed": completed,
        "failed": failed,
        "skipped": skipped,
        **rebuild,
    }


def run_standard_page_ocr(image_path: str, save_dir: str) -> dict:
    """Run OCR for one standard page in a child process so hung API calls cannot block the batch."""
    backend_root = str(settings.UPLOAD_DIR.parent)
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{backend_root}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    script = """
import json
import sys
from app.utils.ocr import run_ocr

result = run_ocr(sys.argv[1], save_dir=sys.argv[2] or None)
safe = {
    "success": result.get("success"),
    "text": result.get("text", ""),
    "error": result.get("error"),
    "md_path": result.get("md_path"),
    "cropped_images": result.get("cropped_images", []),
}
print(json.dumps(safe, ensure_ascii=False))
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, image_path, save_dir or ""],
            capture_output=True,
            text=True,
            timeout=STANDARD_OCR_PAGE_TIMEOUT,
            env=env,
            cwd=backend_root,
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": f"OCR单页超时（>{STANDARD_OCR_PAGE_TIMEOUT}秒）",
        }
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return {
            "success": False,
            "error": f"OCR子进程失败: {detail[:500]}",
        }
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except Exception:
        return {
            "success": False,
            "error": f"OCR子进程返回无法解析: {(result.stdout or result.stderr)[:500]}",
        }


def _proofread_standard_page_with_llm(document: StandardDocument, page: StandardPage, cleaned_text: str) -> dict:
    system_prompt = """你是法医临床学标准规范 OCR 校对助手。
你的任务是对扫描规范的 OCR 文本做保守校对。

规则：
1. 只能修正明显的 OCR 识别错误、乱码、错别字、空格和换行问题。
2. 必须保留原有条款号、标准号、数值、日期、范围、单位、括号内容和医学/法医学术语。
3. 不得根据常识补写原文没有的条款，不得扩写、解释或改写规范含义。
4. 对无法确定的内容保持原样，并在 flags 或 notes 中说明。
5. 删除 OCR 产生的图片标记、HTML 标记、复选框符号等非规范正文。
6. 输出必须是严格 JSON，不要输出 Markdown。"""

    instructions = f"""请校对以下 OCR 文本。

规范名称：{document.title}
页码：第 {page.page_number} 页

请输出 JSON：
{{
  "status": "completed 或 needs_review",
  "confidence": 0-100,
  "corrected_text": "校对后的完整文本",
  "flags": ["可疑点1", "可疑点2"],
  "notes": "简短说明"
}}"""

    result = call_llm_json_harness(
        task_name="proofread_standard_page",
        system_prompt=system_prompt,
        instructions=instructions,
        input_text=cleaned_text,
        output_schema="""{
  "status": "completed 或 needs_review",
  "confidence": 0-100,
  "corrected_text": "校对后的完整文本",
  "flags": ["可疑点1", "可疑点2"],
  "notes": "简短说明"
}""",
        required_fields=("status", "confidence", "corrected_text", "flags", "notes"),
        temperature=0,
        max_tokens=4096,
        max_input_chars=STANDARD_PROOFREAD_MAX_CHARS,
        max_retries=1,
    )
    if not result.get("success"):
        return {
            "success": False,
            "error": result.get("error", "LLM 校对失败"),
        }
    return {"success": True, "data": result.get("data") or {}, "harness": result.get("harness", {})}


def proofread_standard_page(document: StandardDocument, page: StandardPage, db: Session, use_llm: bool = True) -> dict:
    raw_text = page.ocr_text or ""
    cleaned_text = clean_standard_ocr_artifacts(raw_text)
    flags = standard_ocr_quality_flags(raw_text)
    status = ProofreadStatus.COMPLETED
    confidence = 92 if not flags else 78
    notes = "规则清理完成"
    corrected_text = cleaned_text

    if use_llm and cleaned_text and len(cleaned_text) <= STANDARD_PROOFREAD_MAX_CHARS:
        llm_result = _proofread_standard_page_with_llm(document, page, cleaned_text)
        if llm_result.get("success"):
            data = llm_result["data"]
            llm_text = clean_text(str(data.get("corrected_text") or "")).strip()
            if llm_text and len(llm_text) >= max(40, int(len(cleaned_text) * 0.75)):
                corrected_text = llm_text
                status_value = str(data.get("status") or "").strip()
                status = ProofreadStatus.NEEDS_REVIEW if status_value == ProofreadStatus.NEEDS_REVIEW else ProofreadStatus.COMPLETED
                try:
                    confidence = int(float(data.get("confidence", confidence)))
                except (TypeError, ValueError):
                    pass
                llm_flags = data.get("flags") or []
                if isinstance(llm_flags, list):
                    flags.extend(str(flag) for flag in llm_flags if str(flag).strip())
                notes = str(data.get("notes") or "LLM 校对完成").strip()
            else:
                status = ProofreadStatus.NEEDS_REVIEW
                confidence = min(confidence, 55)
                flags.append("LLM返回文本明显短于原文，已保留规则清理文本")
                notes = "LLM返回文本异常，需人工复核"
        else:
            status = ProofreadStatus.NEEDS_REVIEW
            confidence = min(confidence, 60)
            flags.append("LLM校对失败")
            notes = f"{llm_result.get('error')}；已保留规则清理文本"
    elif use_llm and len(cleaned_text) > STANDARD_PROOFREAD_MAX_CHARS:
        status = ProofreadStatus.NEEDS_REVIEW
        confidence = min(confidence, 70)
        flags.append("页面文本过长，未进行LLM全文改写")
        notes = "已做规则清理；为避免模型输出截断，需人工抽查该页"
    elif not cleaned_text:
        status = ProofreadStatus.FAILED
        confidence = 0
        flags.append("无可校对文本")
        notes = "OCR文本为空"

    deduped_flags = []
    for flag in flags:
        flag = str(flag).strip()
        if flag and flag not in deduped_flags:
            deduped_flags.append(flag)

    page.proofread_text = corrected_text
    page.proofread_status = status
    page.proofread_confidence = max(0, min(100, confidence))
    page.proofread_notes = notes[:1000] if notes else None
    page.quality_flags = json.dumps(deduped_flags, ensure_ascii=False)
    page.proofread_at = datetime.now()
    db.commit()

    return {
        "page_id": page.id,
        "page_number": page.page_number,
        "status": page.proofread_status,
        "confidence": page.proofread_confidence,
        "flags": deduped_flags,
        "notes": page.proofread_notes,
    }


def proofread_standard_document(
    document_id: int,
    db: Session,
    retry_failed: bool = True,
    retry_review: bool = False,
    use_llm: bool = True,
) -> dict:
    document = db.query(StandardDocument).filter(StandardDocument.id == document_id).first()
    if not document:
        raise ValueError(f"规范文档不存在: {document_id}")

    pages = db.query(StandardPage).filter(
        StandardPage.document_id == document.id,
        StandardPage.ocr_status == OcrStatus.COMPLETED,
        StandardPage.ocr_text.isnot(None),
    ).order_by(StandardPage.page_number).all()

    target_statuses = {None, "", ProofreadStatus.PENDING, ProofreadStatus.PROCESSING}
    if retry_failed:
        target_statuses.add(ProofreadStatus.FAILED)
    if retry_review:
        target_statuses.add(ProofreadStatus.NEEDS_REVIEW)

    target_pages = [
        page for page in pages
        if (page.proofread_status or ProofreadStatus.PENDING) in target_statuses
    ]

    completed = 0
    needs_review = 0
    failed = 0
    skipped = len(pages) - len(target_pages)
    page_results = []

    for page in target_pages:
        page.proofread_status = ProofreadStatus.PROCESSING
        page.proofread_notes = None
        db.commit()

        try:
            result = proofread_standard_page(document, page, db, use_llm=use_llm)
            page_results.append(result)
            if page.proofread_status == ProofreadStatus.COMPLETED:
                completed += 1
            elif page.proofread_status == ProofreadStatus.NEEDS_REVIEW:
                needs_review += 1
            else:
                failed += 1
        except Exception as exc:
            page.proofread_status = ProofreadStatus.FAILED
            page.proofread_notes = str(exc)
            page.proofread_at = datetime.now()
            db.commit()
            failed += 1
            page_results.append({
                "page_id": page.id,
                "page_number": page.page_number,
                "status": ProofreadStatus.FAILED,
                "error": str(exc),
            })

    rebuild = rebuild_standard_chunks_from_pages(document, db)
    return {
        "document_id": document.id,
        "title": document.title,
        "total_pages": len(pages),
        "target_pages": len(target_pages),
        "completed": completed,
        "needs_review": needs_review,
        "failed": failed,
        "skipped": skipped,
        "page_results": page_results,
        **rebuild,
    }


def standard_document_progress(document: StandardDocument, db: Session) -> dict:
    pages = db.query(StandardPage).filter(StandardPage.document_id == document.id).all()
    proofread_statuses = [page.proofread_status or ProofreadStatus.PENDING for page in pages]
    return {
        "document_id": document.id,
        "title": document.title,
        "filename": document.filename,
        "import_status": document.import_status,
        "page_count": document.page_count,
        "page_rows": len(pages),
        "completed": sum(1 for p in pages if p.ocr_status == OcrStatus.COMPLETED),
        "processing": sum(1 for p in pages if p.ocr_status == OcrStatus.PROCESSING),
        "failed": sum(1 for p in pages if p.ocr_status == OcrStatus.FAILED),
        "pending": sum(1 for p in pages if p.ocr_status == OcrStatus.PENDING),
        "proofread_completed": sum(1 for status in proofread_statuses if status == ProofreadStatus.COMPLETED),
        "proofread_processing": sum(1 for status in proofread_statuses if status == ProofreadStatus.PROCESSING),
        "proofread_failed": sum(1 for status in proofread_statuses if status == ProofreadStatus.FAILED),
        "proofread_pending": sum(1 for status in proofread_statuses if status == ProofreadStatus.PENDING),
        "proofread_needs_review": sum(1 for status in proofread_statuses if status == ProofreadStatus.NEEDS_REVIEW),
        "char_count": document.char_count,
        "chunk_count": document.chunk_count,
        "error_message": document.error_message,
    }


def _document_boost(query: str, standard_name: str) -> float:
    score = 0.0
    query_lower = query.lower()
    name_lower = standard_name.lower()
    if "人身保险" in standard_name and "保险" not in query:
        score -= 20.0
    if "劳动能力" in standard_name and not any(term in query for term in ("劳动", "工伤", "职业病")):
        score -= 12.0
    if "人体损伤致残程度分级" in standard_name and any(term in query for term in ("伤残", "致残", "残疾")):
        score += 18.0
    if "人体损伤程度鉴定标准" in standard_name and any(term in query for term in ("损伤程度", "轻伤", "重伤", "轻微伤")):
        score += 18.0
    for triggers, names in DOC_BOOST_RULES:
        if any(t.lower() in query_lower for t in triggers) and any(n.lower() in name_lower for n in names):
            score += 8.0
    return score


def _score_chunk(query: str, chunk: StandardChunk) -> float:
    keywords = extract_keywords(query)
    if not keywords:
        keywords = [query]
    haystack = f"{chunk.standard_name}\n{chunk.section_code or ''}\n{chunk.section_title or ''}\n{chunk.chunk_text}".lower()
    score = _document_boost(query, chunk.standard_name)
    for term in keywords:
        term_lower = term.lower()
        if not term_lower:
            continue
        hits = haystack.count(term_lower)
        if hits:
            score += min(hits, 6)
            if term_lower in (chunk.standard_name or "").lower():
                score += 3
            if term_lower in (chunk.section_title or "").lower():
                score += 2
    return score


def search_standards(db: Session, query: str, limit: int = 8) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []
    chunks = db.query(StandardChunk).all()
    scored = []
    for chunk in chunks:
        score = _score_chunk(query, chunk)
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda item: (item[0], item[1].id), reverse=True)
    selected = []
    doc_counts: dict[int, int] = {}
    for score, chunk in scored:
        if doc_counts.get(chunk.document_id, 0) >= 3:
            continue
        selected.append((score, chunk))
        doc_counts[chunk.document_id] = doc_counts.get(chunk.document_id, 0) + 1
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        selected_ids = {chunk.id for _, chunk in selected}
        for score, chunk in scored:
            if chunk.id in selected_ids:
                continue
            selected.append((score, chunk))
            if len(selected) >= limit:
                break
    return [chunk_to_reference(chunk, score) for score, chunk in selected[:limit]]


def chunk_to_reference(chunk: StandardChunk, score: float = 0.0) -> dict:
    text = re.sub(r"\s+", " ", chunk.chunk_text or "").strip()
    return {
        "id": chunk.id,
        "document_id": chunk.document_id,
        "standard_name": chunk.standard_name,
        "section_code": chunk.section_code,
        "section_title": chunk.section_title,
        "text": text,
        "snippet": text[:500],
        "score": round(float(score), 2),
    }


# ── 三层智能检索：规则映射 → LLM选条款 → 精准检索 ──────────────────────────

# 第一层：委托事项关键词 → 标准文档标题关键词的确定性映射
ENTRUSTMENT_TO_STANDARD_TITLES: dict[str, tuple[str, ...]] = {
    "伤残等级": ("人体损伤致残程度分级", "劳动能力鉴定"),
    "伤残": ("人体损伤致残程度分级", "劳动能力鉴定"),
    "残疾": ("人体损伤致残程度分级", "劳动能力鉴定"),
    "工伤": ("劳动能力鉴定", "职工工伤", "职业病致残等级"),
    "劳动能力": ("劳动能力鉴定", "职工工伤", "职业病致残等级"),
    "损伤程度": ("人体损伤程度鉴定标准",),
    "轻伤": ("人体损伤程度鉴定标准",),
    "重伤": ("人体损伤程度鉴定标准",),
    "误工期": ("误工期", "护理期", "营养期"),
    "护理期": ("误工期", "护理期", "营养期"),
    "营养期": ("误工期", "护理期", "营养期"),
    "护理人数": ("误工期", "护理期", "营养期"),
    "护理依赖": ("护理依赖",),
    "因果关系": ("因果关系",),
    "参与度": ("因果关系",),
    "视觉": ("视觉功能障碍",),
    "视力": ("视觉功能障碍",),
    "听觉": ("听觉功能障碍",),
    "听力": ("听觉功能障碍",),
    "关节活动": ("关节活动度",),
    "癫痫": ("外伤性癫痫",),
    "后续治疗费": (),        # 无对应正式标准，需结合临床实际
    "后续治疗": (),
}


def _parse_entrustment_items(entrustment: str) -> list[str]:
    """从委托事项文本中提取独立的鉴定项目列表"""
    items = re.split(r"[；;，,、]", entrustment)
    return [it.strip() for it in items if it.strip()]


def _standard_ids_by_title_keywords(db: Session, title_keywords: tuple[str, ...]) -> set[int]:
    if not title_keywords:
        return set()
    docs = db.query(StandardDocument).all()
    matched: set[int] = set()
    for doc in docs:
        haystack = f"{doc.title or ''} {doc.filename or ''}"
        if any(keyword and keyword in haystack for keyword in title_keywords):
            matched.add(doc.id)
    return matched


def _map_entrustment_to_standard_ids(items: list[str], db: Session) -> dict[str, set[int]]:
    """将每个委托事项映射到相关标准文档 ID。

    Returns:
        {"伤残等级": {47}, "误工期": {56}, ...}
    """
    result: dict[str, set[int]] = {}
    for item in items:
        matched_ids: set[int] = set()
        for keyword, title_keywords in ENTRUSTMENT_TO_STANDARD_TITLES.items():
            if keyword in item:
                matched_ids.update(_standard_ids_by_title_keywords(db, title_keywords))
        result[item] = matched_ids
    return result


def build_standard_toc(db: Session, doc_ids: set[int]) -> list[dict]:
    """为指定标准文档构建目录（条款号+标题+摘要），供 LLM 选条款时导航

    Returns:
        [{"document_id": 20, "standard_name": "人体损伤致残程度分级",
          "sections": [{"code": "", "title": "颅脑损伤", "preview": "..."}, ...]}, ...]
    """
    documents = []
    for doc_id in sorted(doc_ids):
        doc = db.query(StandardDocument).filter(StandardDocument.id == doc_id).first()
        if not doc:
            continue
        chunks = (
            db.query(StandardChunk)
            .filter(StandardChunk.document_id == doc_id)
            .order_by(StandardChunk.id)
            .all()
        )
        sections = []
        for chunk in chunks:
            code = chunk.section_code or ""
            title = chunk.section_title or ""
            preview = re.sub(r"\s+", " ", (chunk.chunk_text or "")[:200]).strip()
            sections.append({"code": code, "title": title, "preview": preview})
        documents.append({
            "document_id": doc_id,
            "standard_name": doc.title or str(doc_id),
            "sections": sections,
        })
    return documents


def search_clauses_in_documents(
    db: Session,
    doc_ids: set[int],
    keywords: str,
    limit_per_doc: int = 5,
) -> list[dict]:
    """在指定标准文档内按关键词检索相关条款，返回精准条款内容

    Args:
        doc_ids: 目标标准文档 ID 集合
        keywords: LLM 分析出的检索词（空格分隔）
        limit_per_doc: 每个文档最多返回的条款数

    Returns:
        与 search_standards 相同格式的引用列表
    """
    keywords = (keywords or "").strip()
    if not keywords or not doc_ids:
        return []

    chunks = (
        db.query(StandardChunk)
        .filter(StandardChunk.document_id.in_(list(doc_ids)))
        .all()
    )

    # 拆分关键词，分为"核心词"（>2字符）和"通用词"（≤2字符），核心词权重更高
    raw_terms = [t.strip().lower() for t in keywords.split() if t.strip()]
    core_terms = [t for t in raw_terms if len(t) > 2]
    general_terms = [t for t in raw_terms if len(t) <= 2]

    # 计算 IDF 风格权重：在全部 chunk 中出现越少的词权重越高
    n_docs = len(chunks) if chunks else 1
    term_df: dict[str, int] = {}
    for chunk in chunks:
        haystack = f"{chunk.chunk_text or ''}".lower()
        for term in core_terms:
            if term in haystack:
                term_df[term] = term_df.get(term, 0) + 1
    term_weight = {t: max(1.0 / (df + 1), 0.3) for t, df in term_df.items()}

    scored = []
    for chunk in chunks:
        haystack = f"{chunk.standard_name}\n{chunk.section_code or ''}\n{chunk.section_title or ''}\n{chunk.chunk_text}".lower()
        score = 0.0
        for term in core_terms:
            hits = min(haystack.count(term), 6)
            if hits:
                score += hits * term_weight.get(term, 0.5) * 3.0
                # 条款号命中加分
                if term in (chunk.section_code or "").lower():
                    score += 5.0
                # 条款标题命中加分
                if term in (chunk.section_title or "").lower():
                    score += 2.0
        for term in general_terms:
            if term in haystack:
                score += 0.5
        if score > 0:
            scored.append((score, chunk))

    scored.sort(key=lambda item: (item[0], item[1].id), reverse=True)

    selected = []
    doc_counts: dict[int, int] = {}
    for score, chunk in scored:
        if doc_counts.get(chunk.document_id, 0) >= limit_per_doc:
            continue
        selected.append(chunk_to_reference(chunk, score))
        doc_counts[chunk.document_id] = doc_counts.get(chunk.document_id, 0) + 1
        if len(selected) >= limit_per_doc * len(doc_ids):
            break

    return selected


def format_selected_clauses(references: list[dict], max_chars: int = 4000) -> str:
    """将精准检索的条款列表格式化为 LLM 可引用的提示文本

    输出格式与 format_standard_references_for_prompt 一致，但容量更大。
    """
    if not references:
        return "未检索到可用规范依据。"
    lines = []
    total = 0
    for idx, ref in enumerate(references, 1):
        section = " ".join(filter(None, [ref.get("section_code"), ref.get("section_title")])).strip()
        header = f"[{idx}] 《{ref['standard_name']}》"
        if section:
            header += f" {section}"
        text = (ref.get("text") or ref.get("snippet") or "").strip()
        line = f"{header}\n{text}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n\n".join(lines)


# ── 旧版（保持兼容）──────────────────────────────────────────────

def build_case_standard_query(case_id: int, db: Session) -> str:
    case = db.query(Case).filter(Case.id == case_id).first()
    person = db.query(Person).filter(Person.case_id == case_id).first()
    records = db.query(HospitalRecord).filter(HospitalRecord.case_id == case_id).all()
    imaging = db.query(ImagingReport).filter(ImagingReport.case_id == case_id).all()

    parts = []
    if person and person.name:
        parts.append(person.name)
    if case:
        parts.extend([
            case.entrustment_matter or "",
            case.accident_description or "",
            case.clinical_examination or "",
        ])
    for record in records:
        parts.extend([
            record.chief_complaint or "",
            record.admission_diagnosis or "",
            record.discharge_diagnosis or "",
            record.treatment_process or "",
            record.discharge_orders or "",
        ])
    for report in imaging:
        parts.extend([
            report.exam_type or "",
            report.exam_part or "",
            report.report_content or "",
        ])
    return "\n".join(p for p in parts if p).strip()[:6000]


def case_standard_references(case_id: int, db: Session, limit: int = 8) -> list[dict]:
    query = build_case_standard_query(case_id, db)
    return search_standards(db, query, limit=limit)


def format_standard_references_for_prompt(references: list[dict], max_chars: int = 3200) -> str:
    if not references:
        return "未检索到可用规范依据。"
    lines = []
    total = 0
    for idx, ref in enumerate(references, 1):
        section = " ".join(filter(None, [ref.get("section_code"), ref.get("section_title")])).strip()
        header = f"[{idx}] 《{ref['standard_name']}》"
        if section:
            header += f" {section}"
        snippet = (ref.get("snippet") or ref.get("text") or "").strip()
        line = f"{header}\n{snippet}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n\n".join(lines)
