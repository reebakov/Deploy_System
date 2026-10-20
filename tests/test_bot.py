from types import SimpleNamespace

import bot


def make_issue(**fields):
    base = dict(
        description="", customfield_10002="DR-401", customfield_14725="ClubGG",
        customfield_14312="HMS NSK", customfield_15449="", customfield_16803="",
    )
    base.update(fields)
    return SimpleNamespace(key="ACC-1", fields=SimpleNamespace(**base))


def test_classify_a():
    s, _, _ = bot.classify_issue(make_issue(customfield_15449="Комп есть, требуется подготовка без интернета"))
    assert s == "A"


def test_classify_b_has_priority_over_c():
    s, _, _ = bot.classify_issue(
        make_issue(description="ПК нет в ГР", customfield_15449="комп новый, требуется подбор"))
    assert s == "B"


def test_classify_c():
    s, _, _ = bot.classify_issue(make_issue(customfield_15449="Комп новый, требуется подбор"))
    assert s == "C"


def test_classify_wrong_epic_or_room():
    state = "комп новый, требуется подбор"
    assert bot.classify_issue(make_issue(customfield_10002="XX-1", customfield_15449=state))[0] is None
    assert bot.classify_issue(make_issue(customfield_14725="Other", customfield_15449=state))[0] is None


def test_extract_pcs():
    text = "нужны а105, A106 и A107-B, артикул XA999, A5"
    assert bot.extract_pcs(text) == ["A105", "A106"]


def test_fill_description():
    desc = "|| PC || Acc ||\n| | x |\n|&nbsp;| y |\n| A1 | z |"
    new, filled = bot.fill_pcs_in_description(desc, ["A100", "A101", "A102"])
    assert filled == 2
    assert "| A100 |" in new and "| A101 |" in new
    assert "Дополнительные ПК:* A102" in new


def test_iql_contains_quoted_gpus():
    assert '"NVIDIA GeForce GTX 1650"' in bot.IQL_FREE_PC


# --- автоповтор и устойчивость к флакам ---------------------------------------

def test_failed_hosts_parsing():
    import ansible_runner as ar
    recap = ("PLAY RECAP\nA168 : ok=36 changed=2 unreachable=0 failed=0\n"
             "A253 : ok=19 changed=1 unreachable=0 failed=1\nlocalhost : ok=0 unreachable=1 failed=0\n")
    assert ar.failed_hosts(recap) == ["A253"]
    assert ar.failed_hosts("нет рекапа") == []


def _patch_runs(monkeypatch, results, log_text):
    """results — очередь (rc, status) для run_ansible_blocking; лог всегда содержит log_text."""
    calls = []

    def fake_run(cmd, log_id=None, timeout=None):
        calls.append(cmd)
        return (*results.pop(0)[:1], "", results_status.pop(0))

    results_status = [r[1] for r in results]
    monkeypatch.setattr(bot, "run_ansible_blocking", fake_run)
    monkeypatch.setattr(bot, "failed_hosts_from_log", lambda p: log_text)
    monkeypatch.setattr(bot, "ANSIBLE_RETRY_PAUSE", 0)
    return calls


def test_retry_only_failed_hosts_and_succeeds(monkeypatch):
    calls = _patch_runs(monkeypatch, [(2, "Частично (1 ок, 1 fail)"), (0, "Успешно")], ["A253"])
    msgs = []
    rc, status = bot.run_deploy_with_retry("room_only", ["A168", "A253"], ["x"], 1, msgs.append)
    assert (rc, status) == (0, "Успешно (после автоповтора)")
    assert len(calls) == 2 and "target=A253" in " ".join(calls[1])


def test_retry_still_failing_is_partial(monkeypatch):
    _patch_runs(monkeypatch, [(2, "Частично (1 ок, 1 fail)"), (2, "Все failed (1 шт)")], ["A253"])
    rc, status = bot.run_deploy_with_retry("room_only", ["A168", "A253"], ["x"], 1, lambda m: None)
    assert rc == 2 and status.startswith("Частично (1 ок, 1 fail)")


def test_no_retry_on_timeout_or_success(monkeypatch):
    calls = _patch_runs(monkeypatch, [(-1, "Таймаут")], ["A1"])
    assert bot.run_deploy_with_retry("full", ["A101"], ["x"], 1, lambda m: None) == (-1, "Таймаут")
    assert len(calls) == 1
    calls = _patch_runs(monkeypatch, [(0, "Успешно")], [])
    assert bot.run_deploy_with_retry("full", ["A101"], ["x"], 1, lambda m: None) == (0, "Успешно")
    assert len(calls) == 1
