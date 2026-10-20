import logging

import requests
from jira import JIRA
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import (
    INSIGHT_ROOM_ATTR_ID, INSIGHT_ROOM_VALUE,
    INSIGHT_STATUS_ATTR_ID, INSIGHT_STATUS_FREE_VALUE, INSIGHT_STATUS_USED_VALUE,
    JIRA_2_SERVER, JIRA_2_TOKEN,
    JIRA_SERVER, JIRA_TOKEN, JIRA_USER,
)

log = logging.getLogger(__name__)

_retry_http = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=10),
    retry=retry_if_exception_type(requests.RequestException),
    reraise=True,
)


def get_jira() -> JIRA:
    return JIRA(server=JIRA_SERVER, basic_auth=(JIRA_USER, JIRA_TOKEN))


def _insight_headers() -> dict:
    # Заголовки строятся при вызове, а не при импорте: токен можно подменить без перезагрузки модуля
    return {"Authorization": f"Bearer {JIRA_2_TOKEN}", "Accept": "application/json"}


def iql_quote(value: str) -> str:
    return value.replace('\\', '\\\\').replace('"', '\\"')


@_retry_http
def insight_search(iql: str, result_per_page: int = 500) -> list[dict]:
    r = requests.get(
        f"{JIRA_2_SERVER}/rest/insight/1.0/iql/objects",
        params={"iql": iql, "resultPerPage": result_per_page},
        headers=_insight_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("objectEntries", [])


def _insight_put(object_id: str, attributes: list[dict]) -> bool:
    r = requests.put(
        f"{JIRA_2_SERVER}/rest/insight/1.0/object/{object_id}",
        json={"attributes": attributes},
        headers=_insight_headers(),
        timeout=15,
    )
    r.raise_for_status()  # чтобы retry сработал на 4xx/5xx
    return r.status_code in (200, 201, 202, 204)


@_retry_http
def insight_update_pc(object_id: str) -> bool:
    return _insight_put(object_id, [
        {"objectTypeAttributeId": INSIGHT_STATUS_ATTR_ID,
         "objectAttributeValues": [{"value": INSIGHT_STATUS_USED_VALUE}]},
        {"objectTypeAttributeId": INSIGHT_ROOM_ATTR_ID,
         "objectAttributeValues": [{"value": INSIGHT_ROOM_VALUE}]},
    ])


@_retry_http
def insight_revert_pc(object_id: str) -> bool:
    return _insight_put(object_id, [
        {"objectTypeAttributeId": INSIGHT_STATUS_ATTR_ID,
         "objectAttributeValues": [{"value": INSIGHT_STATUS_FREE_VALUE}]},
        {"objectTypeAttributeId": INSIGHT_ROOM_ATTR_ID,
         "objectAttributeValues": []},
    ])


def safe_revert_pc(object_id: str) -> bool:
    """Откат без исключений: сбой отката не должен ронять обработку задачи."""
    try:
        return insight_revert_pc(object_id)
    except Exception:
        log.exception("Insight revert failed for object %s", object_id)
        return False
