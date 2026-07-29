"""템플릿 무결성 검사기 — 화면을 다시 디자인할 때 조용히 깨지는 것들을 잡는다.

검사 항목
  1. DOM 계약 : <script> 안에서 참조하는 id 가 마크업에 실제로 있는가
  2. 스타일   : 마크업/JS 가 쓰는 CSS 클래스가 스타일시트에 정의돼 있는가
  3. 문법     : <script> 안의 JS 가 문법적으로 파싱되는가 (node 가 있을 때만)

    python tools/check_dom_contract.py

실패 시 종료 코드 1 — CI 에 그대로 물릴 수 있다.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "redmine_report" / "templates"
CSS_DIR = ROOT / "redmine_report" / "static" / "css"

# 각 템플릿이 불러오는 스타일시트
STYLESHEETS = {
    "dashboard.html": ("base.css", "dashboard.css"),
    "testcases.html": ("base.css", "testcases.css"),
}

# JS 안에서 id 를 참조하는 방식들.
# 끝에 `)` 를 요구해 `getElementById('btn-' + m)` 같은 문자열 연결은 제외한다
# (동적으로 조립되는 id 는 정적으로 검증할 수 없다).
ID_PATTERNS = (
    re.compile(r"""getElementById\(\s*['"`]([\w-]+)['"`]\s*\)"""),
    re.compile(r"""\$\(\s*['"`]#([\w-]+)['"`]\s*\)"""),
    re.compile(r"""querySelector(?:All)?\(\s*['"`]#([\w-]+)['"`]\s*\)"""),
)

MARKUP_ID = re.compile(r"""\bid\s*=\s*['"]([\w-]+)['"]""")
CLASS_ATTR = re.compile(r"""class\s*=\s*["'`]([^"'`<>]*)["'`]""")
# id 패턴과 같은 이유로, 문자열을 이어붙여 만드는 클래스명은 제외한다.
CLASSLIST = re.compile(r"""classList\.(?:add|remove|toggle|contains)\(\s*["']([\w-]+)["']\s*[,)]""")
QUERY_CLASS = re.compile(r"""(?:querySelector(?:All)?|closest)\(\s*["'`]\.([\w-]+)""")
CSS_RULE = re.compile(r"""^\s*([^{}@/][^{}]*)\{""", re.M)

# 동적으로 만들어지거나 외부 라이브러리가 그리는 것들
DYNAMIC_IDS = {"sel-go", "flt-all", "flt-none", "cal-prev", "cal-next", "sel-all"}
# 상태 클래스는 JS 가 문자열로 조립한다(st-pass 등) — 정의는 스타일시트에 있다
IGNORED_CLASSES = {"hidden", "on", "off", "active", "show", "done", "err", "ok"}


def _scripts(source: str) -> str:
    return "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", source, re.S))


def _defined_css_classes(names: tuple[str, ...]) -> set[str]:
    defined: set[str] = set()
    for name in names:
        path = CSS_DIR / name
        if not path.exists():
            continue
        text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
        for selector in CSS_RULE.findall(text):
            defined |= set(re.findall(r"\.([\w-]+)", selector))
    return defined


def _used_classes(source: str) -> set[str]:
    used: set[str] = set()
    for raw in CLASS_ATTR.findall(source):
        cleaned = re.sub(r"\$\{[^}]*\}", " ", raw)
        used |= {t for t in cleaned.split() if re.fullmatch(r"[\w-]+", t)}
    used |= set(CLASSLIST.findall(source))
    used |= set(QUERY_CLASS.findall(source))
    return used


def _check_js_syntax(script: str, label: str) -> list[str]:
    """node 가 있으면 JS 문법을 검사한다. 없으면 조용히 건너뛴다."""
    node = shutil.which("node")
    if not node:
        return []
    # Jinja 표현식은 서버에서 값으로 치환되므로, 검사할 때만 자리를 채운다.
    cleaned = re.sub(r"\{\{.*?\}\}", "null", script, flags=re.S)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(cleaned)
        tmp = fh.name
    try:
        proc = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
        if proc.returncode != 0:
            first = (proc.stderr or "").strip().splitlines()
            return [f"{label}: JS 문법 오류 — {' / '.join(first[:4])}"]
        return []
    finally:
        Path(tmp).unlink(missing_ok=True)


# width/height 를 받지만 display 가 없어도 되는 클래스.
# 부모가 flex/grid 라 자동으로 블록화되는 것들 — 왜 안전한지 이유를 남겨 둔다.
INLINE_SIZE_ALLOW = {
    "toggle-track": "부모 .theme-toggle 이 flex",
    "toggle-thumb": "position:absolute",
    "dot": "부모 .gate-badge 가 inline-flex",
    "lib-div": "부모 .lib-bar 가 flex",
    "wl-line": "position:absolute",
    "flt-chk": "부모 .flt-chip 이 inline-flex",
    "sel-chk": "부모 .sel-row 가 flex",
    "chk": "표 셀 안의 체크박스(폼 요소)",
    "cal-day": "부모 .cal-grid 가 grid",
}

_SIZE = re.compile(r"(?<![\w-])(width|height)\s*:")
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)


def _inline_sizing_problems(source: str, sheets: tuple[str, ...]) -> list[str]:
    """inline 요소에 width/height 를 준 곳을 찾는다.

    <span> 은 기본이 display:inline 이고, inline 요소는 width/height 를 **무시한다**.
    그래서 막대그래프 같은 요소를 <span> 으로 두고 height 만 주면 높이 0 으로 접혀
    색이 통째로 안 보인다(실제로 이 프로젝트에서 한 번 겪은 버그다).
    """
    css = ""
    for name in sheets:
        path = CSS_DIR / name
        if path.exists():
            css += "\n" + re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)

    # 크기를 받는 클래스와, 어디서든 display/absolute 를 선언한 클래스를 따로 모은다.
    # (미디어쿼리에서만 width 를 주고 display 는 기본 규칙에 있는 경우가 흔하다)
    sized: set[str] = set()
    safe: set[str] = set()
    for match in _RULE.finditer(css):
        selector, body = match.group(1).strip(), match.group(2)
        target = selector.split(",")[0].strip().split()[-1]
        classes = re.findall(r"\.([\w-]+)", target)
        if _SIZE.search(body):
            sized.update(classes)
        if re.search(r"display\s*:", body) or re.search(r"position\s*:\s*(absolute|fixed)", body):
            safe.update(classes)

    problems, seen = [], set()
    for match in re.finditer(r"<span[^>]*class=[\"'`]([^\"'`]*)[\"'`]", source):
        raw = re.sub(r"\$\{[^}]*\}", " ", match.group(1))
        for cls in raw.split():
            if not re.fullmatch(r"[\w-]+", cls) or cls in seen:
                continue
            seen.add(cls)
            if cls in sized and cls not in safe and cls not in INLINE_SIZE_ALLOW:
                problems.append(
                    f"<span class='{cls}'> 에 width/height 를 줬지만 display 선언이 없습니다. "
                    "inline 요소는 크기가 무시되어 화면에서 사라집니다."
                )
    return problems


# 브라우저·표준 전역. 여기 없는 이름을 호출하는데 정의가 없으면 실행 중에 터진다.
_JS_GLOBALS = {
    "window", "document", "console", "localStorage", "sessionStorage", "fetch",
    "alert", "confirm", "prompt", "setTimeout", "clearTimeout", "setInterval",
    "clearInterval", "requestAnimationFrame", "encodeURIComponent", "decodeURIComponent",
    "JSON", "Object", "Array", "String", "Number", "Boolean", "Math", "Date", "RegExp",
    "Map", "Set", "Promise", "Error", "URLSearchParams", "FormData", "Blob", "File",
    "FileReader", "AbortController", "Event", "CustomEvent", "Intl", "parseInt",
    "parseFloat", "isNaN", "isFinite", "structuredClone", "queueMicrotask",
    "Chart", "getComputedStyle", "matchMedia", "navigator", "location", "history",
    "if", "for", "while", "switch", "catch", "return", "typeof", "function", "super",
}

# 호출처럼 보이지만 함수가 아닌 키워드들
_JS_KEYWORDS = {
    "async", "await", "new", "delete", "void", "in", "of", "do", "else", "try",
    "finally", "throw", "yield", "case", "default", "class", "extends", "import",
    "export", "const", "let", "var", "instanceof", "this",
}

_DECL = re.compile(
    r"""(?:function\s+([A-Za-z_$][\w$]*)|"""
    r"""(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=)"""
)
_CALL = re.compile(r"""(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(""")


def _strip_js_noise(src: str) -> str:
    """주석과 문자열 '내용'을 지운다. 템플릿 리터럴의 ${...} 안은 코드이므로 남긴다.

    이걸 하지 않으면 CSS 문자열의 `var(--x)` 나 한글 주석의 'AND(교집합)' 을
    함수 호출로 오해한다.
    """
    out: list[str] = []
    i, n = 0, len(src)

    def _prev_significant() -> str:
        for c in reversed("".join(out)):
            if not c.isspace():
                return c
        return ""

    # `/` 가 나눗셈인지 정규식 시작인지 가른다.
    # 앞 토큰이 값이면 나눗셈, 연산자·구분자면 정규식이다(표준 휴리스틱).
    _REGEX_PRECEDERS = set("(,=:[!&|?{};+-*%~^<>")
    _REGEX_KEYWORDS = ("return", "typeof", "case", "in", "of", "new", "delete",
                       "void", "instanceof", "do", "else", "yield", "await")

    def _regex_can_start() -> bool:
        joined = "".join(out).rstrip()
        if not joined:
            return True
        if joined[-1] in _REGEX_PRECEDERS:
            return True
        word = re.search(r"([A-Za-z_$][\w$]*)$", joined)
        return bool(word and word.group(1) in _REGEX_KEYWORDS)

    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        # 정규식 리터럴 — 안에 있는 따옴표를 문자열로 오해하면 뒤 코드가 통째로 날아간다
        if ch == "/" and nxt not in ("/", "*") and _regex_can_start():
            j = i + 1
            in_class = False
            while j < n:
                c = src[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    break
                elif c == "\n":
                    j = i   # 줄바꿈이 나오면 정규식이 아니었다 — 나눗셈으로 처리
                    break
                j += 1
            if j > i:
                while j < n and src[j].isalpha():   # gimsuy 플래그
                    j += 1
                out.append(" 0 ")
                i = j + 1 if j < n and src[j] == "/" else j
                continue

        if ch == "/" and nxt == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if ch in "\"'":
            quote = ch
            i += 1
            while i < n and src[i] != quote:
                i += 2 if src[i] == "\\" else 1
            i += 1
            out.append('""')
            continue
        if ch == "`":
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == "`":
                    i += 1
                    break
                if src[i] == "$" and i + 1 < n and src[i + 1] == "{":
                    out.append(" ")
                    i += 2
                    depth = 1
                    start = i
                    while i < n and depth:
                        if src[i] == "{":
                            depth += 1
                        elif src[i] == "}":
                            depth -= 1
                        i += 1
                    out.append(_strip_js_noise(src[start : i - 1]))
                    out.append(" ")
                    continue
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _undefined_calls(script: str) -> list[str]:
    """정의되지 않은 함수를 호출하는 곳을 찾는다.

    `node --check` 는 문법만 본다. 함수 이름이 없어도 문법은 멀쩡하므로
    실제로 눌러보기 전까지 아무도 모른다(실제로 이 프로젝트에서 겪은 사고다).
    """
    script = _strip_js_noise(script)
    declared = {name for pair in _DECL.findall(script) for name in pair if name}
    # 함수 매개변수·구조분해로 들어오는 이름은 선언으로 인정한다(느슨하게).
    declared |= set(re.findall(r"""(?:function\s*\w*\s*\(|\(\s*)([\w$]+)\s*(?:,|\)\s*=>)""", script))
    declared |= set(re.findall(r"""([\w$]+)\s*=>""", script))
    declared |= set(re.findall(r"""catch\s*\(\s*([\w$]+)""", script))

    called = set(_CALL.findall(script))
    unknown = sorted(called - declared - _JS_GLOBALS - _JS_KEYWORDS)
    return [
        f"정의되지 않은 함수를 호출합니다: {name}() — 실행 중 ReferenceError 가 납니다"
        for name in unknown
    ]


def check(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    script = _scripts(source)
    problems: list[str] = []

    referenced: set[str] = set()
    for pattern in ID_PATTERNS:
        referenced |= set(pattern.findall(script))
    declared = set(MARKUP_ID.findall(source))
    missing_ids = sorted(referenced - declared - DYNAMIC_IDS)
    problems += [f"JS 가 찾는 id 가 마크업에 없음: #{name}" for name in missing_ids]

    sheets = STYLESHEETS.get(path.name, ())
    defined = _defined_css_classes(sheets)
    if defined:
        unstyled = sorted(_used_classes(source) - defined - IGNORED_CLASSES)
        problems += [f"스타일 정의가 없는 클래스: .{name}" for name in unstyled]
        problems += _inline_sizing_problems(source, sheets)

    problems += _check_js_syntax(script, path.name)
    problems += _undefined_calls(script)
    return problems


def _say(message: str) -> None:
    """콘솔이 표현하지 못하는 글자가 있어도 죽지 않게 출력한다(cp949 등)."""
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        print(message.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def main() -> int:
    templates = sorted(TEMPLATE_DIR.glob("*.html"))
    if not templates:
        print(f"템플릿을 찾을 수 없습니다: {TEMPLATE_DIR}")
        return 1

    failed = False
    for path in templates:
        problems = check(path)
        if problems:
            failed = True
            _say(f"[FAIL] {path.name}")
            for problem in problems:
                _say(f"         {problem}")
        else:
            _say(f"[ OK ] {path.name}")

    if failed:
        print("\n마크업·스타일·JS 중 하나가 어긋났습니다. 위 항목을 맞춰주세요.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
