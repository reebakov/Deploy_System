import logging
import requests
from jira import JIRA
from requests.auth import HTTPBasicAuth
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)

from config import (
    INSIGHT_ROOM_ATTR_ID, INSIGHT_ROOM_VALUE,
    INSIGHT_STATUS_ATTR_ID, INSIGHT_STATUS_USED_VALUE,
    INSIGHT_STATUS_FREE_VALUE,
    JIRA_2_SERVER, JIRA_2_TOKEN, JIRA_2_USER,
    JIRA_SERVER, JIRA_TOKEN, JIRA_USER,
)

log = logging.getLogger(__name__)


def get_jira() -> JIRA:
    return JIRA(server=JIRA_SERVER, basic_auth=(JIRA_USER, JIRA_TOKEN))


INSIGHT_HEADERS = {
    "Authorization": f"Bearer {JIRA_2_TOKEN}",
    "Accept": "application/json"
}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=10),
    retry=retry_if_exception_type(requests.RequestException),
    reraise=True,
)
def insight_search(iql: str, result_per_page: int = 500) -> list[dict]:
    r = requests.get(
        f"{JIRA_2_SERVER}/rest/insight/1.0/iql/objects",
        params={"iql": iql, "resultPerPage": result_per_page},
        headers=INSIGHT_HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("objectEntries", [])


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=10),
    retry=retry_if_exception_type(requests.RequestException),
    reraise=True,
)
def insight_update_pc(object_id: str) -> bool:
    url = f"{JIRA_2_SERVER}/rest/insight/1.0/object/{object_id}"
    payload = {
        "attributes": [
            {"objectTypeAttributeId": INSIGHT_STATUS_ATTR_ID,
             "objectAttributeValues": [{"value": INSIGHT_STATUS_USED_VALUE}]},
            {"objectTypeAttributeId": INSIGHT_ROOM_ATTR_ID,
             "objectAttributeValues": [{"value": INSIGHT_ROOM_VALUE}]},
        ]
    }
    r = requests.put(url, json=payload, headers=INSIGHT_HEADERS, timeout=15)
    r.raise_for_status()  # Гарантирует, что retry сработает на 4xx/5xx ошибках
    return r.status_code in (200, 201, 202, 204)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=10),
    retry=retry_if_exception_type(requests.RequestException),
    reraise=True,
)
def insight_revert_pc(object_id: str) -> bool:
    url = f"{JIRA_2_SERVER}/rest/insight/1.0/object/{object_id}"
    payload = {
        "attributes": [
            {"objectTypeAttributeId": INSIGHT_STATUS_ATTR_ID,
             "objectAttributeValues": [{"value": INSIGHT_STATUS_FREE_VALUE}]},
            {"objectTypeAttributeId": INSIGHT_ROOM_ATTR_ID,
             "objectAttributeValues": []}, 
        ]
    }
    r = requests.put(url, json=payload, headers=INSIGHT_HEADERS, timeout=15)
    r.raise_for_status()
    return r.status_code in (200, 201, 202, 204)