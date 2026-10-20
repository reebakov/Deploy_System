import sys

import ansible_runner as ar

RECAP_PARTIAL = """
PLAY RECAP ***
A105 : ok=10 changed=1 unreachable=0 failed=0
A106 : ok=3 changed=0 unreachable=1 failed=0
"""
RECAP_ALL_FAIL = "PLAY RECAP\nA105 : ok=0 changed=0 unreachable=1 failed=0\n"


def test_basic_codes():
    assert ar.get_ansible_status(0) == "Успешно"
    assert "Неизвестный" in ar.get_ansible_status(77)


def test_partial(tmp_path):
    f = tmp_path / "l.log"
    f.write_text(RECAP_PARTIAL, encoding="utf-8")
    assert ar.get_detailed_status(2, f).startswith("Частично (1 ок, 1 fail)")


def test_all_failed(tmp_path):
    f = tmp_path / "l.log"
    f.write_text(RECAP_ALL_FAIL, encoding="utf-8")
    assert ar.get_detailed_status(3, f).startswith("Все failed")


def test_infer_status_from_log(tmp_path):
    f = tmp_path / "l.log"
    f.write_text("PLAY RECAP\nA105 : ok=5 changed=0 unreachable=0 failed=0\n", encoding="utf-8")
    assert ar._infer_status_from_log(str(f)).startswith("Успешно")
    assert ar._infer_status_from_log(None) == "Прервано (рестарт)"


def test_execute_success_and_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "ANSIBLE_CWD", tmp_path)
    rc, out, status = ar.execute([sys.executable, "-c", "print('hi')"])
    assert rc == 0 and "hi" in out and status == "Успешно"
    rc, _, status = ar.execute([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
    assert rc == -1 and status == "Таймаут"


def test_rc4_is_unreachable_not_parse_error():
    assert "недоступен" in ar.get_ansible_status(4)
    assert "парсинга" not in ar.get_ansible_status(4)


def test_unreachable_partial_rc4(tmp_path):
    f = tmp_path / "l.log"
    f.write_text(RECAP_PARTIAL, encoding="utf-8")
    assert ar.get_detailed_status(4, f).startswith("Частично")


def test_summarize_errors(tmp_path):
    f = tmp_path / "l.log"
    f.write_text(
        'TASK [x]\nfatal: [A105]: UNREACHABLE! => {"msg": "timed out"}\nok: [A106]\n'
        'fatal: [A106]: FAILED! => {"msg": "boom"}\n',
        encoding="utf-8",
    )
    out = ar.summarize_errors(f)
    assert "A105" in out and "A106" in out and "ok:" not in out
    assert ar.summarize_errors(tmp_path / "missing.log") == ""
