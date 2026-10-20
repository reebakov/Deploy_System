import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from config import BASE_DIR
from statuses import ACTIVE_STATUSES

_db_path = os.getenv("DATABASE_URL")
if not _db_path:
    # Делаем путь к sqlite абсолютным относительно папки проекта
    DATABASE_URL = f"sqlite:///{BASE_DIR / 'deploy_logs.db'}"
else:
    DATABASE_URL = _db_path

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def utcnow() -> datetime:
    """Naive UTC: единый формат времени в БД (SQLite всё равно не хранит tzinfo)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DeployLog(Base):
    __tablename__ = "deploy_logs"

    id             = Column(Integer, primary_key=True, index=True)
    jira_key       = Column(String, index=True)
    task_type      = Column(String)
    target_pcs     = Column(String)
    status         = Column(String, default="В процессе", index=True)
    skip_tags      = Column(String, nullable=True)
    deploy_type    = Column(String, nullable=True)
    pid            = Column(Integer, nullable=True)
    log_file       = Column(String, nullable=True)
    ansible_output = Column(Text, nullable=True)
    triggered_by   = Column(String, nullable=True)   # аудит: кто запустил
    command        = Column(Text, nullable=True)     # аудит: итоговая команда
    created_at     = Column(DateTime, default=utcnow, index=True)
    updated_at     = Column(DateTime, default=utcnow, onupdate=utcnow)


@contextmanager
def session_scope():
    """Сессия с автоматическим commit/rollback/close."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_active_log(db):
    return db.query(DeployLog).filter(DeployLog.status.in_(ACTIVE_STATUSES)).first()


def init_db() -> None:
    """Создаёт таблицы и применяет лёгкие миграции. Вызывается явно при старте приложения."""
    Base.metadata.create_all(bind=engine)
    _migrate()


def _migrate() -> None:
    insp = inspect(engine)
    if 'deploy_logs' not in insp.get_table_names():
        return
    existing = {c['name'] for c in insp.get_columns('deploy_logs')}
    new_cols = {
        'skip_tags':    'VARCHAR',
        'deploy_type':  'VARCHAR',
        'pid':          'INTEGER',
        'log_file':     'VARCHAR',
        'triggered_by': 'VARCHAR',
        'command':      'TEXT',
    }
    with engine.begin() as conn:
        for name, type_ in new_cols.items():
            if name not in existing:
                conn.execute(text(f'ALTER TABLE deploy_logs ADD COLUMN {name} {type_}'))
        # create_all не добавляет индексы в уже существующую таблицу
        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_deploy_logs_status ON deploy_logs (status)'))
        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_deploy_logs_created_at ON deploy_logs (created_at)'))


def cleanup_old_logs(days: int = 90) -> int:
    cutoff = utcnow() - timedelta(days=days)
    with session_scope() as db:
        old_logs = db.query(DeployLog).filter(DeployLog.created_at < cutoff).all()
        for entry in old_logs:
            if entry.log_file and os.path.exists(entry.log_file):
                try:
                    os.remove(entry.log_file)
                except OSError:
                    pass
            db.delete(entry)
        return len(old_logs)
