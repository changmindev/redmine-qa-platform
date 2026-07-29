"""세트 저장소 / 프로젝트 지침 테스트."""

from __future__ import annotations

import pytest

from redmine_report.services import storage


@pytest.fixture(autouse=True)
def _isolated_data(data_dir):
    """모든 테스트가 임시 data_dir 를 쓰도록 강제."""
    return data_dir


def test_project_prompt_roundtrip():
    storage.save_project_prompt("proj-a", "A팀 지침")
    assert storage.get_project_prompt("proj-a") == "A팀 지침"
    assert storage.get_project_prompt("proj-b") == ""


def test_saving_blank_prompt_removes_entry():
    storage.save_project_prompt("proj-a", "지침")
    storage.save_project_prompt("proj-a", "   ")
    assert storage.get_project_prompt("proj-a") == ""


def test_new_set_id_prefix_and_slug():
    assert storage.new_set_id("master", "demo_shop").startswith("m_demo-shop_")
    assert storage.new_set_id("run", "demo_shop").startswith("r_demo-shop_")
    assert storage.new_set_id("master", None).startswith("m_proj_")


def test_kind_of():
    assert storage.kind_of("r_x_1") == "runs"
    assert storage.kind_of("m_x_1") == "masters"


@pytest.mark.parametrize(
    "bad_id",
    ["../../etc/passwd", "m_../escape", "x_notallowed", "", "m_" + "a" * 200],
)
def test_validate_id_rejects_path_traversal(bad_id):
    with pytest.raises(storage.StorageError):
        storage.validate_id(bad_id)


def test_read_set_returns_empty_for_invalid_id():
    assert storage.read_set("../../secret") == {}


def test_set_roundtrip_and_summary():
    record = storage.build_set(
        {
            "type": "master",
            "project": "proj",
            "name": "회귀 세트",
            "mode": "full",
            "cases": [
                {
                    "id": 1,
                    "result": {
                        "cases": [
                            {"status": "pass"},
                            {"status": "fail"},
                            {},  # 상태 없음 → untested
                        ]
                    },
                }
            ],
        }
    )
    storage.write_set(record)

    loaded = storage.read_set(record["id"])
    assert loaded["name"] == "회귀 세트"

    summary = storage.summarize(loaded)
    assert summary["issues"] == 1
    assert summary["rows"] == 3
    assert summary["status_counts"]["pass"] == 1
    assert summary["status_counts"]["fail"] == 1
    assert summary["status_counts"]["untested"] == 1


def test_summarize_handles_checklist_mode():
    record = storage.build_set(
        {
            "name": "체크",
            "mode": "checklist",
            "cases": [{"id": 1, "result": {"checklist": [{"status": "pass"}, {}]}}],
        }
    )
    assert storage.summarize(record)["rows"] == 2


def test_build_set_preserves_created_at_on_update():
    first = storage.build_set({"name": "세트", "project": "p"})
    storage.write_set(first)

    second = storage.build_set({"id": first["id"], "name": "세트 수정"}, first)
    assert second["created_at"] == first["created_at"]
    assert second["name"] == "세트 수정"


def test_list_sets_filters_by_project_and_type():
    storage.write_set(storage.build_set({"type": "master", "project": "p1", "name": "m1"}))
    storage.write_set(storage.build_set({"type": "run", "project": "p1", "name": "r1"}))
    storage.write_set(storage.build_set({"type": "master", "project": "p2", "name": "m2"}))

    assert len(storage.list_sets()) == 3
    assert len(storage.list_sets(project_id="p1")) == 2
    assert len(storage.list_sets(project_id="p1", set_type="run")) == 1
    assert storage.list_sets(project_id="p2")[0]["name"] == "m2"


def test_delete_set():
    record = storage.build_set({"name": "삭제 대상"})
    storage.write_set(record)
    storage.delete_set(record["id"])
    assert storage.read_set(record["id"]) == {}
    storage.delete_set(record["id"])  # 없는 것을 지워도 오류 없음
