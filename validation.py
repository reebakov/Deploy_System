"""Валидация всего, что пользователь/Jira могут подсунуть в команду ansible-playbook."""
import re
import shlex

# Допустимое имя ПК: A + 2..6 цифр (кириллическая «А» заменяется на латинскую)
PC_NAME_RE = re.compile(r'^A\d{2,6}$')
# Теги Ansible: буквы, цифры, _, -, запятая
TAGS_RE = re.compile(r'^[A-Za-z0-9_\-]+(,[A-Za-z0-9_\-]+)*$')

MAX_PCS_PER_REQUEST = 100
MAX_FORKS = 50

DEPLOY_TYPES = ("full", "room_only", "ssh")


class ValidationError(ValueError):
    pass


def sanitize_pc_name(name: str) -> str:
    """Заменяет кириллическую 'А' на латинскую и переводит в верхний регистр."""
    return name.upper().replace('А', 'A').strip()


def parse_pc_list(raw: str) -> list[str]:
    """Разбирает строку 'A105, A106 A107' в список проверенных имён без дублей.

    Бросает ValidationError, если хотя бы одно имя не подходит под формат.
    """
    parts = [p for p in re.split(r'[,\s;]+', raw or '') if p]
    result: list[str] = []
    seen: set[str] = set()
    for p in parts:
        name = sanitize_pc_name(p)
        if not PC_NAME_RE.match(name):
            raise ValidationError(f"Недопустимое имя ПК: {p[:30]!r} (ожидается формат A105)")
        if name not in seen:
            seen.add(name)
            result.append(name)
    if len(result) > MAX_PCS_PER_REQUEST:
        raise ValidationError(f"Слишком много ПК за раз (максимум {MAX_PCS_PER_REQUEST})")
    return result


def validate_tags(tags: str) -> str:
    tags = (tags or '').strip()
    if tags and not TAGS_RE.match(tags):
        raise ValidationError("Теги могут содержать только латиницу, цифры, _, - и запятые")
    return tags


def parse_ansible_args(raw: str) -> list[str]:
    """Разбирает настраиваемые аргументы ansible-playbook по белому списку.

    Разрешено: --tags X, --skip-tags X, -f/--forks N (в т.ч. в форме --tags=X).
    Всё остальное (-e, -m, --extra-vars, @file, ...) отклоняется.
    """
    raw = (raw or '').strip()
    if not raw:
        return []
    try:
        tokens = shlex.split(raw)
    except ValueError as e:
        raise ValidationError(f"Не удалось разобрать аргументы: {e}")

    result: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if '=' in tok and tok.startswith('--'):
            flag, _, value = tok.partition('=')
            i += 1
        else:
            flag, value = tok, None
            i += 1
            if flag in ('--tags', '--skip-tags', '-t', '-f', '--forks'):
                if i >= len(tokens):
                    raise ValidationError(f"У опции {flag} нет значения")
                value = tokens[i]
                i += 1

        if flag in ('--tags', '--skip-tags', '-t'):
            validate_tags(value or '')
            if not value:
                raise ValidationError(f"Пустое значение для {flag}")
            result += ['--tags' if flag == '-t' else flag, value]
        elif flag in ('-f', '--forks'):
            if not (value and value.isdigit() and 1 <= int(value) <= MAX_FORKS):
                raise ValidationError(f"forks должен быть числом от 1 до {MAX_FORKS}")
            result += ['-f', value]
        else:
            raise ValidationError(f"Опция {flag!r} не разрешена")
    return result
