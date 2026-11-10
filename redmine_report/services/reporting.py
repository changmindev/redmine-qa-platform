"""분석 결과 → 리포트(HTML, 가능하면 PDF).

wkhtmltopdf(pdfkit)가 설치돼 있으면 PDF, 없으면 같은 내용의 HTML 을 저장한다.
이슈 제목 등 외부 문자열은 모두 HTML 이스케이프한다(원본은 f-string 으로 그대로 넣어
제목에 `<` 가 들어가면 레이아웃이 깨지고 스크립트가 주입될 수 있었다).
"""

from __future__ import annotations

import html
import logging
from pathlib import Path

from ..config import settings
from .analyzer import PRIORITY_ORDER
from .timeutil import local_now_str

log = logging.getLogger(__name__)

try:
    import pdfkit

    PDFKIT_AVAILABLE = True
except ImportError:
    PDFKIT_AVAILABLE = False

_PRIORITY_COLORS = {
    "즉시": "#ef4444",
    "긴급": "#ef4444",
    "높음": "#f59e0b",
    "보통": "#3b82f6",
    "낮음": "#94a3b8",
}
_URGENT = {"긴급", "즉시"}


def _e(value) -> str:
    """HTML 이스케이프."""
    return html.escape(str(value if value is not None else ""), quote=True)


def _status_colors() -> dict[str, str]:
    """설정된 상태명 → 색상. 원본은 상태명을 하드코딩해 색이 전혀 적용되지 않았다."""
    s = settings.statuses
    return {
        s["new"]: "#6366f1",
        s["progress"]: "#f59e0b",
        s["resolved"]: "#10b981",
        s["qa"]: "#3b82f6",
    }


def _fmt_change(pct: float) -> str:
    if pct > 0:
        return f'<span class="up">▲ {pct}%</span>'
    if pct < 0:
        return f'<span class="down">▼ {abs(pct)}%</span>'
    return '<span class="flat">— 0%</span>'


def _long_term_section(stats: dict) -> str:
    issues = stats.get("long_term_issues", [])
    days = settings.long_term_days
    if not issues:
        return f"""
  <h2>1. 미해결 장기 이슈 목록</h2>
  <p class="section-desc">{days}일 이상 미완료 이슈가 없습니다.</p>"""

    rows = ""
    for issue in issues[:20]:
        subject = str(issue["subject"])
        shown = subject[:60] + ("…" if len(subject) > 60 else "")
        cls = "pri-urgent" if issue["priority"] in _URGENT else ""
        rows += f"""
        <tr>
          <td class="num">#{_e(issue['id'])}</td>
          <td>{_e(shown)}</td>
          <td class="{cls}">{_e(issue['priority'])}</td>
          <td>{_e(issue['status'])}</td>
          <td>{_e(issue['assignee'])}</td>
          <td class="num days-warn">{_e(issue['days_open'])}일</td>
        </tr>"""

    more = ""
    if len(issues) > 20:
        more = f'<p class="section-note">외 {len(issues) - 20}건 (상위 20건만 표시)</p>'

    return f"""
  <h2>1. 미해결 장기 이슈 목록</h2>
  <p class="section-desc">{days}일 이상 미완료 · 총 <b>{len(issues)}건</b> — 회의 액션 아이템으로 활용</p>
  <table class="impact"><thead><tr>
    <th>#</th><th>제목</th><th>우선순위</th><th>상태</th><th>담당자</th><th>경과일</th>
  </tr></thead><tbody>{rows}</tbody></table>{more}"""


def _priority_avg_section(stats: dict) -> str:
    pri_days = stats.get("priority_avg_days", {})
    if not pri_days:
        return """
  <h2>2. 우선순위별 평균 처리시간</h2>
  <p class="section-desc">해결·QA 완료 건이 없어 산출할 수 없습니다.</p>"""

    max_days = max(pri_days.values()) or 1
    ordered = [(p, pri_days[p]) for p in PRIORITY_ORDER if p in pri_days]
    ordered += [(p, v) for p, v in pri_days.items() if p not in PRIORITY_ORDER]

    bars = ""
    for priority, days in ordered:
        pct = round(days / max_days * 100, 1)
        color = _PRIORITY_COLORS.get(priority, "#6366f1")
        bars += f"""
        <div class="bar-row">
          <span class="bar-label">{_e(priority)}</span>
          <div class="bar-track"><div class="bar-fill" style="width:{pct}%;background:{color}"></div></div>
          <span class="bar-val"><b>{days}</b>일</span>
        </div>"""

    return f"""
  <h2>2. 우선순위별 평균 처리시간</h2>
  <p class="section-desc">해결·QA 완료 건 기준 — 긴급 이슈 대응 속도 확인</p>
  {bars}"""


def _bug_trend_section(stats: dict) -> str:
    trend = stats.get("bug_trend", {})
    monthly = trend.get("monthly", {})
    if not monthly:
        return """
  <h2>3. 버그 증감 추이</h2>
  <p class="section-desc">기간 내 버그 트래커 이슈가 없습니다.</p>"""

    rows = "".join(
        f'<tr><td>{_e(month)}</td><td class="num">{count}건</td></tr>'
        for month, count in monthly.items()
    )
    return f"""
  <h2>3. 버그 증감 추이</h2>
  <p class="section-desc">버그 트래커 이슈 월별 생성 · 전월 대비 {_fmt_change(trend.get('change_pct', 0))}
    ({trend.get('previous', 0)}건 → {trend.get('current', 0)}건)</p>
  <table class="compact"><thead><tr><th>월</th><th>신규 버그</th></tr></thead>
  <tbody>{rows}</tbody></table>"""


def _reopen_section(stats: dict) -> str:
    reopen = stats.get("reopen", {})
    rate = reopen.get("reopen_rate", 0)
    rate_cls = "rate-warn" if rate >= 15 else ("rate-ok" if rate <= 5 else "")
    return f"""
  <h2>4. 이슈 재오픈율</h2>
  <p class="section-desc">완료 후 재오픈된 건 / 완료 이력 건 — 개발팀 협업 품질 지표</p>
  <div class="kpi-grid kpi-3">
    <div class="kpi"><div class="val {rate_cls}">{rate}%</div><div class="lbl">전체 재오픈율</div></div>
    <div class="kpi"><div class="val">{reopen.get('reopen_count', 0)}</div><div class="lbl">재오픈 건수</div></div>
    <div class="kpi"><div class="val">{reopen.get('ever_closed', 0)}</div><div class="lbl">완료 이력 건수</div></div>
  </div>
  <p class="section-note">버그 재오픈: {reopen.get('bug_reopen_count', 0)}건 /
    {reopen.get('bug_ever_closed', 0)}건 ({reopen.get('bug_reopen_rate', 0)}%)</p>"""


def build_html(
    stats: dict,
    date_from: str,
    date_to: str,
    project_name: str = "프로젝트",
    version_label: str = "all",
) -> str:
    s = settings.statuses
    assignee_rows = ""
    for name, row in sorted(stats["assignee_stats"].items(), key=lambda x: -x[1]["total"]):
        assignee_rows += f"""
        <tr>
          <td>{_e(name)}</td>
          <td class="num">{row['total']}</td>
          <td class="num">{row.get('new', 0)}</td>
          <td class="num">{row.get('progress', 0)}</td>
          <td class="num">{row.get('resolved', 0)}</td>
          <td class="num">{row.get('qa', 0)}</td>
          <td class="num">{_e(row['avg_days'])}</td>
        </tr>"""

    colors = _status_colors()
    total = stats["total"] or 1
    status_bars = ""
    for status, count in stats["status_count"].items():
        pct = round(count / total * 100, 1)
        color = colors.get(status, "#94a3b8")
        status_bars += f"""
        <div class="bar-row">
          <span class="bar-label">{_e(status)}</span>
          <div class="bar-track"><div class="bar-fill" style="width:{pct}%;background:{color}"></div></div>
          <span class="bar-val">{count}건 ({pct}%)</span>
        </div>"""

    ver = f" · 버전: {_e(version_label)}" if version_label and version_label != "all" else ""

    return f"""<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8">
<title>Redmine 이슈 리포트 — {_e(project_name)}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  /* 폰트는 OS 마다 있는 것이 다르다. 맥(Apple SD Gothic Neo) → 윈도우(Malgun Gothic)
     → 리눅스(Noto Sans KR) 순으로 찾는다. 하나만 적으면 다른 OS 에서 한글이 깨진다. */
  body {{ font-family:'Apple SD Gothic Neo','Malgun Gothic','Noto Sans KR',sans-serif; color:#1e293b; padding:40px; font-size:13px; }}
  h1 {{ font-size:22px; font-weight:700; }}
  h2 {{ font-size:14px; font-weight:600; color:#334155; margin:28px 0 8px;
        border-left:4px solid #6366f1; padding-left:10px; }}
  .meta {{ color:#64748b; font-size:12px; margin-top:4px; }}
  .section-desc {{ color:#64748b; font-size:12px; margin-bottom:10px; }}
  .section-note {{ color:#94a3b8; font-size:11px; margin-top:6px; }}
  .kpi-grid {{ display:flex; gap:14px; margin:20px 0; }}
  .kpi-grid.kpi-3 .kpi {{ flex:1; }}
  .kpi {{ flex:1; background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px;
           padding:14px; text-align:center; }}
  .kpi .val {{ font-size:26px; font-weight:700; color:#6366f1; }}
  .kpi .val.rate-warn {{ color:#ef4444; }}
  .kpi .val.rate-ok {{ color:#10b981; }}
  .kpi .lbl {{ font-size:11px; color:#64748b; margin-top:4px; }}
  .bar-row {{ display:flex; align-items:center; margin-bottom:8px; }}
  .bar-label {{ width:90px; font-size:12px; font-weight:600; }}
  .bar-track {{ flex:1; height:14px; background:#e2e8f0; border-radius:4px; overflow:hidden; }}
  .bar-fill {{ height:100%; border-radius:4px; }}
  .bar-val {{ width:90px; text-align:right; font-size:12px; color:#475569; }}
  table {{ width:100%; border-collapse:collapse; margin-top:8px; }}
  table.impact td:nth-child(2) {{ max-width:280px; }}
  table.compact {{ width:auto; min-width:200px; }}
  th {{ background:#f1f5f9; padding:8px 10px; font-size:11px; color:#475569;
        border-bottom:2px solid #e2e8f0; text-align:left; }}
  td {{ padding:7px 10px; border-bottom:1px solid #f1f5f9; font-size:12px; }}
  td.num {{ text-align:center; }}
  td.pri-urgent {{ color:#ef4444; font-weight:600; }}
  td.days-warn {{ color:#ef4444; font-weight:700; }}
  .up {{ color:#ef4444; font-weight:600; }}
  .down {{ color:#10b981; font-weight:600; }}
  .flat {{ color:#64748b; }}
  .divider {{ border:none; border-top:2px dashed #e2e8f0; margin:32px 0; }}
  .footer {{ margin-top:40px; font-size:11px; color:#94a3b8; text-align:center; }}
</style></head><body>
  <h1>🗂 Redmine 이슈 리포트</h1>
  <p class="meta">프로젝트: <b>{_e(project_name)}</b>{ver} &nbsp;|&nbsp;
     기간: {_e(date_from)} ~ {_e(date_to)} &nbsp;|&nbsp; 생성: {local_now_str('%Y년 %m월 %d일 %H:%M')}</p>
  <div class="kpi-grid">
    <div class="kpi"><div class="val">{stats['total']}</div><div class="lbl">전체 이슈</div></div>
    <div class="kpi"><div class="val">{stats['qa_count']}</div><div class="lbl">QA 완료</div></div>
    <div class="kpi"><div class="val">{stats['qa_rate']}%</div><div class="lbl">QA 통과율</div></div>
    <div class="kpi"><div class="val">{_e(stats['avg_days_total'])}</div><div class="lbl">평균 처리일</div></div>
    <div class="kpi"><div class="val">{stats['wip_count']}</div><div class="lbl">진행중</div></div>
  </div>

  {_long_term_section(stats)}
  {_priority_avg_section(stats)}
  {_bug_trend_section(stats)}
  {_reopen_section(stats)}

  <hr class="divider">
  <h2>부록: 상태별 분포</h2>{status_bars}
  <h2>부록: 담당자별 현황</h2>
  <table><thead><tr>
    <th>담당자</th><th>전체</th><th>{_e(s['new'])}</th><th>{_e(s['progress'])}</th>
    <th>{_e(s['resolved'])}</th><th>{_e(s['qa'])}</th><th>평균처리일</th>
  </tr></thead><tbody>{assignee_rows}</tbody></table>
  <div class="footer">본 리포트는 Redmine API를 통해 자동 생성되었습니다.</div>
</body></html>"""


def generate_report(
    stats: dict,
    date_from: str,
    date_to: str,
    project_name: str = "프로젝트",
    version_id: str = "all",
) -> Path:
    """리포트 파일을 만들고 경로를 반환한다(PDF 불가 시 HTML)."""
    out_dir = settings.report_output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = local_now_str("%Y%m%d_%H%M")
    ver_slug = f"_v{version_id}" if version_id and version_id != "all" else ""
    stem = f"redmine_report_{date_from}_{date_to}{ver_slug}_{ts}"

    html_doc = build_html(stats, date_from, date_to, project_name, version_id)

    if PDFKIT_AVAILABLE:
        pdf_path = out_dir / f"{stem}.pdf"
        options = {
            "encoding": "UTF-8",
            "quiet": "",
            "page-size": "A4",
            "margin-top": "10mm",
            "margin-bottom": "10mm",
            "margin-left": "10mm",
            "margin-right": "10mm",
            # 리포트에는 이슈 제목처럼 바깥에서 온 글이 들어간다. 이스케이프는 하지만,
            # wkhtmltopdf 가 스크립트를 돌리거나 로컬 파일을 읽을 길은 아예 닫는다
            # (pdfkit 1.0.0 CVE-2025-26240 — 고친 버전이 없다). 리포트는 스크립트가 없다.
            "disable-javascript": "",
            "disable-local-file-access": "",
        }
        try:
            pdfkit.from_string(html_doc, str(pdf_path), options=options)
            return pdf_path
        except Exception as exc:  # wkhtmltopdf 미설치/실행 실패 → HTML 로 폴백
            log.warning("PDF 생성 실패, HTML 로 대체합니다: %s", exc)

    html_path = out_dir / f"{stem}.html"
    html_path.write_text(html_doc, encoding="utf-8")
    return html_path
