import logging
import sys
import os

_configured = False

def setup_logging(log_file: str = '/var/log/deploy-bot.log') -> logging.Logger:
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
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception as e:
        root.warning("Cannot open log file %s: %s", log_file, e)

    _configured = True
    return logging.getLogger('deploy')