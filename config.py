import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


def _env_list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


# --- Jira 1 (issues) ---
JIRA_SERVER = os.getenv('JIRA_1_SERVER', 'https://jira.getcom.pw')
JIRA_USER   = os.getenv('JIRA_1_USER')
JIRA_TOKEN  = os.getenv('JIRA_1_TOKEN')

# --- Jira 2 (Insight) ---
JIRA_2_SERVER = os.getenv('JIRA_2_SERVER')
JIRA_2_USER   = os.getenv('JIRA_2_USER')
JIRA_2_TOKEN  = os.getenv('JIRA_2_TOKEN')

# --- Mattermost ---
MATTERMOST_WEBHOOK = os.getenv('MATTERMOST_WEBHOOK')

# --- Dashboard auth ---
# Локальный сервис за VPN: слабый пароль не блокирует запуск, только пишется предупреждение.
DASH_USER = os.getenv('DASH_USER', 'admin')
DASH_PASS = os.getenv('DASH_PASS') or 'changeme'
# Секрет для CSRF-токенов. Если не задан — генерируется при старте (токены живут до рестарта).
CSRF_SECRET = os.getenv('CSRF_SECRET', '')
AUTH_MAX_FAILURES   = int(os.getenv('AUTH_MAX_FAILURES', '5'))
AUTH_LOCKOUT_SECONDS = int(os.getenv('AUTH_LOCKOUT_SECONDS', '300'))
DASH_TIMEZONE = os.getenv('DASH_TIMEZONE', 'Asia/Novosibirsk')

# --- Paths ---
ANSIBLE_CWD         = Path(os.getenv('ANSIBLE_CWD', '/root/tars-deploy/ansible'))
INVENTORY_PATH      = Path(os.getenv('INVENTORY_PATH', str(ANSIBLE_CWD / 'inventory/smr')))
INVENTORY_LOCK_PATH = Path(os.getenv('INVENTORY_LOCK_PATH', str(INVENTORY_PATH) + '.lock'))
DEPLOY_LOG_DIR      = Path(os.getenv('DEPLOY_LOG_DIR', '/var/log/deploy'))
APP_LOG_FILE        = os.getenv('APP_LOG_FILE', '/var/log/deploy-bot.log')
# Файлы состояния (статус бота и настройки деплоя)
STATE_DIR           = Path(os.getenv('STATE_DIR', str(BASE_DIR)))


def ensure_dirs() -> None:
    """Создаёт рабочие каталоги. Вызывается явно при старте, а не при импорте модуля."""
    DEPLOY_LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def validate_config() -> None:
    """Предупреждает о слабом пароле дашборда, но не мешает запуску."""
    if DASH_PASS.strip().lower() in {"changeme", "admin", "password", "123456"}:
        logging.getLogger(__name__).warning(
            "DASH_PASS не задан или слишком простой — рекомендуется сменить пароль в .env."
        )


# --- Target group ---
TARGET_GROUP = os.getenv('TARGET_GROUP', '[smr_clubgg_mobidesk]')

# --- Jira: выбор задач (раньше было захардкожено в коде) ---
JIRA_PROJECT      = os.getenv('JIRA_PROJECT', 'ACC')
JIRA_JQL          = os.getenv(
    'JIRA_JQL',
    f'project = {JIRA_PROJECT} AND assignee is EMPTY AND status = "To Do"',
)
JIRA_MAX_RESULTS  = int(os.getenv('JIRA_MAX_RESULTS', '50'))
TASK_POKER        = os.getenv('TASK_POKER', 'CLUBGG').upper()
TASK_ROLE         = os.getenv('TASK_ROLE', 'HMS NSK').upper()
TASK_EPICS        = [e.upper() for e in _env_list('TASK_EPICS', 'DR-401,DR-390')]
JIRA_DONE_TRANSITIONS = [t.lower() for t in _env_list(
    'JIRA_DONE_TRANSITIONS', 'готово,done,решена,закрыто,closed')]
# Не брать повторно задачу, по которой уже был запуск в последние N минут
BOT_RETRY_COOLDOWN_MIN = int(os.getenv('BOT_RETRY_COOLDOWN_MIN', '30'))

# --- Jira custom fields ---
EPIC_FIELD_ID         = os.getenv('EPIC_FIELD_ID', 'customfield_10002')
POKER_FIELD_ID        = os.getenv('POKER_FIELD_ID', 'customfield_14725')
ROLE_FIELD_ID         = os.getenv('ROLE_FIELD_ID', 'customfield_14312')
STATE_FIELD_ID        = os.getenv('STATE_FIELD_ID', 'customfield_15449')
ACCOUNTS_FIELD_ID     = os.getenv('ACCOUNTS_FIELD_ID', 'customfield_15419')
PROBLEM_DESC_FIELD_ID = os.getenv('PROBLEM_DESC_FIELD_ID', 'customfield_16803')

# --- Insight ---
INSIGHT_STATUS_ATTR_ID    = int(os.getenv('INSIGHT_STATUS_ATTR_ID', '589'))
INSIGHT_ROOM_ATTR_ID      = int(os.getenv('INSIGHT_ROOM_ATTR_ID', '843'))
INSIGHT_STATUS_USED_VALUE = int(os.getenv('INSIGHT_STATUS_USED_VALUE', '10'))
INSIGHT_ROOM_VALUE        = os.getenv('INSIGHT_ROOM_VALUE', 'CAT-66314')
INSIGHT_STATUS_FREE_VALUE = int(os.getenv('INSIGHT_STATUS_FREE_VALUE', '18'))
INSIGHT_GPU_MODELS = _env_list(
    'INSIGHT_GPU_MODELS',
    "Intel UHD Graphics 630,Intel UHD Graphics 730,Intel HD Graphics 630,"
    "HD Graphics 630,NVIDIA GeForce GT 710,NVIDIA GeForce GT 1030,"
    "NVIDIA GeForce GTX 1050 Ti,NVIDIA GeForce GTX 1650",
)

# --- Ansible ---
ANSIBLE_TIMEOUT = int(os.getenv('ANSIBLE_TIMEOUT', '3600'))
SKIP_TAGS_DEFAULT = (
    "extended_software,taskbar,win_common_setup,win_activation,"
    "common_software,win_user_autologon,vnc,seq"
)
ROOM_TAGS_DEFAULT = "neoagent,clubgg,neoagent_account_shortcuts"

# Автоповтор упавших хостов (флаки: choco-репозиторий, сеть) и подбор замены недоступным ПК
ANSIBLE_AUTO_RETRIES = int(os.getenv('ANSIBLE_AUTO_RETRIES', '1'))
ANSIBLE_RETRY_PAUSE = int(os.getenv('ANSIBLE_RETRY_PAUSE', '30'))
PC_REPLACEMENT_ATTEMPTS = int(os.getenv('PC_REPLACEMENT_ATTEMPTS', '3'))

# --- Retention / watchdog ---
LOG_RETENTION_DAYS = int(os.getenv('LOG_RETENTION_DAYS', '90'))
# Деплой, висящий дольше ANSIBLE_TIMEOUT + запас, считается зависшим → алерт в Mattermost
WATCHDOG_GRACE_SECONDS = int(os.getenv('WATCHDOG_GRACE_SECONDS', '600'))

# --- Playbooks ---
PLAYBOOK_MOBIDESK = 'playbooks/im/mobi_desktop/clubgg_mobidesk.yml'
PLAYBOOK_OPENSSH  = 'playbooks/im/openssh_init.yaml'
