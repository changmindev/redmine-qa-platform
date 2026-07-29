"""첨부 문서에서 기획 내용을 뽑아낸다.

기획서는 순수 텍스트로 오는 일이 거의 없다. PDF, 워드, 화면 캡처, Figma 링크로 온다.
그래서 **무엇이 들어오든 텍스트로 바꿔** 기존 생성 흐름에 그대로 태운다.
추출 결과는 화면의 입력칸에 채워 넣어 사용자가 눈으로 확인하고 고칠 수 있게 한다
(모델에 무엇이 들어가는지 감추지 않기 위해서다).

이미지는 텍스트가 아니므로 Claude 비전으로 '읽어서 글로 옮긴' 뒤 같은 흐름을 탄다.
이 경로는 API 키가 있을 때만 동작한다(구독형 CLI 는 이미지를 받지 못한다).
"""

from __future__ import annotations

import base64
import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

from ..config import settings

log = logging.getLogger(__name__)


class ExtractError(RuntimeError):
    """첨부 처리 실패 — 사용자에게 그대로 보여줄 메시지를 담는다."""


#: 확장자 → 처리 방식
TEXT_EXTS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".yml", ".yaml"}
HTML_EXTS = {".html", ".htm"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
PDF_EXTS = {".pdf"}
DOCX_EXTS = {".docx"}

SUPPORTED_EXTS = TEXT_EXTS | HTML_EXTS | IMAGE_EXTS | PDF_EXTS | DOCX_EXTS

IMAGE_MEDIA = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

#: 압축 폭탄 방지 — docx 내부 XML 합계 상한
_MAX_UNCOMPRESSED = 60 * 1024 * 1024


def _ext_of(filename: str) -> str:
    match = re.search(r"(\.[A-Za-z0-9]+)$", filename or "")
    return match.group(1).lower() if match else ""


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "cp949", "euc-kr", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class _TextOnly(HTMLParser):
    """HTML 에서 사람이 읽는 글자만 남긴다."""

    SKIP = {"script", "style", "noscript", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def _from_html(data: bytes) -> str:
    parser = _TextOnly()
    parser.feed(_decode(data))
    return re.sub(r"\n{3,}", "\n\n", " ".join(parser.parts))


def _from_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 설치 환경에 따라 다름
        raise ExtractError(
            "PDF 를 읽으려면 pypdf 가 필요합니다. 'pip install pypdf' 후 다시 시도하세요."
        ) from exc

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise ExtractError(f"PDF 를 열지 못했습니다: {exc}") from exc

    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ExtractError("암호가 걸린 PDF 입니다. 암호를 푼 뒤 올려주세요.") from exc

    pages = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if text.strip():
            pages.append(f"[{number}쪽]\n{text.strip()}")

    if not pages:
        raise ExtractError(
            "PDF 에서 글자를 찾지 못했습니다. 스캔 이미지로 된 PDF 라면 "
            "캡처 이미지로 올리거나 원본 문서를 사용하세요."
        )
    return "\n\n".join(pages)


def _from_docx(data: bytes) -> str:
    """워드 문서에서 문단과 표 글자를 뽑는다. 외부 패키지 없이 zip + XML 로 처리한다."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ExtractError("워드 파일(.docx)이 아니거나 손상되었습니다.") from exc

    if sum(info.file_size for info in archive.infolist()) > _MAX_UNCOMPRESSED:
        raise ExtractError("문서가 너무 큽니다.")

    try:
        xml = archive.read("word/document.xml")
    except KeyError as exc:
        raise ExtractError("워드 문서 본문을 찾지 못했습니다. (.doc 은 .docx 로 저장해 주세요)") from exc

    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    root = ET.fromstring(xml)

    lines: list[str] = []
    for paragraph in root.iter(f"{{{ns['w']}}}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{{{ns['w']}}}t"))
        if text.strip():
            lines.append(text.strip())
    if not lines:
        raise ExtractError("워드 문서에서 글자를 찾지 못했습니다.")
    return "\n".join(lines)


def _from_image(filename: str, data: bytes) -> str:
    """화면 캡처·디자인 시안을 Claude 비전으로 읽어 글로 옮긴다."""
    from . import ai  # 순환 import 방지를 위해 지연 로딩

    if not (settings.anthropic_api_key and ai.anthropic is not None):
        raise ExtractError(
            "이미지 분석은 ANTHROPIC_API_KEY 를 설정한 경우에만 됩니다. "
            "(구독형 claude CLI 는 이미지를 받지 못합니다) "
            "화면 설명을 글로 적어 붙여넣어 주세요."
        )
    media_type = IMAGE_MEDIA.get(_ext_of(filename), "image/png")
    return ai.describe_image(media_type, base64.standard_b64encode(data).decode("ascii"))


def from_upload(filename: str, data: bytes) -> dict:
    """업로드된 파일 하나를 텍스트로 바꾼다."""
    limit = settings.max_upload_mb * 1024 * 1024
    if len(data) > limit:
        raise ExtractError(f"파일이 너무 큽니다. {settings.max_upload_mb}MB 이하만 올릴 수 있습니다.")
    if not data:
        raise ExtractError("빈 파일입니다.")

    ext = _ext_of(filename)
    if ext not in SUPPORTED_EXTS:
        raise ExtractError(
            f"지원하지 않는 형식입니다({ext or '확장자 없음'}). "
            "지원: PDF, 워드(docx), 텍스트/마크다운, CSV, JSON, HTML, 이미지"
        )

    if ext in PDF_EXTS:
        text, kind = _from_pdf(data), "pdf"
    elif ext in DOCX_EXTS:
        text, kind = _from_docx(data), "docx"
    elif ext in HTML_EXTS:
        text, kind = _from_html(data), "html"
    elif ext in IMAGE_EXTS:
        text, kind = _from_image(filename, data), "image"
    else:
        text, kind = _decode(data), "text"

    text = text.strip()
    if not text:
        raise ExtractError("내용을 찾지 못했습니다.")
    return {"text": text, "kind": kind, "source": filename}


# ── Figma ────────────────────────────────────────────────────

FIGMA_URL = re.compile(r"figma\.com/(?:file|design|proto|board)/([A-Za-z0-9]+)")


def is_figma_url(url: str) -> bool:
    return bool(FIGMA_URL.search(url or ""))


def _figma_node_id(url: str) -> str | None:
    match = re.search(r"[?&]node-id=([^&]+)", url or "")
    if not match:
        return None
    # URL 에는 1-23 처럼 하이픈으로 오지만 API 는 1:23 형식을 쓴다.
    return match.group(1).replace("-", ":", 1)


def _walk_figma(node: dict, depth: int = 0, out: list[str] | None = None) -> list[str]:
    """프레임 이름과 텍스트를 계층 그대로 뽑는다. 기획 의도가 이름에 담기는 경우가 많다."""
    out = [] if out is None else out
    node_type = node.get("type", "")
    name = (node.get("name") or "").strip()
    indent = "  " * min(depth, 6)

    if node_type == "TEXT":
        characters = (node.get("characters") or "").strip()
        if characters:
            out.append(f"{indent}{characters}")
    elif node_type in ("FRAME", "COMPONENT", "COMPONENT_SET", "INSTANCE", "SECTION", "GROUP"):
        if name and not name.startswith("_"):
            out.append(f"{indent}[{name}]")

    for child in node.get("children") or []:
        _walk_figma(child, depth + 1, out)
    return out


def from_figma(url: str) -> dict:
    """Figma 파일에서 프레임 구조와 텍스트를 가져온다."""
    match = FIGMA_URL.search(url or "")
    if not match:
        raise ExtractError("Figma 파일 주소가 아닙니다. (figma.com/design/... 형태)")
    if not settings.figma_token:
        raise ExtractError(
            "Figma 를 읽으려면 개인 액세스 토큰이 필요합니다. "
            "Figma > Settings > Security > Personal access tokens 에서 발급해 "
            ".env 의 FIGMA_TOKEN 에 넣어주세요."
        )

    file_key = match.group(1)
    params = {"depth": 8}
    node_id = _figma_node_id(url)
    if node_id:
        params["ids"] = node_id

    try:
        resp = requests.get(
            f"https://api.figma.com/v1/files/{file_key}",
            headers={"X-Figma-Token": settings.figma_token},
            params=params,
            timeout=settings.http_timeout,
        )
    except requests.RequestException as exc:
        raise ExtractError(f"Figma 에 연결하지 못했습니다: {exc}") from exc

    if resp.status_code == 403:
        raise ExtractError("Figma 토큰이 유효하지 않거나 이 파일에 접근 권한이 없습니다.")
    if resp.status_code == 404:
        raise ExtractError("Figma 파일을 찾을 수 없습니다. 주소를 확인해 주세요.")
    if resp.status_code >= 400:
        raise ExtractError(f"Figma 응답 오류 (HTTP {resp.status_code})")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise ExtractError("Figma 응답을 해석하지 못했습니다.") from exc

    document = payload.get("document") or {}
    lines = _walk_figma(document)
    if not lines:
        raise ExtractError("이 Figma 파일에서 텍스트를 찾지 못했습니다.")

    title = payload.get("name") or "Figma"
    body = "\n".join(lines)
    return {
        "text": f"[Figma: {title}]\n{body}",
        "kind": "figma",
        "source": title,
    }


def from_url(url: str) -> dict:
    """지금은 Figma 만 지원한다. 임의의 주소를 서버가 대신 요청하면
    사내망 내부 자원을 긁어오는 통로가 될 수 있어 의도적으로 막았다."""
    url = (url or "").strip()
    if not url:
        raise ExtractError("주소를 입력하세요.")
    if is_figma_url(url):
        return from_figma(url)

    host = (urlparse(url).hostname or "").lower()
    raise ExtractError(
        f"'{host or url}' 은(는) 지원하지 않습니다. 현재는 Figma 파일 주소만 읽을 수 있습니다. "
        "다른 문서는 파일로 올리거나 내용을 붙여넣어 주세요."
    )


def preview(text: str, limit: int = 400) -> str:
    """로그·응답용 짧은 미리보기."""
    flat = re.sub(r"\s+", " ", text).strip()
    return flat[:limit] + ("…" if len(flat) > limit else "")


__all__ = [
    "SUPPORTED_EXTS", "ExtractError", "from_figma", "from_upload",
    "from_url", "is_figma_url", "preview",
]
