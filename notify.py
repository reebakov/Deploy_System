import logging

import requests

from config import MATTERMOST_WEBHOOK

log = logging.getLogger(__name__)


def send_mattermost(text: str) -> bool:
    if not MATTERMOST_WEBHOOK:
        return False
    try:
        r = requests.post(MATTERMOST_WEBHOOK, json={"text": text}, timeout=10)
        return r.status_code < 400
    except Exception as e:
        log.warning("Mattermost send failed: %s", e)
        return False