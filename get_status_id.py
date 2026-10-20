"""
Универсальный скрипт: находит числовой ID статуса в Jira Insight по его имени.

Использование:
    python get_status_id.py                 # ищет 'Ожидает'
    python get_status_id.py "Используется"  # ищет другой статус
    python get_status_id.py "Ожидает" "Automat"   # явно указать objectType
"""
import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

JIRA_2_SERVER = os.getenv('JIRA_2_SERVER')
JIRA_2_USER   = os.getenv('JIRA_2_USER')
JIRA_2_TOKEN  = os.getenv('JIRA_2_TOKEN')

if not all([JIRA_2_SERVER, JIRA_2_USER, JIRA_2_TOKEN]):
    print("❌ Не заданы JIRA_2_SERVER / JIRA_2_USER / JIRA_2_TOKEN в .env")
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {JIRA_2_TOKEN}",
    "Accept": "application/json"
}
from config import INSIGHT_STATUS_ATTR_ID as STATUS_ATTR_ID  # noqa: E402


def insight_get(path: str, **params) -> dict:
    url = f"{JIRA_2_SERVER}/rest/insight/1.0{path}"
    r = requests.get(url, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def find_status_id_by_example(status_name: str, object_type: str = "Automat") -> int | None:
    iql = f'objectType = "{object_type}" AND Status = "{status_name}"'
    print(f"🔍 Ищем любой объект: {iql}")

    data = insight_get("/iql/objects", iql=iql, resultPerPage=1)
    entries = data.get("objectEntries", [])
    if not entries:
        print(f"   ⚠️  Ни одного объекта со статусом '{status_name}' не найдено.")
        return None

    obj = entries[0]
    obj_id = obj["id"]
    print(f"   ✅ Найден объект: {obj.get('label')} (id={obj_id})")

    obj_full = insight_get(f"/object/{obj_id}")

    for attr in obj_full.get("attributes", []):
        if attr.get("objectTypeAttributeId") != STATUS_ATTR_ID:
            continue
        values = attr.get("objectAttributeValues", [])
        if not values:
            return None

        status_ref = values[0].get("status") or {}
        sid = status_ref.get("id")

        if sid is None:
            return None
        return sid
    return None


def find_status_id_via_objecttype_attribute(status_name: str, object_type: str = "Automat") -> int | None:
    print(f"\n🔍 Fallback: перебор допустимых значений атрибута Status у '{object_type}'")
    types_data = insight_get("/objectschema/list")
    for schema in types_data.get("objectSchemas", []):
        schema_id = schema["id"]
        try:
            type_list = insight_get(f"/objectschema/{schema_id}/objecttypes/flat")
        except Exception:
            continue

        for ot in type_list:
            if ot["name"].lower() != object_type.lower():
                continue
            ot_id = ot["id"]
            attrs = insight_get(f"/objecttype/{ot_id}/attributes")
            status_attr = None
            for a in attrs:
                if a.get("name", "").lower() in ("status", "статус"):
                    status_attr = a
                    break

            if not status_attr:
                continue

            status_ot_id = status_attr.get("typeValue")
            if not status_ot_id:
                continue

            statuses = insight_get(f"/objecttype/{status_ot_id}/objects", resultPerPage=500)
            for s in statuses:
                if s.get("label", "").lower() == status_name.lower():
                    print(f"   ✅ Найден: {s['label']} -> id={s['id']}")
                    return int(s["id"])
    return None


def main():
    status_name = sys.argv[1] if len(sys.argv) > 1 else "Ожидает"
    object_type = sys.argv[2] if len(sys.argv) > 2 else "Automat"

    print(f"=== Поиск ID статуса '{status_name}' (objectType='{object_type}') ===\n")
    sid = find_status_id_by_example(status_name, object_type)
    if sid is None:
        sid = find_status_id_via_objecttype_attribute(status_name, object_type)

    print()
    if sid is not None:
        print(f"🎯 ID статуса '{status_name}' = {sid}\n")
        print("Добавь в .env:")
        if status_name.lower() == "ожидает":
            print(f"INSIGHT_STATUS_FREE_VALUE={sid}")
        elif status_name.lower() == "используется":
            print(f"INSIGHT_STATUS_USED_VALUE={sid}")
        else:
            print(f"INSIGHT_STATUS_{status_name.upper()}_VALUE={sid}")
    else:
        print(f"❌ Не удалось определить ID статуса '{status_name}'.")
        sys.exit(1)


if __name__ == "__main__":
    main()
