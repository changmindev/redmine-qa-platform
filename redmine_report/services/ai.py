"""Redmine 이슈 → 테스트케이스/체크리스트 생성 (Claude).

백엔드 2가지
- SDK  : ANTHROPIC_API_KEY 가 있으면 종량제 API 사용. 구조화 출력(JSON Schema)으로
         스키마를 강제하므로 파싱 실패가 사실상 없다.
- CLI  : 키가 없으면 로컬에 로그인된 `claude` CLI(구독 인증)를 사용한다.
         이 경로는 자유 텍스트라 JSON 추출/검증이 필요하다.

프롬프트 구조 (2단)
- 고정 구간(화면에 보이지 않음)
    FORMAT_*  출력 양식 · COMMON_*  공통 품질 기준 · MIX_*  개수와 유형 배분
- 편집 구간
    프로젝트 지침. 공통 기준을 '대체'하지 않고 뒤에 '덧붙는다'.

축 두 개
- source  : 무엇을 근거로 만드는가 — issue(회귀 검증) / spec(신규 기능 흐름)
- purpose : 무엇에 쓸 것인가   — full(전체 검증) / core(무검수 자동 수행)

각 케이스는 evidence(근거가 된 원문)를 갖는다. 비어 있으면 '사람 검수 필요'라는 뜻이며,
이 신호로 무검수 자동 실행 가능 여부를 시스템이 판단한다.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from typing import Any

from ..config import settings

log = logging.getLogger(__name__)

try:
    import anthropic
except ImportError:  # 패키지 미설치 시에도 앱은 뜬다(카드에 오류만 표시)
    anthropic = None  # type: ignore[assignment]


class GenerationError(RuntimeError):
    """테스트케이스 생성 실패."""


# 프롬프트 조립용 자리표시자
#   [[GUIDANCE]] : 출력 양식 안에 '공통 기준 + 프로젝트 규칙' 이 들어갈 자리
#   [[MIX]]      : 공통 기준 안에 '개수·유형 배분' 이 들어갈 자리(근거에 따라 달라진다)
_GUIDE_MARK = "[[GUIDANCE]]"
_MIX_MARK = "[[MIX]]"

FORMAT_FULL = """당신은 숙련된 QA 엔지니어입니다.
주어진 Redmine 버그 이슈를 분석해 아래 표 양식의 테스트케이스를 작성합니다.

[각 테스트케이스(cases 항목) 필드]
- priority : 우선순위. 반드시 "P0" / "P1" / "P2" 중 하나.
- depth1 : 기능 대분류 (예: "회원")
- depth2 : 중분류 (예: "소셜 회원가입")
- depth3 : 소분류 (예: "네이버")
- depth4 : 세부 분류 (해당 없으면 빈 문자열 "")
- type : 테스트 유형 (정상 / 예외 / 경계 / UI-UX / 호환성 / 성능 중에서 택1)
- objective : 테스트 목적 (무엇을 검증하는지)
- precondition : 사전조건
- procedure : 테스트 절차 (여러 단계면 "1. ...\\n2. ..." 처럼 줄바꿈으로)
- expected : 기대결과
- evidence : 이 케이스의 근거가 된 원문 한 문장(짧게 인용). 근거가 없으면 빈 문자열 ""

주어진 내용(이슈 본문·코멘트·커스텀 필드·첨부, 또는 기획서)에 근거해 작성하고,
근거 없는 사실을 지어내지 마세요. 정보가 부족하면 합리적으로 가정하되 억지 항목은 만들지 마세요.

[[GUIDANCE]]"""

# ── 공통 품질 기준 : 테스트케이스 (고정 · 화면에 보이지 않음) ──
COMMON_FULL = """[테스트 설계 기법 — 아래 기법으로 케이스를 도출하세요]
1. 동등 분할 : 입력값을 '같게 동작하는 묶음'으로 나누고 각 묶음에서 대표값 1개씩 뽑으세요.
2. 경계값 분석 : 제한이 있는 값(길이·개수·금액·날짜)은 경계 바로 아래 / 경계 / 경계 바로 위를 확인하세요.
3. 상태 전이 : 상태가 있는 기능(로그인·주문·결제)은 정상 전이뿐 아니라 '허용되지 않는 전이'도 시도하세요.
4. 조합 : 조건이 2개 이상 얽히면 모든 조합이 아니라 '결과가 달라지는 조합'만 만드세요.
5. 오류 추정 : 실무에서 자주 깨지는 지점(중복 클릭·뒤로가기·새로고침·세션 만료·빈 목록)을 의도적으로 포함하세요.

[훑어야 할 관점 — 대상과 관련된 것만 고르되, 근거 없이 빠뜨리지 마세요]
· 흐름 : 정상 / 대체 경로 / 실패 후 복구
· 데이터 : 빈 값, 공백만, 최소·최대 길이, 형식 오류, 특수문자, 이모지, 다국어
· 권한 : 비로그인, 다른 사용자의 자원, 권한 없는 역할, 만료된 세션
· 상태 : 최초 진입, 데이터 0건, 대량 데이터, 이미 삭제된 대상
· 조작 : 중복 클릭·제출, 뒤로가기, 새로고침, 다른 탭에서 동시 변경
· 연결 : 네트워크 끊김, 응답 지연, 중간 실패 후 재시도
· 환경 : 대상에 명시된 브라우저·OS·기기·화면 크기
· 비기능 : 눈에 띄는 지연, 키보드만으로 조작, 오류 문구의 이해 가능성

[[MIX]]

[우선순위 기준]
· P0 — 실패하면 서비스를 못 쓰거나 돈·개인정보에 영향
       (로그인·인증, 결제, 데이터 유실, 핵심 화면 진입 불가)
· P1 — 주요 사용자 흐름에 포함되고 우회 방법이 마땅치 않은 기능
· P2 — 보조 기능, UI 표기, 사용 빈도가 낮은 설정
판단이 애매하면 '실패했을 때 사용자가 입는 피해'를 기준으로 한 단계 높게 잡으세요.

[기능 분류(depth) 규칙]
· depth1 = 서비스 대분류 (회원, 학습, 결제, 설정 …)
· depth2 = 화면 또는 기능 묶음
· depth3 = 세부 동작
· depth4 = 조건·변형 (없으면 빈 문자열)
같은 기능은 항상 같은 분류 이름을 쓰세요. 표를 정렬했을 때 흩어지면 안 됩니다.

[작성 품질 기준]
· 한 케이스는 한 가지만 검증합니다. "그리고 / 또한" 으로 검증을 붙이지 마세요.
· 각 케이스는 독립적으로 수행 가능해야 합니다(앞 케이스의 결과에 의존 금지).
· procedure 는 3~7단계, 각 단계는 하나의 행동만. 입력값은 실제 값으로 적으세요.
  ("잘못된 이메일" 이 아니라 "test@" 처럼)
· precondition 은 재현에 꼭 필요한 상태만 (계정 종류, 데이터 유무, 설정 값).
· expected 는 화면에서 눈으로 확인 가능한 결과로. 무엇이 어디에 어떻게 보이는지 쓰세요.
  ("정상 동작한다" 금지 → "'이메일 형식이 올바르지 않습니다' 문구가 입력칸 아래 빨간색으로 표시된다")
· 절차와 기대결과가 사실상 같은 케이스는 하나로 합치세요.
· 주어진 내용에 없는 기능을 지어내지 마세요. 확인이 필요하면 그 자체를 검증 케이스로 만드세요.

[근거(evidence) 표시 — 사람 검수 없이 바로 수행할 수 있는지 가르는 기준입니다]
· 각 케이스의 evidence 에는 그 케이스를 만들게 한 **원문을 짧게 한 문장만 인용**하세요.
  (이슈 본문·코멘트·커스텀 필드, 또는 기획서의 문장)
· 원문에 근거가 없고 일반적인 QA 관행으로 넣은 케이스는 evidence 를 **빈 문자열 ""** 로 두세요.
· 없는 문장을 지어내서 채우지 마세요. 빈 문자열이 정직한 답이며, 그렇게 표시된 케이스는
  사람이 따로 확인하게 됩니다. 근거를 꾸며내면 그 확인 절차가 무너집니다.
모든 값은 한국어로 작성합니다."""

FORMAT_CHECKLIST = """당신은 숙련된 QA 엔지니어입니다.
주어진 Redmine 버그 이슈를 읽고, 실무에서 바로 체크하며 확인할 수 있는 '간략한 체크리스트'를 만듭니다.

각 항목은 한 줄의 간결한 확인 문장으로 작성합니다. (예: "회원가입 후 홈으로 이동하는지 확인")
주어진 내용에 근거해 작성하고, 근거 없는 사실을 지어내지 마세요.

[[GUIDANCE]]"""

# ── 공통 품질 기준 : 체크리스트 (고정 · 화면에 보이지 않음) ──
COMMON_CHECKLIST = """[분류 기준]
· 기본 — 이 기능이 의도대로 동작하는지 (정상 흐름)
· 엣지 — 경계값, 빈 값, 예외 입력, 권한 없음, 네트워크 오류
· 중요 — 놓치면 서비스·데이터·돈에 피해가 가는 항목 (버그 재발, 결제, 개인정보, 데이터 유실)

[훑어야 할 관점 — 대상과 관련된 것만]
· 흐름 : 정상 / 대체 경로 / 실패 후 복구
· 데이터 : 빈 값, 최소·최대, 형식 오류, 특수문자
· 권한 : 비로그인, 권한 없는 역할, 만료된 세션
· 상태 : 데이터 0건, 대량 데이터, 이미 삭제된 대상
· 조작 : 중복 클릭, 뒤로가기, 새로고침
· 연결 : 네트워크 끊김, 응답 지연

[[MIX]]

[작성 기준]
· 한 항목 = 한 번의 확인. "~인지 확인" 으로 끝내세요.
· 무엇을 어디서 어떻게 보는지가 드러나야 합니다.
  ("로그인 확인" 금지 → "잘못된 비밀번호 5회 입력 시 계정이 잠기고 안내 문구가 뜨는지 확인")
· 실행 순서대로 정렬하세요 (진입 → 정상 → 예외 → 복구).
· 서로 같은 뜻의 항목은 하나로 합치세요.
· 주어진 내용에 없는 기능을 지어내지 마세요.

[근거(evidence) 표시 — 사람 검수 없이 바로 수행할 수 있는지 가르는 기준입니다]
· 각 항목의 evidence 에 그 항목을 만들게 한 **원문을 짧게 한 문장만 인용**하세요.
· 원문에 근거가 없으면 evidence 를 **빈 문자열 ""** 로 두세요. 지어내지 마세요.
모든 출력은 한국어로 작성합니다."""

FORMAT_MORE_FULL = """당신은 숙련된 QA 엔지니어입니다.
주어진 이슈와 '이미 작성된 테스트케이스 목록'이 함께 주어집니다.
이미 있는 것과 겹치지 않는 '새로운' 테스트케이스만 같은 표 양식으로 추가하세요.

각 항목 필드:
- priority : "P0"/"P1"/"P2" 중 하나
- depth1~depth4 : 기능 분류 계층 (없으면 "")
- type : 정상/예외/경계/UI-UX/호환성/성능 중 택1
- objective, precondition, procedure, expected
- evidence : 근거가 된 원문 한 문장(짧게 인용). 근거가 없으면 빈 문자열 ""

[[GUIDANCE]]"""

FORMAT_MORE_CHECKLIST = """당신은 숙련된 QA 엔지니어입니다.
주어진 이슈와 '이미 도출된 체크리스트 항목'이 함께 주어집니다.
이미 있는 항목과 겹치지 않는 '새로운' 한 줄 체크 항목만 추가로 만드세요.
- category 는 기본/엣지/중요 중 하나.

[[GUIDANCE]]"""


# ── 개수·유형 배분 : 무엇을 근거로 만드느냐에 따라 달라진다 ──
#   이슈 = 이미 일어난 결함의 회귀 검증 → 예외·경계를 두껍게
#   기획서 = 아직 만들지 않은 기능 → 전체 흐름이 이어지는지가 먼저
# 생성 목적(purpose)
#   full : 전체 검증 — 요구사항을 빠짐없이 덮는다. 기획 리뷰·최초 QA 용.
#   core : 핵심 흐름 — 사람이 검수하지 않고 그대로 수행하는 것을 전제로,
#          '판정이 확실한' 케이스만 만든다. 자동 회귀 용.
#
#   core 에서 비율(90/10)은 지시하는 목표가 아니라 '판정 확실한 것만 남긴' 결과다.
#   비율 자체를 목표로 주면 모델이 요구사항 검증을 버리거나 빈 케이스로 숫자를 채운다.
MIX_FULL = {
    "issue": {
        "full": """[개수와 유형 배분 — 이슈 기반 · 전체 검증]
· 이슈 하나당 최대 5개. 단순한 이슈는 2~3개로 충분합니다.
  개수를 채우려고 억지 케이스를 만들지 마세요.
· 유형 배분(참고) : 정상 40~50% · 예외 20~30% · 경계 15~20% · UI-UX/호환성/성능 나머지
· 버그 이슈라면 보고된 조건에서 재발하지 않는지 확인하는 케이스를 P0 또는 P1 로 반드시 1개 포함하세요.
  같은 원인으로 깨질 수 있는 인접 기능이 있으면 1개까지 더 확인하세요.""",
        "core": """[개수와 유형 배분 — 이슈 기반 · 핵심 흐름]
이 세트는 **사람이 내용을 검수하지 않고 그대로 수행**하는 것을 전제로 합니다.
따라서 판정이 확실한 케이스만 만드세요.
· 보고된 결함이 재발하지 않는지 확인하는 케이스를 반드시 1개 포함하세요(P0 또는 P1).
· 그 밖에는 이 이슈가 건드린 주요 경로가 정상 동작하는지만 확인하세요.
· 예외는 이슈 본문·코멘트에 **결과가 명확히 적힌 것만** 만드세요. 추측 금지.
· 이슈당 최대 3개 (재발 확인 1 + 주요 경로 1~2).""",
    },
    "spec": {
        "full": """[개수와 유형 배분 — 기획서 기반 · 전체 검증]
· 문서에 적힌 기능·화면을 빠짐없이 덮는 것이 우선입니다.
  기능(화면) 하나당 최대 5개로 나눠 작성하고, 문서 전체를 덮을 때까지 기능 단위로 이어가세요.
· 먼저 전체 사용 흐름이 처음부터 끝까지 끊기지 않고 이어지는지를 정상 케이스로 덮고,
  그다음 문서가 정한 제약(필수값·형식·제한·권한)을 어겼을 때를 예외 케이스로 덮으세요.
· 문서가 규칙만 정하고 결과를 정하지 않은 부분도 확인 케이스로 만들되,
  근거가 없으므로 evidence 는 빈 문자열로 두세요.""",
        "core": """[개수와 유형 배분 — 기획서 기반 · 핵심 흐름]
이 세트는 **사람이 내용을 검수하지 않고 그대로 수행**하는 것을 전제로 합니다.
따라서 판정이 확실한 케이스만 만드세요.
· 전체 사용 흐름이 처음부터 끝까지 끊기지 않고 이어지는지를 정상 케이스로 먼저 덮으세요.
  흐름의 각 단계를 빠뜨리지 마세요.
· 예외는 문서가 **결과(문구·동작)까지 명확히 정의한 것만** 만드세요.
  규칙만 있고 위반 시 동작이 정의되지 않은 것(예: "8자 이상"만 적혀 있음)은 만들지 마세요.
· 문서에 근거가 없는 케이스는 아예 만들지 마세요. 모든 케이스의 evidence 가 채워져야 합니다.
· 기능(화면) 하나당 최대 5개.""",
    },
    "more": {
        "full": """[개수와 유형 배분 — 추가 생성]
· 이미 있는 항목과 겹치지 않는 '새로운' 케이스만, 최대 5개까지 추가하세요.
· 기존에 부족한 관점(예외·경계·권한·복구)을 우선 채우세요.
· 추가할 게 없으면 빈 배열을 반환하세요. 억지로 만들지 마세요.""",
        "core": """[개수와 유형 배분 — 추가 생성 · 핵심 흐름]
· 이미 있는 항목과 겹치지 않는 '새로운' 케이스만, 최대 3개까지 추가하세요.
· 원문에 근거가 있는 것만 추가하세요(evidence 가 반드시 채워져야 합니다).
· 추가할 게 없으면 빈 배열을 반환하세요.""",
    },
}

MIX_CHECKLIST = {
    "issue": {
        "full": """[개수와 배분 — 이슈 기반 · 전체 검증]
· 핵심만 추려 5개 안팎. 많아도 8개를 넘기지 마세요.
· 배분(참고) : 기본 절반 · 엣지 30% 내외 · 중요 나머지
· 버그 이슈라면 재발 확인 항목을 '중요' 로 반드시 포함하세요.""",
        "core": """[개수와 배분 — 이슈 기반 · 핵심 흐름]
이 세트는 사람이 검수하지 않고 그대로 수행하는 것을 전제로 합니다.
· 재발 확인 항목을 '중요' 로 1개 반드시 포함하세요.
· 그 밖에는 주요 경로가 정상 동작하는지만. 최대 3개.
· 원문에 결과가 명확히 적힌 것만 만드세요. 추측 금지.""",
    },
    "spec": {
        "full": """[개수와 배분 — 기획서 기반 · 전체 검증]
· 문서에 적힌 기능·화면을 빠짐없이 덮되, 기능(화면) 하나당 5개 안팎으로 나눠 작성하세요.
· 전체 흐름(기본)을 먼저 덮고, 문서가 정한 제약 위반을 엣지·중요로 덮으세요.""",
        "core": """[개수와 배분 — 기획서 기반 · 핵심 흐름]
이 세트는 사람이 검수하지 않고 그대로 수행하는 것을 전제로 합니다.
· 전체 사용 흐름을 '기본' 항목으로 빠짐없이 덮으세요.
· 엣지·중요는 문서가 결과까지 정의한 것만. 문서에 근거가 없으면 만들지 마세요.
· 기능(화면) 하나당 5개 안팎.""",
    },
    "more": {
        "full": """[개수와 배분 — 추가 생성]
· 이미 있는 항목과 겹치지 않는 '새로운' 항목만, 최대 5개까지 추가하세요.
· 추가할 게 없으면 빈 배열을 반환하세요.""",
        "core": """[개수와 배분 — 추가 생성 · 핵심 흐름]
· 겹치지 않는 새 항목만 최대 3개. 원문에 근거가 있는 것만.
· 추가할 게 없으면 빈 배열을 반환하세요.""",
    },
}


# ── 프로젝트 지침 편집 시드 ('기본 지침 넣기' 버튼이 넣어준다) ──
#   '#' 로 시작하는 줄은 프롬프트를 만들 때 제거되므로 메모로 남겨도 안전하다.
PROJECT_TEMPLATE_FULL = """# 이 프로젝트에만 적용할 규칙을 적어주세요.
# 출력 양식과 공통 품질 기준(설계 기법 · 우선순위 정의 · 작성 규칙)은 이미 고정으로 적용됩니다.
# 여기에는 '이 프로젝트라서 다른 점'만 적으면 됩니다. 비워두면 공통 기준만 적용됩니다.
# '#' 로 시작하는 줄은 메모로 취급되어 생성에 사용되지 않습니다.
#
# 아래는 예시입니다. 필요한 줄의 '#' 를 지우고 내용을 고쳐 쓰세요.
#
# · 소셜 로그인은 네이버·카카오·애플을 각각 별도 케이스로 만든다
# · 태블릿 전용 서비스이므로 가로/세로 회전 케이스를 포함한다
# · 오프라인 사용을 지원하므로 네트워크 끊김 케이스를 항상 넣는다
# · depth1 은 [회원 / 학습 / 결제 / 설정] 중에서만 고른다
# · 결제·환불 관련 케이스는 무조건 P0 으로 둔다
"""

PROJECT_TEMPLATE_CHECKLIST = """# 이 프로젝트에만 적용할 규칙을 적어주세요.
# 분류 기준(기본/엣지/중요)과 작성 규칙은 이미 고정으로 적용됩니다.
# '#' 로 시작하는 줄은 메모로 취급되어 생성에 사용되지 않습니다.
#
# 아래는 예시입니다. 필요한 줄의 '#' 를 지우고 내용을 고쳐 쓰세요.
#
# · 결제 관련 항목은 반드시 '중요' 로 분류한다
# · 태블릿 실기기에서만 확인 가능한 항목은 앞에 [실기기] 를 붙인다
"""

# CLI 경로에는 구조화 출력이 없으므로 JSON 형식을 프롬프트로 지시한다.
_JSON_HINT_FULL = """

반드시 아래 JSON 형식으로만, 다른 설명 없이 JSON 하나만 출력하세요:
{
  "summary": "이슈 한 줄 요약",
  "cases": [
    {"priority": "P0", "depth1": "", "depth2": "", "depth3": "", "depth4": "",
     "type": "정상", "objective": "", "precondition": "", "procedure": "", "expected": ""}
  ]
}"""

_JSON_HINT_CHECKLIST = """

반드시 아래 JSON 형식으로만, 다른 설명 없이 JSON 하나만 출력하세요:
{
  "summary": "이슈 한 줄 요약",
  "checklist": [{"category": "기본", "text": "확인할 한 줄 항목"}]
}"""

_CASE_PROPS = {
    "priority": {"type": "string", "enum": ["P0", "P1", "P2"]},
    "depth1": {"type": "string"},
    "depth2": {"type": "string"},
    "depth3": {"type": "string"},
    "depth4": {"type": "string"},
    "type": {
        "type": "string",
        "enum": ["정상", "예외", "경계", "UI-UX", "호환성", "성능"],
    },
    "objective": {"type": "string"},
    "precondition": {"type": "string"},
    "procedure": {"type": "string"},
    "expected": {"type": "string"},
    # 근거가 된 원문 인용. 근거가 없으면 빈 문자열 — 그 케이스는 사람이 검수한다.
    "evidence": {"type": "string"},
}

_CASE_SCHEMA = {
    "type": "object",
    "properties": _CASE_PROPS,
    "required": list(_CASE_PROPS),
    "additionalProperties": False,
}

_CHECKLIST_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": ["기본", "엣지", "중요"]},
        "text": {"type": "string"},
        "evidence": {"type": "string"},
    },
    "required": ["category", "text", "evidence"],
    "additionalProperties": False,
}


def _schema(mode: str, more: bool) -> dict:
    """구조화 출력용 JSON Schema. more=True 면 summary 없이 항목만."""
    if mode == "checklist":
        items = {"checklist": {"type": "array", "items": _CHECKLIST_ITEM_SCHEMA}}
    else:
        items = {"cases": {"type": "array", "items": _CASE_SCHEMA}}
    props: dict[str, Any] = dict(items)
    if not more:
        props["summary"] = {"type": "string"}
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def default_guidance(mode: str = "full") -> str:
    """'기본 지침 넣기' 버튼이 편집칸에 넣어줄 시드.

    공통 품질 기준이 아니라 **프로젝트가 덧붙일 규칙의 예시**다.
    공통 기준은 고정이라 화면에 노출하지 않는다.
    """
    return PROJECT_TEMPLATE_CHECKLIST if mode == "checklist" else PROJECT_TEMPLATE_FULL


def strip_notes(text: str | None) -> str:
    """'#' 로 시작하는 줄(메모)을 걷어낸다. 편집칸의 설명·예시가 모델에 들어가지 않게 한다."""
    lines = (text or "").splitlines()
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith("#")).strip()


#: 생성 목적 — 전체 검증(full) / 핵심 흐름(core)
PURPOSES = ("full", "core")


def normalize_purpose(value: str | None) -> str:
    return "core" if str(value or "").lower() == "core" else "full"


def build_system(
    mode: str,
    guidance: str | None,
    more: bool = False,
    json_hint: bool = False,
    source: str = "issue",
    purpose: str = "full",
) -> str:
    """최종 system 프롬프트를 조립한다.

        [출력 양식]  +  [공통 품질 기준 + 개수·배분]  +  [프로젝트 추가 규칙]
         고정·비공개        고정·비공개                     프로젝트별 편집

    프로젝트 지침은 공통 기준을 '대체'하지 않고 뒤에 덧붙는다.
    그래서 프로젝트 규칙을 바꿔도 설계 기법과 작성 규칙은 항상 살아 있다.

    source  : 'issue' | 'spec' (more=True 면 'more') — 무엇을 근거로 만드는가
    purpose : 'full'(전체 검증) | 'core'(핵심 흐름, 무검수 수행 전제)
    """
    checklist = mode == "checklist"
    if checklist:
        template = FORMAT_MORE_CHECKLIST if more else FORMAT_CHECKLIST
        common, mixes, hint = COMMON_CHECKLIST, MIX_CHECKLIST, _JSON_HINT_CHECKLIST
    else:
        template = FORMAT_MORE_FULL if more else FORMAT_FULL
        common, mixes, hint = COMMON_FULL, MIX_FULL, _JSON_HINT_FULL

    key = "more" if more else (source if source in mixes else "issue")
    block = common.replace(_MIX_MARK, mixes[key][normalize_purpose(purpose)])

    project = strip_notes(guidance)
    if project:
        block += (
            "\n\n[이 프로젝트 추가 규칙 — 위 기준과 충돌하면 이 규칙을 우선합니다]\n" + project
        )

    system = template.replace(_GUIDE_MARK, block)
    return system + hint if json_hint else system


def parse_json(text: str) -> dict:
    """모델 응답에서 JSON 본문을 추출·파싱한다(코드펜스/앞뒤 텍스트 방어)."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise GenerationError("모델 응답을 JSON으로 해석하지 못했습니다.") from exc
    if not isinstance(parsed, dict):
        raise GenerationError("모델 응답이 JSON 객체가 아닙니다.")
    return parsed


# ── 프롬프트 크기 제한 (코멘트가 매우 길 수 있다) ──────────────
_MAX_COMMENTS = 20
_MAX_COMMENT_LEN = 600
_MAX_EXTRA_TOTAL = 6000


def _issue_extra(issue: dict) -> str:
    """커스텀필드·코멘트·첨부·연관이슈를 프롬프트용 텍스트로 정리.

    재현절차·환경 등 핵심 맥락이 본문이 아니라 코멘트/필드에만 있는 경우가 많다.
    """
    parts: list[str] = []

    custom_fields = []
    for cf in issue.get("custom_fields") or []:
        name = str(cf.get("name", "")).strip()
        value = cf.get("value")
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value if str(v).strip())
        value = str(value or "").strip()
        if name and value:
            custom_fields.append(f"- {name}: {value}")
    if custom_fields:
        parts.append("[추가 정보(커스텀 필드)]\n" + "\n".join(custom_fields))

    notes = []
    for journal in issue.get("journals") or []:
        note = (journal.get("notes") or "").strip()
        if not note:
            continue
        who = (journal.get("user") or {}).get("name", "")
        when = (journal.get("created_on") or "")[:10]
        if len(note) > _MAX_COMMENT_LEN:
            note = note[:_MAX_COMMENT_LEN] + " …(중략)"
        notes.append(f"- ({when}, {who}) {note}")
    if notes:
        parts.append("[이슈 코멘트(오래된→최근)]\n" + "\n".join(notes[-_MAX_COMMENTS:]))

    attachments = []
    for att in issue.get("attachments") or []:
        filename = str(att.get("filename", "")).strip()
        desc = str(att.get("description") or "").strip()
        if filename:
            attachments.append(filename + (f" ({desc})" if desc else ""))
    if attachments:
        parts.append("[첨부 파일] " + ", ".join(attachments))

    relations = issue.get("relations") or []
    if relations:
        parts.append(f"[연관 이슈] {len(relations)}건 연결됨")

    text = "\n\n".join(parts)
    if len(text) > _MAX_EXTRA_TOTAL:
        text = text[:_MAX_EXTRA_TOTAL] + "\n…(이하 생략)"
    return text


def build_prompt(issue: dict) -> str:
    subject = issue.get("subject", "") or ""
    description = (issue.get("description") or "").strip()
    base = (
        f"[이슈 #{issue.get('id', '')}] {subject}\n"
        f"- 트래커: {(issue.get('tracker') or {}).get('name', '')}"
        f" / 우선순위: {(issue.get('priority') or {}).get('name', '')}"
        f" / 상태: {(issue.get('status') or {}).get('name', '')}\n\n"
        f"[설명]\n{description or '(설명 없음)'}"
    )
    extra = _issue_extra(issue)
    return base + ("\n\n" + extra if extra else "")


def _cli_available() -> bool:
    return shutil.which(settings.claude_cli) is not None


def _run_via_cli(system: str, user: str) -> str:
    """구독형 claude CLI(-p 프린트 모드)로 호출해 모델 텍스트를 반환한다.

    셸을 거치지 않고 인자 리스트로 실행한다(주입 위험 제거).
    다만 Windows 에서는 반드시 **shutil.which 로 찾은 전체 경로**를 넘겨야 한다.
    subprocess 는 PATHEXT 를 해석하지 않아, 이름만 주면 `claude.CMD` 를 찾지 못하고
    FileNotFoundError 가 난다(셸 없이 실행할 때의 함정).
    """
    executable = shutil.which(settings.claude_cli)
    if not executable:
        raise GenerationError(
            f"'{settings.claude_cli}' 명령을 찾을 수 없습니다. "
            "Claude Code 설치와 로그인 상태를 확인하세요."
        )
    cmd = [executable, "-p", "--output-format", "json"]
    try:
        proc = subprocess.run(  # noqa: S603 - 명령은 설정값 고정, 사용자 입력은 stdin
            cmd,
            input=f"{system}\n\n----\n\n{user}",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=settings.claude_cli_timeout,
        )
    except FileNotFoundError as exc:
        raise GenerationError(f"'{settings.claude_cli}' 명령을 찾을 수 없습니다.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GenerationError(
            f"claude CLI 응답이 {settings.claude_cli_timeout}초를 초과했습니다."
        ) from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "claude CLI 실행 실패").strip()[:400]
        raise GenerationError(detail)
    try:
        out = json.loads(proc.stdout)
    except ValueError as exc:
        raise GenerationError("claude CLI 응답을 해석하지 못했습니다.") from exc
    if out.get("is_error"):
        raise GenerationError(str(out.get("result", "claude CLI 오류"))[:400])
    return out.get("result", "")


def _run_via_sdk(system: str, user: str, schema: dict) -> str:
    """종량제 API 키(anthropic SDK)로 호출. 구조화 출력으로 JSON 스키마를 강제한다."""
    if anthropic is None:  # pragma: no cover - 의존성 미설치 환경
        raise GenerationError("anthropic 패키지가 설치되어 있지 않습니다.")
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=settings.anthropic_max_tokens,
        system=system,
        output_config={
            "effort": settings.anthropic_effort,
            "format": {"type": "json_schema", "schema": schema},
        },
        messages=[{"role": "user", "content": user}],
    )
    if getattr(resp, "stop_reason", None) == "refusal":
        raise GenerationError("모델이 이 요청에 대한 응답을 거부했습니다.")
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise GenerationError(
            "응답이 최대 토큰에 도달해 잘렸습니다. ANTHROPIC_MAX_TOKENS 를 늘려보세요."
        )
    return next((b.text for b in resp.content if b.type == "text"), "")


def backend_status() -> dict:
    """현재 사용 가능한 생성 백엔드 (프론트 안내·헬스체크용)."""
    if settings.anthropic_api_key and anthropic is not None:
        return {"backend": "api", "ready": True, "model": settings.anthropic_model}
    if _cli_available():
        return {"backend": "cli", "ready": True, "model": f"{settings.claude_cli} (구독 인증)"}
    return {"backend": "none", "ready": False, "model": ""}


def _generate(system_base: str, user: str, mode: str, more: bool) -> dict:
    """백엔드를 골라 호출하고 파싱된 dict 를 돌려준다."""
    use_sdk = bool(settings.anthropic_api_key) and anthropic is not None
    if use_sdk:
        text = _run_via_sdk(system_base, user, _schema(mode, more))
    elif _cli_available():
        text = _run_via_cli(system_base, user)
    else:
        raise GenerationError(
            "구독형 claude CLI 도 없고 ANTHROPIC_API_KEY 도 없습니다. "
            "Claude Code(claude 명령)를 설치하거나 .env 에 API 키를 넣으세요."
        )
    return parse_json(text)


def generate_for_issue(
    issue: dict,
    mode: str = "full",
    avoid: list | None = None,
    guidance: str | None = None,
    purpose: str = "full",
) -> dict:
    """이슈 하나에 대한 테스트케이스(full) 또는 체크리스트(checklist) 생성.

    avoid : 다른 이슈에서 이미 만든 항목 목록. 이와 중복되는 건 만들지 않는다.
    실패 시 {'error': ...} 를 담아 반환한다(카드 하나만 실패하고 나머지는 진행).
    """
    user = build_prompt(issue)
    if avoid:
        bullets = "\n".join(f"- {x}" for x in avoid if str(x).strip())
        if bullets:
            user += (
                "\n\n[다른 이슈에서 이미 만든 테스트케이스 목록 — 아래와 중복되거나 "
                "사실상 같은 케이스는 만들지 마세요]\n" + bullets
            )
    use_cli = not (settings.anthropic_api_key and anthropic is not None)
    system = build_system(mode, guidance, more=False, json_hint=use_cli,
                          source="issue", purpose=purpose)
    try:
        return _generate(system, user, mode, more=False)
    except GenerationError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # 네트워크/인증 등 예상 밖 오류도 카드에 표시
        log.exception("테스트케이스 생성 실패 (issue=%s)", issue.get("id"))
        return {"error": f"{type(exc).__name__}: {exc}"}


#: 기획서 본문 상한 (프롬프트가 지나치게 커지는 것 방지)
MAX_SPEC_CHARS = 20000

IMAGE_READER_SYSTEM = """당신은 QA 엔지니어를 돕는 분석가입니다.
주어진 이미지(기획 화면, 디자인 시안, 캡처)를 보고 **테스트케이스를 만들 수 있을 만큼** 자세히 글로 옮기세요.

다음을 빠짐없이 적으세요.
- 화면의 목적과 전체 구조(영역 구분)
- 보이는 모든 텍스트(라벨, 버튼명, 안내 문구, 오류 메시지, placeholder)
- 입력 요소와 제약(필수 여부, 형식, 글자 수, 선택지)
- 버튼·링크와 그것을 눌렀을 때 예상되는 동작
- 상태 표시(로딩, 비어 있음, 오류, 선택됨 등)와 조건부로 보이는 요소

보이지 않는 것을 추측해서 단정하지 마세요. 불확실하면 "(이미지상 불명확)" 이라고 적으세요.
설명 문장이 아니라 문서처럼 항목을 나눠 한국어로 작성하세요."""


def describe_image(media_type: str, b64_data: str) -> str:
    """이미지를 '테스트케이스를 뽑을 수 있는 글'로 옮긴다.

    구조화 출력이 필요 없는 자유 서술이므로 SDK 로만 호출한다.
    (구독형 CLI 는 표준입력으로 이미지를 받지 못한다.)
    """
    if anthropic is None:  # pragma: no cover
        raise GenerationError("anthropic 패키지가 설치되어 있지 않습니다.")
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=settings.anthropic_max_tokens,
        system=IMAGE_READER_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64",
                                                 "media_type": media_type, "data": b64_data}},
                    {"type": "text", "text": "이 화면을 위 기준에 따라 글로 옮겨 주세요."},
                ],
            }
        ],
    )
    if getattr(resp, "stop_reason", None) == "refusal":
        raise GenerationError("모델이 이 이미지에 대한 응답을 거부했습니다.")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    if not text.strip():
        raise GenerationError("이미지에서 읽어낸 내용이 없습니다.")
    return text


def build_spec_prompt(title: str, spec_text: str) -> str:
    body = (spec_text or "").strip()
    if len(body) > MAX_SPEC_CHARS:
        body = body[:MAX_SPEC_CHARS] + "\n…(이하 생략 — 문서가 길어 앞부분만 사용)"
    return f"[기획서 제목] {title or '(제목 없음)'}\n\n[문서 본문]\n{body}"


def generate_from_spec(
    title: str,
    spec_text: str,
    mode: str = "full",
    guidance: str | None = None,
    avoid: list | None = None,
    purpose: str = "full",
) -> dict:
    """기획서/요구사항 문서로 테스트케이스를 생성한다(Shift-Left).

    개발이 끝난 뒤 이슈를 보고 만드는 대신, 기획 단계 문서로 미리 만들어 두는 용도다.
    출력 형식은 이슈 기반 생성과 동일해서 같은 화면·같은 내보내기를 그대로 쓴다.
    """
    if not (spec_text or "").strip():
        return {"error": "기획서 내용이 비어 있습니다."}

    user = build_spec_prompt(title, spec_text)
    if avoid:
        bullets = "\n".join(f"- {x}" for x in avoid if str(x).strip())
        if bullets:
            user += "\n\n[이미 만든 케이스 — 아래와 중복되지 않게 만드세요]\n" + bullets

    use_cli = not (settings.anthropic_api_key and anthropic is not None)
    system = build_system(mode, guidance, more=False, json_hint=use_cli,
                          source="spec", purpose=purpose)
    try:
        return _generate(system, user, mode, more=False)
    except GenerationError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        log.exception("기획서 기반 생성 실패 (title=%s)", title)
        return {"error": f"{type(exc).__name__}: {exc}"}


def generate_more_for_issue(
    issue: dict,
    mode: str = "full",
    existing: list | None = None,
    guidance: str | None = None,
    purpose: str = "full",
) -> dict:
    """이미 도출된 항목(existing)과 겹치지 않는 '새' 항목만 추가 생성한다."""
    user = build_prompt(issue)
    if existing:
        bullets = "\n".join(f"- {x}" for x in existing if str(x).strip())
        if bullets:
            user += (
                "\n\n[이미 도출된 항목 — 아래와 의미가 겹치지 않는 '새로운' 것만 만드세요]\n"
                + bullets
            )
    use_cli = not (settings.anthropic_api_key and anthropic is not None)
    system = build_system(mode, guidance, more=True, json_hint=use_cli, purpose=purpose)
    try:
        return _generate(system, user, mode, more=True)
    except GenerationError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        log.exception("추가 생성 실패 (issue=%s)", issue.get("id"))
        return {"error": f"{type(exc).__name__}: {exc}"}
