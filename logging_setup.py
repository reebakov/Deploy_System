import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from config import APP_LOG_FILE

_configured = False


def setup_logging(log_file: str = APP_LOG_FILE) -> logging.Logger:
    global _configured
    root = logging.getLogger()
    if _configured:
        return logging.getLogger('deploy')

    root.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s %(levelname)s [%(name)s] %(message)s')

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8')
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception as e:
        root.warning("Cannot open log file %s: %s", log_file, e)

    _configured = True
    return logging.getLogger('deploy')
