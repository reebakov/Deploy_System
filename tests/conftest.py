import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Окружение задаётся ДО импорта модулей проекта
_TMP = Path(os.environ.get("PYTEST_TMP_ROOT") or tempfile.mkdtemp(prefix="deploy_tests_"))
_TMP.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["DASH_USER"] = "admin"
os.environ["DASH_PASS"] = "s3cret-pass"
os.environ["DEPLOY_LOG_DIR"] = str(_TMP / "logs")
os.environ["STATE_DIR"] = str(_TMP / "state")
os.environ["APP_LOG_FILE"] = str(_TMP / "app.log")
os.environ["ANSIBLE_CWD"] = str(_TMP / "ansible")
os.environ["INVENTORY_PATH"] = str(_TMP / "inventory")
os.environ["MATTERMOST_WEBHOOK"] = ""


@pytest.fixture(autouse=True)
def clean_db():
    import config
    from database import Base, engine, init_db
    config.ensure_dirs()
    Base.metadata.drop_all(bind=engine)
    init_db()
    yield
