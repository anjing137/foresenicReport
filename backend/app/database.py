"""
数据库配置 - SQLite 同步模式
"""
from sqlalchemy import create_engine, event
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import settings


DATABASE_URL = settings.DATABASE_URL
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
)


if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """初始化数据库表"""
    Base.metadata.create_all(bind=engine)
    _ensure_runtime_columns()


def _ensure_runtime_columns():
    """SQLite 简易迁移：为既有数据库补充新增列。"""
    if not DATABASE_URL.startswith("sqlite"):
        return

    inspector = inspect(engine)
    statements = []
    if inspector.has_table("materials"):
        material_columns = {col["name"] for col in inspector.get_columns("materials")}
        if "material_subtype" not in material_columns:
            statements.append("ALTER TABLE materials ADD COLUMN material_subtype VARCHAR(50)")

    if inspector.has_table("material_groups"):
        material_group_columns = {col["name"] for col in inspector.get_columns("material_groups")}
        if "is_confirmed" not in material_group_columns:
            statements.append("ALTER TABLE material_groups ADD COLUMN is_confirmed BOOLEAN DEFAULT 0")

    if inspector.has_table("hospital_records"):
        hospital_record_columns = {col["name"] for col in inspector.get_columns("hospital_records")}
        additions = {
            "review_status": "VARCHAR(20) DEFAULT 'pending'",
            "extraction_confidence": "INTEGER",
            "quality_flags": "TEXT",
        }
        for column, definition in additions.items():
            if column not in hospital_record_columns:
                statements.append(f"ALTER TABLE hospital_records ADD COLUMN {column} {definition}")

    if inspector.has_table("imaging_reports"):
        imaging_report_columns = {col["name"] for col in inspector.get_columns("imaging_reports")}
        additions = {
            "group_id": "INTEGER",
            "review_status": "VARCHAR(20) DEFAULT 'pending'",
            "extraction_confidence": "INTEGER",
            "quality_flags": "TEXT",
            "source_material_ids": "TEXT",
            "source_page_numbers": "TEXT",
        }
        for column, definition in additions.items():
            if column not in imaging_report_columns:
                statements.append(f"ALTER TABLE imaging_reports ADD COLUMN {column} {definition}")

    if inspector.has_table("standard_pages"):
        standard_page_columns = {col["name"] for col in inspector.get_columns("standard_pages")}
        additions = {
            "proofread_text": "TEXT",
            "proofread_status": "VARCHAR(20)",
            "proofread_confidence": "INTEGER",
            "proofread_notes": "TEXT",
            "quality_flags": "TEXT",
            "proofread_at": "DATETIME",
        }
        for column, definition in additions.items():
            if column not in standard_page_columns:
                statements.append(f"ALTER TABLE standard_pages ADD COLUMN {column} {definition}")

    if not statements:
        return

    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))
