"""프롬프트 조립과 모델 응답 파싱 테스트."""

from __future__ import annotations

import pytest

from redmine_report.services import ai


def test_parse_json_plain():
    assert ai.parse_json('{"a": 1}') == {"a": 1}


def test_parse_json_strips_code_fence():
    text = '설명입니다.\n```json\n{"cases": []}\n```\n끝'
    assert ai.parse_json(text) == {"cases": []}


def test_parse_json_trims_surrounding_prose():
    assert ai.parse_json('결과: {"summary": "요약"} 이상입니다.') == {"summary": "요약"}


def test_parse_json_raises_on_garbage():
    with pytest.raises(ai.GenerationError):
        ai.parse_json("JSON 없음")


def test_parse_json_raises_on_non_object():
    with pytest.raises(ai.GenerationError):
        ai.parse_json("[1, 2, 3]")


def test_common_guidance_always_applies_even_when_project_blank():
    """공통 품질 기준은 프로젝트 지침 여부와 무관하게 항상 들어간다."""
    system = ai.build_system("full", "   ")
    assert "테스트 설계 기법" in system
    assert "경계값 분석" in system
    assert "P0 —" in system                 # 우선순위 정의도 고정
    assert "[[GUIDANCE]]" not in system
    assert "[[MIX]]" not in system


def test_project_guidance_appends_not_replaces():
    """프로젝트 지침은 공통 기준을 '대체'하지 않고 뒤에 덧붙는다."""
    system = ai.build_system("full", "결제 케이스는 무조건 P0")

    assert "결제 케이스는 무조건 P0" in system
    assert "테스트 설계 기법" in system      # 공통 기준이 살아 있어야 한다
    assert "이 프로젝트 추가 규칙" in system
    assert "숙련된 QA 엔지니어" in system    # 고정 양식도 유지


def test_notes_are_stripped_from_project_guidance():
    """'#' 로 시작하는 메모 줄은 모델에 전달되지 않는다."""
    guidance = "# 이건 설명이라 빠져야 함\n· 실제 규칙은 남아야 함\n#   들여쓴 메모도 제외"
    system = ai.build_system("full", guidance)

    assert "실제 규칙은 남아야 함" in system
    assert "이건 설명이라 빠져야 함" not in system
    assert "들여쓴 메모도 제외" not in system


def test_template_seed_is_all_notes_so_it_adds_nothing_until_edited():
    """'기본 지침 넣기' 시드는 전부 주석이라, 그대로 저장해도 프롬프트가 오염되지 않는다."""
    assert ai.strip_notes(ai.PROJECT_TEMPLATE_FULL) == ""
    assert ai.strip_notes(ai.PROJECT_TEMPLATE_CHECKLIST) == ""
    system = ai.build_system("full", ai.PROJECT_TEMPLATE_FULL)
    assert "이 프로젝트 추가 규칙" not in system


def test_issue_and_spec_use_different_mix():
    """이슈(회귀)와 기획서(신규 흐름)는 근거가 달라 배분도 달라야 한다."""
    issue = ai.build_system("full", None, source="issue")
    spec = ai.build_system("full", None, source="spec")

    assert "이슈 기반" in issue and "정상 40~50%" in issue
    assert "기획서 기반" in spec
    assert "최대 5개" in issue and "최대 5개" in spec


def test_core_purpose_only_keeps_judgeable_cases():
    """핵심 흐름은 '무검수 수행' 전제라 근거 없는 케이스를 만들지 않게 지시한다."""
    core = ai.build_system("full", None, source="spec", purpose="core")
    full = ai.build_system("full", None, source="spec", purpose="full")

    assert "핵심 흐름" in core
    assert "검수하지 않고 그대로 수행" in core
    assert "근거가 없는 케이스는 아예 만들지 마세요" in core
    assert "핵심 흐름" not in full


def test_core_issue_is_tighter_than_full():
    core = ai.build_system("full", None, source="issue", purpose="core")
    assert "최대 3개" in core
    assert "추측 금지" in core


def test_purpose_is_normalized():
    assert ai.normalize_purpose("core") == "core"
    assert ai.normalize_purpose("CORE") == "core"
    assert ai.normalize_purpose(None) == "full"
    assert ai.normalize_purpose("아무거나") == "full"


def test_evidence_rule_is_in_common_guidance():
    """근거 필드는 무검수 판단의 핵심이라 항상 지시돼야 한다."""
    for mode in ("full", "checklist"):
        system = ai.build_system(mode, None)
        assert "evidence" in system
        assert "지어내" in system      # 근거를 꾸며내지 말라는 경고


def test_evidence_is_required_in_schema():
    case_schema = ai._schema("full", more=False)["properties"]["cases"]["items"]
    assert "evidence" in case_schema["properties"]
    assert "evidence" in case_schema["required"]

    item_schema = ai._schema("checklist", more=False)["properties"]["checklist"]["items"]
    assert "evidence" in item_schema["required"]


def test_more_mode_caps_additional_items():
    system = ai.build_system("full", None, more=True)
    assert "추가 생성" in system
    assert "최대 5개까지 추가" in system


def test_checklist_common_guidance():
    system = ai.build_system("checklist", None)
    assert "분류 기준" in system
    assert "기본 —" in system and "엣지 —" in system and "중요 —" in system
    assert "[[MIX]]" not in system


def test_build_system_json_hint_only_for_cli():
    assert "JSON 형식으로만" not in ai.build_system("full", None, json_hint=False)
    assert "JSON 형식으로만" in ai.build_system("full", None, json_hint=True)


def test_schema_shapes():
    full = ai._schema("full", more=False)
    assert "cases" in full["properties"] and "summary" in full["properties"]

    more = ai._schema("checklist", more=True)
    assert "checklist" in more["properties"] and "summary" not in more["properties"]


def test_build_prompt_includes_comments_and_custom_fields():
    issue = {
        "id": 42,
        "subject": "결제 실패",
        "description": "카드 결제 시 오류",
        "tracker": {"name": "버그"},
        "priority": {"name": "긴급"},
        "status": {"name": "신규"},
        "custom_fields": [{"name": "브라우저", "value": "Chrome 120"}],
        "journals": [{"notes": "재현 100%", "user": {"name": "QA"}, "created_on": "2026-07-01"}],
        "attachments": [{"filename": "screenshot.png"}],
        "relations": [{"id": 1}],
    }
    prompt = ai.build_prompt(issue)

    assert "[이슈 #42] 결제 실패" in prompt
    assert "카드 결제 시 오류" in prompt
    assert "브라우저: Chrome 120" in prompt
    assert "재현 100%" in prompt
    assert "screenshot.png" in prompt
    assert "[연관 이슈] 1건" in prompt


def test_build_prompt_without_description():
    assert "(설명 없음)" in ai.build_prompt({"id": 1, "subject": "제목만"})


def test_long_comments_are_truncated():
    issue = {
        "id": 1,
        "subject": "s",
        "journals": [{"notes": "가" * 5000, "user": {"name": "u"}, "created_on": "2026-07-01"}],
    }
    assert "…(중략)" in ai.build_prompt(issue)


def test_generate_for_issue_returns_error_dict_when_no_backend(monkeypatch):
    """백엔드가 없어도 예외를 던지지 않고 카드에 표시할 error 를 돌려준다."""
    monkeypatch.setattr(ai.settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(ai, "_cli_available", lambda: False)

    result = ai.generate_for_issue({"id": 1, "subject": "s"})
    assert "error" in result


def test_generate_for_issue_parses_backend_output(monkeypatch):
    monkeypatch.setattr(ai.settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(ai, "_cli_available", lambda: True)
    monkeypatch.setattr(ai, "_run_via_cli", lambda s, u: '{"summary":"요약","cases":[]}')

    result = ai.generate_for_issue({"id": 1, "subject": "s"})
    assert result == {"summary": "요약", "cases": []}


def test_cli_is_invoked_with_resolved_full_path(monkeypatch):
    """Windows 에서 subprocess 는 PATHEXT 를 해석하지 않는다.

    이름만 넘기면 `claude.CMD` 를 못 찾아 FileNotFoundError 가 난다.
    (실제로 이 프로젝트에서 CLI 생성이 통째로 죽었던 회귀다)
    """
    resolved = r"C:\fake\path\claude.CMD"
    monkeypatch.setattr(ai.shutil, "which", lambda name: resolved)
    captured = {}

    class Done:
        returncode = 0
        stdout = '{"result": "{\\"cases\\": []}"}'
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return Done()

    monkeypatch.setattr(ai.subprocess, "run", fake_run)
    ai._run_via_cli("system", "user")

    assert captured["cmd"][0] == resolved, "이름이 아니라 전체 경로를 넘겨야 한다"
    assert captured["cmd"][1:] == ["-p", "--output-format", "json"]


def test_cli_missing_gives_actionable_error(monkeypatch):
    monkeypatch.setattr(ai.shutil, "which", lambda name: None)
    with pytest.raises(ai.GenerationError, match="찾을 수 없습니다"):
        ai._run_via_cli("system", "user")


def test_backend_status_none(monkeypatch):
    monkeypatch.setattr(ai.settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(ai, "_cli_available", lambda: False)
    assert ai.backend_status() == {"backend": "none", "ready": False, "model": ""}
