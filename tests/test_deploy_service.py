import threading

import pytest

import deploy_service as ds
from database import DeployLog, session_scope
from statuses import Status
from validation import ValidationError

KW = dict(jira_key="k", task_type="t", target_pcs="A105", status=Status.RUNNING, deploy_type="full")


def test_build_command_variants():
    name, cmd = ds.build_command("room_only", ["A105", "A106"])
    assert name == "Обновление рума"
    assert cmd[:4] == ["ansible-playbook", ds.PLAYBOOK_MOBIDESK, "-e", "target=A105,A106"]
    assert "--tags" in cmd and cmd[-2:] == ["-f", "2"]

    _, cmd = ds.build_command("full", ["A105"], "a,b")
    assert cmd[-2:] == ["--skip-tags", "a,b"]

    _, cmd = ds.build_command("ssh", ["A105", "A106"])
    assert "-f" not in cmd


def test_build_command_rejects_bad_input():
    with pytest.raises(ValidationError):
        ds.build_command("full", ["A105"], "x; rm -rf /")
    with pytest.raises(ValidationError):
        ds.build_command("evil", ["A105"])


def test_only_one_active_deploy():
    first = ds.create_deploy_log(**KW)
    with pytest.raises(ds.DeployBusy):
        ds.create_deploy_log(**KW)
    ds.update_log(first, Status.SUCCESS)
    assert ds.create_deploy_log(**KW) != first


def test_parallel_creation_is_atomic():
    results = []

    def attempt():
        try:
            results.append(ds.create_deploy_log(**KW))
        except ds.DeployBusy:
            results.append(None)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is not None for r in results) == 1
    with session_scope() as db:
        assert db.query(DeployLog).count() == 1
