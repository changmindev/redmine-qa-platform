"""첨부 문서 추출 테스트 — 파일 형식별 처리 · 한도 · Figma."""

from __future__ import annotations

import io
import zipfile

import pytest

from redmine_report.services import extract


def _docx(paragraphs: list[str]) -> bytes:
    """최소 형태의 .docx 를 만든다(테스트용)."""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


# ── 텍스트 계열 ──────────────────────────────────────────


def test_plain_text():
    result = extract.from_upload("spec.txt", "로그인 요구사항".encode())
    assert result["kind"] == "text"
    assert "로그인 요구사항" in result["text"]
    assert result["source"] == "spec.txt"


def test_markdown_and_json_are_text():
    assert extract.from_upload("a.md", b"# title")["kind"] == "text"
    assert extract.from_upload("a.json", b'{"a":1}')["kind"] == "text"


def test_cp949_korean_file_is_decoded():
    data = "한글 기획서".encode("cp949")
    assert "한글 기획서" in extract.from_upload("spec.txt", data)["text"]


def test_html_strips_tags_and_scripts():
    html = b"<html><head><style>p{color:red}</style></head><body><p>\xea\xb8\xb0\xed\x9a\x8d</p>" \
           b"<script>alert(1)</script></body></html>"
    text = extract.from_upload("spec.html", html)["text"]
    assert "기획" in text
    assert "alert" not in text
    assert "color:red" not in text


# ── 워드 ─────────────────────────────────────────────────


def test_docx_paragraphs():
    result = extract.from_upload("spec.docx", _docx(["첫 문단", "둘째 문단"]))
    assert result["kind"] == "docx"
    assert "첫 문단" in result["text"] and "둘째 문단" in result["text"]


def test_docx_without_body_is_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("other.xml", "<a/>")
    with pytest.raises(extract.ExtractError, match="본문"):
        extract.from_upload("spec.docx", buf.getvalue())


def test_corrupt_docx_is_rejected():
    with pytest.raises(extract.ExtractError):
        extract.from_upload("spec.docx", b"not a zip")


# ── 한도 · 형식 ──────────────────────────────────────────


def test_unsupported_extension():
    with pytest.raises(extract.ExtractError, match="지원하지 않는"):
        extract.from_upload("spec.exe", b"x")


def test_empty_file():
    with pytest.raises(extract.ExtractError, match="빈 파일"):
        extract.from_upload("spec.txt", b"")


def test_oversize_file(monkeypatch):
    monkeypatch.setattr(extract.settings, "max_upload_mb", 1, raising=False)
    with pytest.raises(extract.ExtractError, match="너무 큽니다"):
        extract.from_upload("spec.txt", b"x" * (2 * 1024 * 1024))


# ── 이미지 ───────────────────────────────────────────────


def test_image_requires_api_key(monkeypatch):
    monkeypatch.setattr(extract.settings, "anthropic_api_key", "", raising=False)
    with pytest.raises(extract.ExtractError, match="ANTHROPIC_API_KEY"):
        extract.from_upload("shot.png", b"\x89PNG\r\n\x1a\n fake")


def test_image_uses_vision_when_key_present(monkeypatch):
    from redmine_report.services import ai

    monkeypatch.setattr(extract.settings, "anthropic_api_key", "sk-test", raising=False)
    monkeypatch.setattr(ai, "anthropic", object(), raising=False)
    captured = {}

    def fake(media_type, b64):
        captured.update(media_type=media_type, b64=b64)
        return "로그인 화면. 이메일 입력칸과 [로그인] 버튼이 있다."

    monkeypatch.setattr(ai, "describe_image", fake)

    result = extract.from_upload("shot.png", b"\x89PNG fake")
    assert result["kind"] == "image"
    assert "로그인 화면" in result["text"]
    assert captured["media_type"] == "image/png"


# ── Figma ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.figma.com/design/abc123XYZ/My-Spec", True),
        ("https://figma.com/file/KEY9/Proto?node-id=1-23", True),
        ("https://example.com/design/abc", False),
    ],
)
def test_is_figma_url(url, expected):
    assert extract.is_figma_url(url) is expected


def test_figma_requires_token(monkeypatch):
    monkeypatch.setattr(extract.settings, "figma_token", "", raising=False)
    with pytest.raises(extract.ExtractError, match="토큰"):
        extract.from_figma("https://www.figma.com/design/abc123/Spec")


def test_figma_walks_frames_and_text(monkeypatch):
    monkeypatch.setattr(extract.settings, "figma_token", "figd_test", raising=False)

    document = {
        "type": "DOCUMENT",
        "children": [{
            "type": "FRAME", "name": "로그인 화면",
            "children": [
                {"type": "TEXT", "characters": "이메일"},
                {"type": "TEXT", "characters": "비밀번호"},
                {"type": "FRAME", "name": "_숨김", "children": []},
            ],
        }],
    }

    class Resp:
        status_code = 200
        def json(self):
            return {"name": "로그인 개편", "document": document}

    monkeypatch.setattr(extract.requests, "get", lambda *a, **kw: Resp())

    result = extract.from_figma("https://www.figma.com/design/abc123/Spec?node-id=1-23")
    assert result["kind"] == "figma"
    assert "[로그인 화면]" in result["text"]
    assert "이메일" in result["text"] and "비밀번호" in result["text"]
    assert "_숨김" not in result["text"]      # 밑줄로 시작하는 프레임은 제외


def test_figma_permission_error(monkeypatch):
    monkeypatch.setattr(extract.settings, "figma_token", "figd_test", raising=False)

    class Resp:
        status_code = 403
        def json(self):
            return {}

    monkeypatch.setattr(extract.requests, "get", lambda *a, **kw: Resp())
    with pytest.raises(extract.ExtractError, match="권한"):
        extract.from_figma("https://www.figma.com/design/abc123/Spec")


def test_non_figma_url_is_refused():
    """임의의 주소를 서버가 대신 요청하면 내부망 자원을 긁는 통로가 된다."""
    with pytest.raises(extract.ExtractError, match="지원하지 않습니다"):
        extract.from_url("http://192.168.0.1/admin")


def test_empty_url():
    with pytest.raises(extract.ExtractError, match="주소를 입력"):
        extract.from_url("   ")


def test_preview_truncates():
    assert extract.preview("가" * 500, limit=100).endswith("…")
