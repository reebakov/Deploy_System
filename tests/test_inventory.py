import pytest

import inventory_manager as inv

G = "[smr_clubgg_mobidesk]"


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    path = tmp_path / "smr"
    path.write_text(
        "[other]\nB1\n\n[smr_clubgg_mobidesk]\n# comment\nA106\nA105 ansible_host=1.2.3.4\n\n[tail]\nC1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(inv, "INVENTORY_PATH", path)
    monkeypatch.setattr(inv, "INVENTORY_LOCK_PATH", tmp_path / "smr.lock")
    return path


def test_check_handles_host_vars(inventory):
    found, missing = inv.check_pcs_in_inventory(["A105", "A200"], G)
    assert found == ["A105"] and missing == ["A200"]


def test_add_sorted_preserves_comments_and_other_groups(inventory):
    assert inv.add_pcs_to_inventory(["a110", "A100", "A105"], G)
    text = inventory.read_text(encoding="utf-8")
    assert "# comment" in text
    group = text.split(G)[1].split("[tail]")[0]
    hosts = [ln.split()[0] for ln in group.splitlines() if ln.strip() and not ln.startswith("#")]
    assert hosts == ["A100", "A105", "A106", "A110"]
    assert "[other]\nB1" in text and "[tail]\nC1" in text
    assert "A105 ansible_host=1.2.3.4" in text
    assert inventory.with_suffix(".bak").exists()


def test_remove(inventory):
    inv.add_pcs_to_inventory(["A300"], G)
    assert inv.remove_pcs_from_inventory(["A300"], G)
    assert "A300" not in inventory.read_text(encoding="utf-8")
    assert "A106" in inventory.read_text(encoding="utf-8")


def test_missing_group_appended(inventory):
    inv.add_pcs_to_inventory(["A1000"], "[new_group]")
    assert "[new_group]\nA1000" in inventory.read_text(encoding="utf-8")


def test_natural_sort():
    assert sorted(["A10", "A9", "A100"], key=inv.natural_sort_key) == ["A9", "A10", "A100"]
