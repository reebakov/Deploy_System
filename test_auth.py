import os
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

SERVER = os.getenv('JIRA_2_SERVER')
USER   = os.getenv('JIRA_2_USER')
TOKEN  = os.getenv('JIRA_2_TOKEN')

url = f"{SERVER}/rest/insight/1.0/iql/objects?iql=objectType=%22Automat%22&resultPerPage=1"

print(f"URL: {url}")
print(f"USER: {USER}")
print(f"TOKEN: {TOKEN[:5]}... (скрыто)")

print("\n--- ТЕСТ 1: Basic Auth (Логин + Токен/Пароль) ---")
r1 = requests.get(url, auth=HTTPBasicAuth(USER, TOKEN), timeout=10)
print(f"Ответ: {r1.status_code}")
if r1.status_code == 200:
    print(f"Найдено объектов: {len(r1.json().get('objectEntries', []))}")

print("\n--- ТЕСТ 2: Bearer Auth (Только Токен) ---")
headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
r2 = requests.get(url, headers=headers, timeout=10)
print(f"Ответ: {r2.status_code}")
if r2.status_code == 200:
    print(f"Найдено объектов: {len(r2.json().get('objectEntries', []))}")