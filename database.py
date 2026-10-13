import os
from datetime import datetime, timedelta

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import inspect, text

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./deploy_logs.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class DeployLog(Base):
    __tablename__ = "deploy_logs"

    id             = Column(Integer, primary_key=True, index=True)
    jira_key       = Column(String, index=True)
    task_type      = Column(String)
    target_pcs     = Column(String)
    status         = Column(String, default="В процессе")
    skip_tags      = Column(String, nullable=True)
    deploy_type    = Column(String, nullable=True)
    pid            = Column(Integer, nullable=True)
    log_file       = Column(String, nullable=True)
    ansible_output = Column(Text, nullable=True) # Оставляем колонку, чтобы не ломать старую БД SQLite
    created_at     = Column(DateTime, default=datetime.utcnow)
    updated_at     = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)


def _migrate():
    insp = inspect(engine)
    if 'deploy_logs' not in insp.get_table_names():
        return
    existing = {c['name'] for c in insp.get_columns('deploy_logs')}
    new_cols = {
        'skip_tags':   'VARCHAR',
        'deploy_type': 'VARCHAR',
        'pid':         'INTEGER',
        'log_file':    'VARCHAR',
    }
    with engine.begin() as conn:
        for name, type_ in new_cols.items():
            if name not in existing:
                conn.execute(text(f'ALTER TABLE deploy_logs ADD COLUMN {name} {type_}'))


def cleanup_old_logs(days: int = 90) -> int:
    """Удаляет старые записи из БД и физические файлы логов с диска."""
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=days)
        old_logs = db.query(DeployLog).filter(DeployLog.created_at < cutoff).all()
        count = 0
        for l in old_logs:
            if l.log_file and os.path.exists(l.log_file):
                try:
                    os.remove(l.log_file)
                except OSError:
                    pass
            db.delete(l)
            count += 1
        db.commit()
        return count
    finally:
        db.close()


init_db()
_migrate()
