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
