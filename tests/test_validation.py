import pytest

from validation import ValidationError, parse_ansible_args, parse_pc_list, sanitize_pc_name, validate_tags


def test_sanitize_cyrillic_a():
    assert sanitize_pc_name("а105") == "A105"


def test_parse_pc_list_ok_and_dedup():
    assert parse_pc_list("a105, A106  A105;А107") == ["A105", "A106", "A107"]


@pytest.mark.parametrize("bad", ["A105,-e", "A1", "host;rm", "A105 --extra-vars=@x", "../etc"])
def test_parse_pc_list_rejects(bad):
    with pytest.raises(ValidationError):
        parse_pc_list(bad)


def test_too_many_pcs():
    with pytest.raises(ValidationError):
        parse_pc_list(",".join(f"A{i}" for i in range(100, 300)))


def test_tags():
    assert validate_tags("a,b_c,d-e") == "a,b_c,d-e"
    for bad in ("a b", "a;b", "$(x)", "a,,b"):
        with pytest.raises(ValidationError):
            validate_tags(bad)


def test_ansible_args_whitelist():
    assert parse_ansible_args("--skip-tags a,b") == ["--skip-tags", "a,b"]
    assert parse_ansible_args("--tags=x -f 3") == ["--tags", "x", "-f", "3"]
    assert parse_ansible_args("") == []


@pytest.mark.parametrize("bad", [
    "-e foo=bar", "--extra-vars @/etc/passwd", "-m shell", "--tags", "-f 9999", "--tags a;b",
    "--skip-tags x -e y=z",
])
def test_ansible_args_rejects(bad):
    with pytest.raises(ValidationError):
        parse_ansible_args(bad)
