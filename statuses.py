"""Единый справочник статусов деплоя (значения совпадают с теми, что хранятся в БД и ожидает UI)."""


class Status:
    INIT        = "Инициализация"
    RUNNING     = "В процессе (Ansible)"
    SUCCESS     = "Успешно"
    TIMEOUT     = "Таймаут"
    CRITICAL    = "Критическая ошибка"
    INTERRUPTED = "Прервано (рестарт)"
    NO_PCS      = "Ошибка: нет ПК"
    INV_ERROR   = "Ошибка инвентаря"
    PARSE_ERROR = "Ошибка парсинга"
    NOT_ENOUGH  = "Отмена: нехватка ПК"
    PREPARING   = "Подбор ПК..."
    UNREACHABLE = "ПК недоступен"
    RETRY_OK    = "Успешно (после автоповтора)"


ACTIVE_STATUSES = (Status.INIT, Status.RUNNING)


def is_active(status: str | None) -> bool:
    s = status or ""
    return "В процессе" in s or s == Status.INIT


def is_partial(status: str | None) -> bool:
    return "Частично" in (status or "")
