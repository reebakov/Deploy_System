import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

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
DASH_USER = os.getenv('DASH_USER', 'admin')
DASH_PASS = os.getenv('DASH_PASS', 'changeme')

# --- Paths ---
ANSIBLE_CWD         = Path(os.getenv('ANSIBLE_CWD', '/root/tars-deploy/ansible'))
INVENTORY_PATH      = Path(os.getenv('INVENTORY_PATH', str(ANSIBLE_CWD / 'inventory/smr')))
INVENTORY_LOCK_PATH = Path(os.getenv('INVENTORY_LOCK_PATH', str(INVENTORY_PATH) + '.lock'))
DEPLOY_LOG_DIR      = Path(os.getenv('DEPLOY_LOG_DIR', '/var/log/deploy'))
DEPLOY_LOG_DIR.mkdir(parents=True, exist_ok=True)

# --- Target group ---
TARGET_GROUP = os.getenv('TARGET_GROUP', '[smr_clubgg_mobidesk]')

# --- Jira custom fields ---
EPIC_FIELD_ID         = 'customfield_10002'
POKER_FIELD_ID        = 'customfield_14725'
ROLE_FIELD_ID         = 'customfield_14312'
STATE_FIELD_ID        = 'customfield_15449'
ACCOUNTS_FIELD_ID     = 'customfield_15419'
PROBLEM_DESC_FIELD_ID = 'customfield_16803'

# --- Insight attribute IDs ---
INSIGHT_STATUS_ATTR_ID    = 589
INSIGHT_ROOM_ATTR_ID      = 843
INSIGHT_STATUS_USED_VALUE = 10
INSIGHT_ROOM_VALUE        = "CAT-66314"
INSIGHT_STATUS_FREE_VALUE = int(os.getenv('INSIGHT_STATUS_FREE_VALUE', '18'))

# --- Ansible ---
ANSIBLE_TIMEOUT = int(os.getenv('ANSIBLE_TIMEOUT', '3600'))
SKIP_TAGS_DEFAULT = (
    "extended_software,taskbar,win_common_setup,win_activation,"
    "common_software,win_user_autologon,vnc"
)

# --- Retention ---
LOG_RETENTION_DAYS = int(os.getenv('LOG_RETENTION_DAYS', '90'))

# --- Playbooks ---
PLAYBOOK_MOBIDESK = 'playbooks/im/mobi_desktop/clubgg_mobidesk.yml'
PLAYBOOK_OPENSSH  = 'playbooks/im/openssh_init.yaml'
