from __future__ import annotations

import argparse
import calendar
from datetime import date, datetime, timedelta
import hashlib
from html import escape
import json
from pathlib import Path
import re
import shutil
import unicodedata
import zipfile

from shuttle_schedule_parser import parse_schedule, parse_schedule_workbook


DEFAULT_ADMIN_LABEL = "지정 관리자"
DEFAULT_ADMIN_PIN = "0066"
SCHEDULE_JSON_PATH = Path(__file__).resolve().parent.parent / "src" / "data" / "generated" / "schedule.json"
DRIVER_POSITIONS = {"요양보호사", "사무원", "대표"}
COMPANION_POSITIONS = {"요양보호사", "요양팀장", "사회복지사"}
EXCLUDED_DRIVERS = {"김중순"}
MONTHLY_WORKBOOK_RE = re.compile(r"^등송영표 ?(\d{1,2})월\.xlsx$")
DEFAULT_STAFF_ROSTER = [
    {"name": "강선진", "position": "사회복지사"},
    {"name": "강현애", "position": "요양보호사"},
    {"name": "김경애", "position": "요양보호사"},
    {"name": "김계순", "position": "조리원"},
    {"name": "김영숙", "position": "요양보호사"},
    {"name": "김용숙", "position": "요양보호사"},
    {"name": "김윤자", "position": "간호조무사"},
    {"name": "김은비", "position": "사회복지사"},
    {"name": "김중순", "position": "요양보호사"},
    {"name": "박정희", "position": "요양보호사"},
    {"name": "변해미", "position": "사회복지사"},
    {"name": "신은희", "position": "요양팀장"},
    {"name": "오주환", "position": "요양보호사"},
    {"name": "이기찬", "position": "사무원"},
    {"name": "임경호", "position": "요양보호사"},
    {"name": "장미란", "position": "시설장"},
    {"name": "정국현", "position": "요양보호사"},
    {"name": "정문조", "position": "요양보호사"},
    {"name": "최재영", "position": "대표"},
]


def dialog_id(prefix: str, vehicle_name: str) -> str:
    return f"{prefix}-{vehicle_name.replace('호차', '')}"


def format_clock(value: str | None) -> str:
    if not value:
        return "-"
    if value == "결석":
        return "결석"
    hour, minute = value.split(":")
    return f"{int(hour)}시 {minute}분"


def derive_base_date(parsed: dict) -> date:
    sheet_name = parsed["sheet_name"]
    if sheet_match := re.search(r"\((\d{2})\.(\d{1,2})\.(\d{1,2})\)", sheet_name):
        year, month, day = map(int, sheet_match.groups())
        return date(2000 + year, month, day)
    patterns = [
        re.search(r"\((\d{1,2})\.(\d{1,2})\)", sheet_name),
        re.search(r"\((\d{1,2})\D+(\d{1,2})\D*\)", Path(parsed["source_file"]).stem),
        re.search(r"(\d{1,2})[._-](\d{1,2})", Path(parsed["source_file"]).stem),
    ]
    current_year = datetime.now().year
    for match in patterns:
        if match:
            month, day = map(int, match.groups())
            return date(current_year, month, day)
    return date.today()


def load_staff_roster() -> list[dict[str, str]]:
    if not SCHEDULE_JSON_PATH.exists():
        return DEFAULT_STAFF_ROSTER
    try:
        payload = json.loads(SCHEDULE_JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_STAFF_ROSTER

    seen: dict[str, dict[str, str]] = {}
    for day in payload.get("days", []):
        for key in ("allEmployees", "workEmployees", "offEmployees"):
            for employee in day.get(key, []):
                name = (employee.get("name") or "").strip()
                position = (employee.get("position") or "").strip()
                if name and name not in seen:
                    seen[name] = {"name": name, "position": position}
    return sorted(seen.values(), key=lambda item: item["name"]) or DEFAULT_STAFF_ROSTER


def load_schedule_calendar_payload(base_date: date) -> dict[str, object]:
    if SCHEDULE_JSON_PATH.exists():
        try:
            payload = json.loads(SCHEDULE_JSON_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("months"), list) and isinstance(payload.get("days"), list):
                return payload
        except (OSError, json.JSONDecodeError):
            pass

    month_key = base_date.strftime("%Y-%m")
    _, last_day = calendar.monthrange(base_date.year, base_date.month)
    days = []
    for day_num in range(1, last_day + 1):
        current = date(base_date.year, base_date.month, day_num)
        days.append(
            {
                "date": current.isoformat(),
                "isSundayClosed": current.weekday() == 6,
                "isHoliday": False,
                "holidayName": "",
                "remarks": "",
            }
        )
    return {
        "months": [{"key": month_key, "label": f"{base_date.year}년 {base_date.month}월"}],
        "days": days,
    }


def is_monthly_schedule_workbook(path: str | Path) -> bool:
    normalized_name = unicodedata.normalize("NFC", Path(path).name)
    return bool(MONTHLY_WORKBOOK_RE.match(normalized_name))


def monthly_workbook_sort_key(path: str | Path) -> tuple[int, str]:
    normalized_name = unicodedata.normalize("NFC", Path(path).name)
    match = MONTHLY_WORKBOOK_RE.match(normalized_name)
    if not match:
        return (999, normalized_name)
    return (int(match.group(1)), normalized_name)


def latest_schedule_date(schedule_bundle: dict[str, dict], fallback: date) -> date:
    if not schedule_bundle:
        return fallback
    return date.fromisoformat(max(schedule_bundle))


def collect_resident_names(parsed: dict) -> set[str]:
    names: set[str] = set()
    for vehicle in parsed.get("vehicles", []):
        for side_key in ("pickup_rounds", "dropoff_rounds"):
            for round_data in vehicle.get(side_key, []):
                for entry in round_data.get("entries", []):
                    name = (entry.get("name") or "").strip()
                    if name:
                        names.add(name)
    for section_key in ("self_pickup", "self_dropoff"):
        for entry in parsed.get(section_key, {}).get("entries", []):
            name = (entry.get("name") or "").strip()
            if name:
                names.add(name)
    for entry in parsed.get("long_term_absences", []):
        name = (entry.get("name") or "").strip()
        if name:
            names.add(name)
    return names


def collect_schedule_files(primary_path: str | Path) -> list[Path]:
    primary = Path(primary_path).resolve()
    if is_monthly_schedule_workbook(primary):
        files = [
            path.resolve()
            for path in sorted(primary.parent.glob("*.xlsx"), key=monthly_workbook_sort_key)
            if is_monthly_schedule_workbook(path)
        ]
        if files:
            return files
    return [primary]


def build_schedule_bundle(primary_path: str | Path) -> tuple[dict[str, dict], dict]:
    primary = Path(primary_path).resolve()
    monthly_mode = is_monthly_schedule_workbook(primary)
    bundle: dict[str, dict] = {}
    primary_parsed: dict | None = None
    for path in collect_schedule_files(primary):
        try:
            parsed_sheets = parse_schedule_workbook(path)
        except zipfile.BadZipFile:
            continue
        for parsed in parsed_sheets:
            date_key = derive_base_date(parsed).isoformat()
            bundle[date_key] = parsed
            if not monthly_mode and path == primary and primary_parsed is None:
                primary_parsed = parsed
    if monthly_mode and bundle:
        primary_parsed = bundle[max(bundle)]
    if primary_parsed is None:
        primary_parsed = parse_schedule(primary)
        bundle[derive_base_date(primary_parsed).isoformat()] = primary_parsed
    return bundle, primary_parsed


def render_person_row(label: str, person: str | None, css_class: str) -> str:
    if not person:
        return ""
    return f"""
      <div class="person-row {css_class}">
        <span class="role-badge">{escape(label)}</span>
        <strong>{escape(person)}</strong>
      </div>
    """


def render_schedule_dialog(title: str, rounds: list[dict], dialog_name: str) -> str:
    round_blocks = []
    for round_data in rounds:
        items = []
        for entry in round_data["entries"]:
            state = "결석" if entry["absent"] else format_clock(entry["time"])
            note = f'<span class="entry-note">{escape(entry["note"])}</span>' if entry["note"] else ""
            items.append(
                f"""
                <li class="schedule-entry {'is-absent' if entry['absent'] else ''}">
                  <div class="entry-seq">{entry['sequence']}</div>
                  <div class="entry-body">
                    <div class="entry-main">
                      <strong>{escape(entry['name'] or '-')}</strong>
                      <span>{escape(state)}</span>
                    </div>
                    <div class="entry-sub">{escape(entry['address'] or '-')}</div>
                    {note}
                  </div>
                </li>
                """
            )

        round_blocks.append(
            f"""
            <section class="schedule-round">
              <h4>{round_data['round']}차</h4>
              <ul class="schedule-list">
                {''.join(items)}
              </ul>
            </section>
            """
        )

    return f"""
    <dialog id="{escape(dialog_name)}" class="modal-dialog">
      <div class="modal-shell">
        <div class="modal-header">
          <div>
            <p class="eyebrow">스케줄 보기</p>
            <h3>{escape(title)}</h3>
          </div>
          <button type="button" class="modal-close" data-close-dialog>닫기</button>
        </div>
        <div class="modal-content">
          {''.join(round_blocks) if round_blocks else '<p class="empty-copy">명단이 없습니다.</p>'}
        </div>
      </div>
    </dialog>
    """


def render_info_dialog(card: dict, dialog_name: str) -> str:
    return f"""
    <dialog id="{escape(dialog_name)}" class="modal-dialog">
      <div class="modal-shell narrow">
        <div class="modal-header">
          <div>
            <p class="eyebrow">차량 정보</p>
            <h3>{escape(card['display_name'])}</h3>
          </div>
          <button type="button" class="modal-close" data-close-dialog>닫기</button>
        </div>
        <div class="modal-content meta-grid">
          <div><span>차종</span><strong>{escape(card['vehicle_type'])}</strong></div>
          <div><span>차량 번호</span><strong>{escape(card['vehicle_number'])}</strong></div>
          <div><span>보험사</span><strong>{escape(card['insurance_company'])}</strong></div>
          <div><span>보험사 전화번호</span><strong>{escape(card['insurance_phone'])}</strong></div>
        </div>
      </div>
    </dialog>
    """


def render_vehicle_card(card: dict, section_label: str) -> tuple[str, str]:
    info_name = dialog_id(f"info-{section_label}", card["vehicle_name"])
    schedule_name = dialog_id(f"schedule-{section_label}", card["vehicle_name"])
    first_line = " - ".join(
        part for part in [format_clock(card["first_time"]), card["first_name"], card["first_address_short"]] if part
    )

    card_html = f"""
    <article class="vehicle-card">
      <span class="vehicle-mark">{escape(card['vehicle_name'].replace('호차', ''))}</span>
      <div class="vehicle-card-top">
        <h3>{escape(card['vehicle_name'])}</h3>
        <button type="button" class="ghost-button" data-open-dialog="{escape(info_name)}">차량 정보</button>
      </div>
      <div class="vehicle-body">
        {render_person_row("운", card["driver"], "driver")}
        {render_person_row("동", card["companion"], "companion")}
        <div class="departure-line">
          <span class="departure-label">출발 시간</span>
          <strong>{escape(first_line or '-')}</strong>
        </div>
        <div class="count-line">
          <span>{escape(section_label)}</span>
          <strong>{card['count'] if card['count'] is not None else '-'}</strong>
        </div>
      </div>
      <button type="button" class="schedule-link" data-open-dialog="{escape(schedule_name)}">스케줄 보기</button>
    </article>
    """

    dialogs_html = render_info_dialog(card, info_name) + render_schedule_dialog(
        f"{card['display_name']} {section_label}", card["schedule_rounds"], schedule_name
    )
    return card_html, dialogs_html


def render_self_dialog(title: str, entries: list[dict], dialog_name: str) -> str:
    items = []
    for entry in entries:
        summary = " - ".join(
            part for part in [entry["name"], entry["time"], entry["address"]] if part
        )
        items.append(
            f"<li class=\"self-entry {'is-absent' if entry['absent'] else ''}\">{escape(summary)}</li>"
        )

    return f"""
    <dialog id="{escape(dialog_name)}" class="modal-dialog">
      <div class="modal-shell">
        <div class="modal-header">
          <div>
            <p class="eyebrow">자가 명단</p>
            <h3>{escape(title)}</h3>
          </div>
          <button type="button" class="modal-close" data-close-dialog>닫기</button>
        </div>
        <div class="modal-content">
          <ul class="self-list">
            {''.join(items) if items else '<li class="self-entry empty-copy">명단이 없습니다.</li>'}
          </ul>
        </div>
      </div>
    </dialog>
    """


def render_self_card(title: str, count: int, dialog_name: str) -> str:
    return f"""
    <button type="button" class="self-card" data-open-dialog="{escape(dialog_name)}">
      <span>{escape(title)}</span>
      <strong>명단 보기</strong>
      <small>{count}명</small>
    </button>
    """


def render_order_strip(order_cards: list[dict]) -> str:
    items = []
    for item in order_cards:
        vehicle_number = item["vehicle_name"].replace("호차", "")
        items.append(
            f"""
            <div class="order-item">
              <strong>{escape(vehicle_number)}</strong>
              <span>{item['minute']}분</span>
            </div>
            """
        )
    return f"""
    <section class="order-strip-card">
      <div class="order-strip-copy">
        <p class="eyebrow">송영 운행 순서</p>
        <h3>차량 출발 순서</h3>
      </div>
      <div class="order-strip-items">
        {''.join(items)}
      </div>
    </section>
    """


def render_html(
    data: dict,
    schedule_bundle: dict[str, dict] | None = None,
    admin_pin: str = DEFAULT_ADMIN_PIN,
    admin_label: str = DEFAULT_ADMIN_LABEL,
) -> str:
    base_date = derive_base_date(data)
    admin_pin_hash = hashlib.sha256(admin_pin.encode("utf-8")).hexdigest()
    staff_roster = load_staff_roster()
    driver_candidates = [
        item["name"] for item in staff_roster if item["position"] in DRIVER_POSITIONS and item["name"] not in EXCLUDED_DRIVERS
    ]
    companion_candidates = [item["name"] for item in staff_roster if item["position"] in COMPANION_POSITIONS]
    if schedule_bundle is None:
        schedule_bundle = {base_date.isoformat(): data}
    base_date = latest_schedule_date(schedule_bundle, base_date)
    resident_names = sorted(
        {
            name
            for parsed in schedule_bundle.values()
            for name in collect_resident_names(parsed)
        }
    )
    schedule_json = (
        json.dumps(schedule_bundle, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("</script", "<\\/script")
    )
    source_name = Path(data["source_file"]).name

    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>반디셔틀 홈</title>
  <style>
    :root {{
      --ink: #31353c;
      --ink-soft: #72767d;
      --paper-base: rgba(250, 245, 236, 0.9);
      --paper-strong: rgba(252, 248, 241, 0.98);
      --paper-card: #f7f2ea;
      --paper-card-warm: #f3ede4;
      --paper-card-cool: #eef0ea;
      --accent: #8f735c;
      --accent-soft: rgba(143, 115, 92, 0.12);
      --danger-soft: rgba(173, 108, 95, 0.18);
      --line: rgba(49, 53, 60, 0.12);
      --shadow-lg: 0 28px 48px rgba(95, 79, 58, 0.18);
      --shadow-md: 0 16px 28px rgba(95, 79, 58, 0.15);
      --paper-lift: 0 2px 0 rgba(255, 255, 255, 0.5) inset, 0 -10px 18px rgba(95, 79, 58, 0.035) inset;
      --radius-xl: 34px;
      --radius-lg: 26px;
      --radius-md: 20px;
      --font-display: "Avenir Next", "Apple SD Gothic Neo", "SUIT", sans-serif;
      --font-body: "Pretendard", "Apple SD Gothic Neo", "SUIT", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: var(--font-body);
      color: var(--ink);
      background: linear-gradient(180deg, #efe6d8 0%, #f5eee4 40%, #ede3d4 100%);
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background:
        repeating-linear-gradient(0deg, rgba(255,255,255,0.02) 0, rgba(255,255,255,0.02) 1px, transparent 1px, transparent 7px),
        repeating-linear-gradient(90deg, rgba(83,68,49,0.012) 0, rgba(83,68,49,0.012) 1px, transparent 1px, transparent 11px);
      opacity: 0.72;
    }}
    .page-shell {{ max-width: 520px; margin: 0 auto; padding: 18px 12px 32px; }}
    .site-header,
    .hero-top,
    .toolbar,
    .transport-section,
    .order-strip-card,
    .vehicle-card,
    .self-card,
    .modal-shell {{
      position: relative;
      overflow: hidden;
      border: 1px solid rgba(49, 53, 60, 0.08);
      background: var(--paper-base);
      background-clip: padding-box;
      box-shadow: var(--shadow-lg), var(--paper-lift);
      backdrop-filter: blur(14px);
    }}
    .site-header::before,
    .hero-top::before,
    .toolbar::before,
    .transport-section::before,
    .vehicle-card::before,
    .self-card::before,
    .order-strip-card::before,
    .modal-shell::before {{
      content: "";
      position: absolute;
      inset: 1px;
      border-radius: inherit;
      background:
        linear-gradient(180deg, rgba(255, 255, 255, 0.34), transparent 32%),
        repeating-linear-gradient(0deg, transparent 0, transparent 10px, rgba(120, 103, 79, 0.012) 10px, rgba(120, 103, 79, 0.012) 11px);
      pointer-events: none;
    }}
    .top-shell {{ display: grid; gap: 18px; }}
    .site-header {{
      display: flex; align-items: center; justify-content: space-between; gap: 18px;
      margin-bottom: 22px; padding: 16px 20px; border-radius: 999px; background: rgba(250,246,240,0.76);
      overflow: visible;
      isolation: isolate;
      z-index: 40;
    }}
    .brand, .header-actions, .hero-top > *, .toolbar > *, .transport-section > *, .order-strip-card > *, .vehicle-card > *, .self-card > *, .modal-shell > * {{ position: relative; z-index: 1; }}
    .brand {{ display: inline-flex; align-items: center; gap: 14px; text-decoration: none; color: inherit; }}
    .brand-logo {{ width: 48px; height: 48px; object-fit: contain; }}
    .brand-text, .hero-title, .section-heading h2, .order-strip-copy h3, .modal-header h3, .hero-date-display {{
      font-family: var(--font-display); letter-spacing: -0.04em;
    }}
    .brand-text {{ font-size: 1.24rem; font-weight: 760; }}
    .header-actions {{ display: flex; flex-direction: column; align-items: flex-end; gap: 8px; position: relative; z-index: 90; }}
    .header-menu {{ position: relative; display: flex; justify-content: flex-end; }}
    .menu-button {{
      width: 42px; height: 42px; display: grid; place-items: center; padding: 0;
      border: 1px solid var(--line); background: rgba(255,255,255,0.72); color: inherit; border-radius: 999px; cursor: pointer;
      font-size: 1.2rem; font-weight: 800;
    }}
    .menu-panel {{
      position: fixed; top: 18px; right: 18px; min-width: 220px; max-width: min(260px, calc(100vw - 88px)); padding: 8px;
      border-radius: 18px; border: 1px solid var(--line); background: rgba(252,248,241,1);
      box-shadow: var(--shadow-md); display: none; z-index: 140;
    }}
    .menu-panel.is-open {{ display: grid; gap: 6px; }}
    .menu-item {{
      min-height: 40px; padding: 0 12px; border: 1px solid transparent; border-radius: 12px;
      background: transparent; color: inherit; text-decoration: none; font-weight: 700; font-size: 1rem; line-height: 1.35; display: inline-flex; align-items: center; cursor: pointer;
    }}
    .menu-item[hidden] {{ display: none !important; }}
    .menu-item:hover {{ background: rgba(143,115,92,0.08); border-color: var(--line); }}
    .chip-button, .nav-link, .ghost-button, .schedule-link, .self-card, .modal-close, .primary-button, .danger-button, .inline-button {{
      border: 1px solid var(--line); background: rgba(255, 255, 255, 0.72); color: inherit; border-radius: 999px; cursor: pointer;
    }}
    .nav-link, .chip-button, .ghost-button, .primary-button, .danger-button {{ min-height: 40px; padding: 0 14px; text-decoration: none; font-weight: 700; display: inline-flex; align-items: center; }}
    .chip-button.active {{ background: var(--accent-soft); }}
    .hero, .app-body {{ display: grid; gap: 22px; }}
    .hero {{ position: relative; z-index: 1; }}
    .hero-top {{ padding: 30px 32px; border-radius: var(--radius-xl); text-align: center; background: rgba(247, 242, 234, 0.84); }}
    .eyebrow {{ margin: 0 0 10px; color: var(--ink-soft); font-size: 0.82rem; font-weight: 740; letter-spacing: 0.18em; text-transform: uppercase; }}
    .hero-title {{ margin: 0; font-size: clamp(1.4rem, 2.2vw, 2rem); line-height: 1.1; }}
    .hero-date-row {{ display: grid; grid-template-columns: 58px auto 58px; align-items: center; justify-content: center; gap: 18px; margin-top: 18px; }}
    .date-arrow {{ width: 58px; height: 58px; flex: 0 0 58px; display: grid; place-items: center; border-radius: 999px; background: var(--paper-strong); border: 1px solid rgba(49,53,60,0.08); box-shadow: var(--shadow-md); color: inherit; cursor: pointer; font-size: 1.5rem; font-weight: 700; }}
    .hero-date-display {{ margin: 0; text-align: center; font-size: clamp(2.5rem, 6vw, 5.4rem); line-height: 0.95; }}
    .hero-actions {{ margin-top: 14px; display: flex; justify-content: center; }}
    .toolbar {{ display: flex; align-items: center; justify-content: center; gap: 16px; padding: 12px 14px; border-radius: var(--radius-lg); }}
    .toolbar-group {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
    .toolbar-group.search-group {{ flex: 1 1 920px; min-width: 0; justify-content: center; }}
    .search-shell {{ display: flex; gap: 10px; align-items: center; width: min(100%, 1080px); min-width: 0; margin: 0 auto; }}
    .search-field {{ flex: 1 1 auto; min-width: 0; min-height: 42px; padding: 0 16px; border-radius: 999px; border: 1px solid var(--line); background: rgba(255,255,255,0.82); font: inherit; }}
    .search-field::placeholder {{ color: #96897b; }}
    .search-helper {{ color: var(--ink-soft); font-size: 0.84rem; }}
    .primary-button.search-button {{ min-width: 92px; justify-content: center; white-space: nowrap; }}
    .mobile-side-tabs {{ display: none; gap: 10px; }}
    .mobile-side-tab {{
      min-height: 42px; padding: 0 16px; border-radius: 999px; border: 1px solid var(--line);
      background: rgba(255,255,255,0.74); color: inherit; font: inherit; font-weight: 740; cursor: pointer;
    }}
    .mobile-side-tab.is-active {{ background: var(--accent-soft); border-color: rgba(143,115,92,0.32); }}
    .transport-section {{ display: grid; gap: 22px; padding: 24px; border-radius: var(--radius-xl); }}
    .mobile-section-head {{ display: none; }}
    .section-heading {{ display: flex; align-items: end; justify-content: space-between; gap: 16px; }}
    .section-total {{ display: inline-flex; align-items: center; min-height: 38px; padding: 0 16px; border-radius: 999px; background: rgba(255,255,255,0.62); border: 1px solid var(--line); font-weight: 700; }}
    .section-total.mobile-total {{ display: none; }}
    .order-strip-card {{ display: flex; align-items: center; justify-content: space-between; gap: 18px; padding: 22px 24px; border-radius: var(--radius-lg); background: var(--paper-card-cool); }}
    .order-strip-items {{ display: flex; flex-wrap: wrap; gap: 12px; justify-content: flex-end; }}
    .order-item {{ display: inline-flex; align-items: baseline; gap: 8px; padding: 12px 14px; border-radius: 999px; background: rgba(255,255,255,0.74); border: 1px solid var(--line); }}
    .order-item strong {{ font-family: var(--font-display); font-size: 1.45rem; line-height: 1; }}
    .vehicle-grid {{ display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 18px; }}
    .vehicle-card {{ min-height: 300px; padding: 18px; border-radius: var(--radius-lg); background: var(--paper-card); display: flex; flex-direction: column; gap: 16px; }}
    .vehicle-card-top {{ display: flex; align-items: start; justify-content: space-between; gap: 12px; }}
    .vehicle-card h3 {{ margin: 0; font-size: 1.18rem; position: relative; z-index: 2; }}
    .vehicle-mark {{ position: absolute; top: 4px; right: 10px; font-family: var(--font-display); font-size: 4.6rem; font-weight: 760; line-height: 0.9; color: rgba(143,115,92,0.16); pointer-events: none; }}
    .ghost-button {{ min-height: 34px; padding: 0 12px; font-size: 0.82rem; font-weight: 700; white-space: nowrap; }}
    .vehicle-meta {{ display: grid; gap: 10px; }}
    .info-badges {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .info-badge {{
      display: inline-flex; align-items: center; min-height: 28px; padding: 0 10px;
      border-radius: 999px; background: rgba(255,255,255,0.72); border: 1px solid var(--line);
      color: var(--ink-soft); font-size: 0.78rem; font-weight: 760;
    }}
    .info-badge.is-alert {{ background: rgba(173,108,95,0.12); color: #9b655a; }}
    .vehicle-body {{ display: grid; gap: 12px; margin-top: 8px; }}
    .person-row {{ display: flex; align-items: center; gap: 10px; min-height: 32px; }}
    .person-row strong {{ font-size: 1.14rem; }}
    .person-row.companion strong {{ font-size: 0.96rem; font-weight: 650; color: var(--ink-soft); }}
    .assignment-editor {{ display: grid; gap: 8px; padding: 10px 12px; border-radius: 16px; background: rgba(255,255,255,0.58); border: 1px solid var(--line); }}
    .assignment-editor .form-grid {{ grid-template-columns: 1fr; }}
    .assignment-editor .inline-button {{ justify-content: center; }}
    .role-badge {{ width: 28px; height: 28px; flex: 0 0 28px; display: grid; place-items: center; border: 1.5px solid rgba(49,53,60,0.24); border-radius: 999px; background: var(--paper-strong); font-size: 0.82rem; font-weight: 800; }}
    .departure-line, .count-line {{ display: grid; gap: 6px; }}
    .departure-label, .count-line span, .meta-grid span, .form-grid label, .form-inline label {{ color: var(--ink-soft); font-size: 0.82rem; font-weight: 650; }}
    .departure-line strong {{ font-size: 1rem; line-height: 1.4; }}
    .count-line strong {{ font-family: var(--font-display); font-size: 2rem; line-height: 1; }}
    .schedule-link {{ margin-top: auto; min-height: 42px; padding: 0 14px; font-weight: 700; text-align: left; display: inline-flex; align-items: center; }}
    .self-row {{ display: flex; justify-content: flex-start; }}
    .self-card {{ min-width: 220px; min-height: 100px; padding: 16px 18px; display: grid; gap: 4px; text-align: left; border-radius: var(--radius-md); background: var(--paper-card-warm); }}
    .self-card span {{ color: var(--ink-soft); font-weight: 700; }}
    .self-card strong {{ font-size: 1.14rem; }}
    .self-card small {{ color: var(--ink-soft); }}
    .modal-dialog {{ width: min(980px, calc(100vw - 30px)); border: 0; padding: 0; background: transparent; }}
    .modal-dialog::backdrop {{ background: rgba(49,53,60,0.34); backdrop-filter: blur(5px); }}
    .modal-shell {{ border-radius: 28px; padding: 24px; background: var(--paper-strong); }}
    .modal-shell.narrow {{ width: min(560px, 100%); }}
    .modal-header {{ display: flex; align-items: start; justify-content: space-between; gap: 18px; margin-bottom: 20px; }}
    .modal-content {{ display: grid; gap: 14px; }}
    .meta-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
    .meta-grid div, .schedule-round, .self-list-shell {{ border: 1px solid var(--line); border-radius: 18px; background: rgba(255,255,255,0.74); }}
    .meta-grid div {{ padding: 14px 16px; display: grid; gap: 6px; }}
    .schedule-round {{ padding: 16px; }}
    .schedule-round h4 {{ margin: 0 0 12px; }}
    .schedule-list, .self-list {{ margin: 0; padding: 0; list-style: none; }}
    .entry-card {{ padding: 12px 0; border-top: 1px solid rgba(49,53,60,0.08); }}
    .entry-card:first-child {{ border-top: 0; padding-top: 0; }}
    .entry-card.is-absent .entry-main strong, .entry-card.is-absent .entry-main span, .entry-card.is-absent .entry-sub {{ color: #9f8f84; text-decoration: line-through; }}
    .entry-card.is-absent {{ opacity: 0.78; }}
    .entry-card.is-search-hit {{ border-color: rgba(201,123,55,0.32); background: rgba(255,247,233,0.94); box-shadow: inset 0 0 0 1px rgba(201,123,55,0.18); }}
    .entry-card.is-search-hit .entry-main strong {{ color: #8c4e17; }}
    .entry-main {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: baseline; margin-bottom: 4px; }}
    .entry-main strong {{ font-size: 1rem; }}
    .entry-sub {{ color: var(--ink-soft); font-size: 0.92rem; }}
    .entry-note {{ margin-top: 4px; color: var(--ink-soft); font-size: 0.86rem; }}
    .search-match-button {{ width: 100%; justify-content: space-between; text-align: left; display: flex; gap: 12px; align-items: center; }}
    .search-match-meta {{ color: var(--ink-soft); font-size: 0.88rem; white-space: nowrap; }}
    .entry-editor {{ margin-top: 12px; display: grid; gap: 10px; }}
    .form-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }}
    .form-grid input, .form-grid select {{ width: 100%; min-height: 40px; padding: 0 10px; border-radius: 12px; border: 1px solid var(--line); background: rgba(255,255,255,0.82); }}
    .form-inline {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
    .inline-button {{ min-height: 34px; padding: 0 12px; font-size: 0.82rem; font-weight: 700; }}
    .danger-button {{ background: var(--danger-soft); }}
    .empty-copy {{ color: var(--ink-soft); }}
    .desktop-only {{ display: none; }}
    .mobile-only {{ display: block; }}
    @media (max-width: 1280px) {{ .vehicle-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }} .form-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
    @media (max-width: 9999px) {{
      .page-shell {{ padding: 18px 12px 32px; }}
      .top-shell {{
        position: sticky;
        top: 12px;
        z-index: 34;
        gap: 12px;
        padding: 14px 16px 16px;
        border-radius: 28px;
        border: 1px solid rgba(49, 53, 60, 0.08);
        background: var(--paper-base);
        overflow: hidden;
        box-shadow: var(--shadow-lg), var(--paper-lift);
        backdrop-filter: blur(14px);
      }}
      .top-shell::before {{
        display: none;
      }}
      .top-shell > * {{ position: relative; z-index: 1; }}
      .site-header {{ margin-bottom: 0; padding: 0; border-radius: 0; flex-direction: row; align-items: center; background: transparent; box-shadow: none; border: 0; backdrop-filter: none; z-index: 120; }}
      .hero-top {{ padding: 0; border-radius: 0; background: transparent; box-shadow: none; border: 0; backdrop-filter: none; z-index: 1; }}
      .site-header::before,
      .hero-top::before,
      .toolbar::before,
      .transport-section::before,
      .vehicle-card::before,
      .self-card::before,
      .order-strip-card::before {{
        display: none;
      }}
      .toolbar {{ border-radius: 18px; padding: 8px 10px; align-items: center; margin-top: 4px; }}
      .header-actions {{ width: auto; align-items: flex-end; }}
      .toolbar-group {{ width: 100%; align-items: stretch; }}
      .toolbar-group:last-child {{ gap: 8px; }}
      .header-menu {{ align-self: auto; }}
      .menu-panel {{
        top: 16px;
        right: 16px;
        left: auto;
        min-width: 196px;
        max-width: min(248px, calc(100vw - 32px));
      }}
      .menu-item {{ font-size: 1.14rem; }}
      .mobile-side-tabs {{ display: flex; width: 100%; }}
      .transport-section, .vehicle-card, .order-strip-card, .modal-shell {{ border-radius: 26px; }}
      .hero-date-row {{ grid-template-columns: 44px minmax(0, 1fr) 44px; gap: 8px; }}
      .date-arrow {{ width: 46px; height: 46px; flex-basis: 46px; font-size: 1.2rem; }}
      .hero-date-display {{ min-width: 0; font-size: clamp(2rem, 7vw, 3.2rem); }}
      .transport-section.mobile-transport {{
        position: relative;
        gap: 14px;
        padding-top: 0;
        overflow: visible;
      }}
      .mobile-section-head {{
        display: grid;
        gap: 10px;
        position: sticky;
        top: var(--mobile-sticky-offset, 164px);
        z-index: 14;
        padding: 16px 0 18px;
        margin: 0;
        background: linear-gradient(180deg, rgba(250,245,236,0.98) 0%, rgba(250,245,236,0.98) 72%, rgba(250,245,236,0) 100%);
      }}
      .mobile-section-head .eyebrow {{ margin-bottom: 0; }}
      .section-heading {{ display: none; }}
      .section-total.mobile-total {{ display: inline-flex; }}
      .order-strip-card {{ align-items: flex-start; flex-direction: column; }}
      .vehicle-grid {{ grid-template-columns: 1fr; gap: 12px; }}
      .vehicle-card {{ min-height: 0; padding: 16px; }}
      .vehicle-mark {{ font-size: 3.3rem; }}
      .vehicle-card h3 {{ font-size: 1.08rem; }}
      .ghost-button, .schedule-link, .chip-button, .nav-link, .primary-button, .danger-button {{ font-size: 0.76rem; }}
      .person-row strong {{ font-size: 1rem; }}
      .person-row.companion strong, .departure-line strong {{ font-size: 0.9rem; }}
      .count-line strong {{ font-size: 1.6rem; }}
      .desktop-only {{ display: none; }}
      .mobile-only {{ display: block; }}
      .self-card {{ width: 100%; }}
      .meta-grid, .form-grid {{ grid-template-columns: 1fr; }}
      .search-shell {{ width: min(100%, 94vw); }}
      .modal-dialog[data-sheet="true"] {{
        width: 100vw;
        max-width: none;
        margin: auto 0 0 0;
        padding: 0;
        inset: auto 0 0 0;
      }}
      .modal-dialog[data-sheet="true"] .modal-shell {{
        width: 100%;
        max-height: 76vh;
        overflow: auto;
        border-radius: 28px 28px 0 0;
        padding-bottom: calc(24px + env(safe-area-inset-bottom));
      }}
    }}
  </style>
</head>
<body>
  <main class="page-shell">
    <section class="hero">
      <div class="top-shell" id="top-shell">
        <header class="site-header">
          <a class="brand" href="./index.html">
            <img class="brand-logo" src="./logo.png" alt="반디 로고" />
            <span class="brand-text">반디</span>
          </a>
          <div class="header-actions">
            <div class="header-menu">
              <button type="button" class="menu-button" id="menu-toggle" aria-label="메뉴">☰</button>
            </div>
          </div>
        </header>
        <section class="hero-top">
          <p class="eyebrow">Shuttle Dashboard</p>
          <h1 class="hero-title">오늘의 셔틀 운행표</h1>
          <div class="hero-date-row" data-base-date="{base_date.isoformat()}">
            <button type="button" class="date-arrow" data-shift-date="-1" aria-label="이전 날짜">←</button>
            <div class="hero-date-display" id="hero-date-display"></div>
            <button type="button" class="date-arrow" data-shift-date="1" aria-label="다음 날짜">→</button>
          </div>
          <div class="hero-actions">
            <a class="nav-link" href="./calendar.html">월별 캘린더</a>
          </div>
        </section>
      </div>
      <div class="app-body" id="app-root"></div>
      <section class="toolbar" id="admin-toolbar">
        <div class="toolbar-group search-group">
          <div class="search-shell">
            <input id="resident-search" class="search-field" type="search" list="resident-suggestions" placeholder="어르신 찾기" autocomplete="off" />
            <datalist id="resident-suggestions"></datalist>
            <button type="button" class="primary-button search-button" id="resident-search-button">검색</button>
          </div>
        </div>
      </section>
    </section>
  </main>
  <div class="menu-panel" id="menu-panel">
    <button type="button" class="menu-item" id="upload-menu-item" data-action="open-upload" hidden>엑셀 업로드</button>
    <button type="button" class="menu-item" data-action="open-export" data-kind="original">원본 내보내기</button>
    <button type="button" class="menu-item" data-action="open-export" data-kind="edited">수정본 내보내기</button>
    <button type="button" class="menu-item" id="reset-menu-item" data-action="reset-schedule" hidden>수정 초기화</button>
    <button type="button" class="menu-item" id="admin-toggle">관리자 로그인</button>
  </div>
  <dialog id="app-dialog" class="modal-dialog"></dialog>
  <script id="schedule-data" type="application/json">{schedule_json}</script>
  <script>
    const ADMIN_CONFIG = {{ label: {json.dumps(admin_label, ensure_ascii=False)}, pinHash: "{admin_pin_hash}" }};
    const STAFF_OPTIONS = {{
      driver: {json.dumps(driver_candidates, ensure_ascii=False)},
      companion: {json.dumps(companion_candidates, ensure_ascii=False)},
    }};
    const initialSchedules = JSON.parse(document.getElementById("schedule-data").textContent);
    const SCHEDULE_BUNDLE_CACHE_KEY = "bandi_shuttle_schedule_bundle_cache_v1";
    const API_ENDPOINTS = {{
      config: "./api/config",
      schedules: "./api/schedules",
      overrides: "./api/overrides",
      upload: "./api/upload",
    }};
    function isValidScheduleBundle(candidate) {{
      return Boolean(
        candidate &&
        typeof candidate === "object" &&
        !Array.isArray(candidate) &&
        Object.values(candidate).every((value) => isValidScheduleData(value))
      );
    }}

    function loadCachedScheduleBundle() {{
      try {{
        const raw = window.localStorage.getItem(SCHEDULE_BUNDLE_CACHE_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        return isValidScheduleBundle(parsed) ? parsed : null;
      }} catch (_error) {{
        return null;
      }}
    }}

    function persistScheduleBundleCache(bundle) {{
      try {{
        if (!isValidScheduleBundle(bundle)) return;
        window.localStorage.setItem(SCHEDULE_BUNDLE_CACHE_KEY, JSON.stringify(bundle));
      }} catch (_error) {{
      }}
    }}

    let scheduleStore = loadCachedScheduleBundle() || initialSchedules;
    let RESIDENT_NAMES = collectResidentNamesFromSchedules(scheduleStore);
    const weekdayNames = ["일요일", "월요일", "화요일", "수요일", "목요일", "금요일", "토요일"];
    const topShell = document.getElementById("top-shell");
    const heroDateRow = document.querySelector(".hero-date-row");
    const heroDateDisplay = document.getElementById("hero-date-display");
    const appRoot = document.getElementById("app-root");
    const appDialog = document.getElementById("app-dialog");
    const menuToggle = document.getElementById("menu-toggle");
    const menuPanel = document.getElementById("menu-panel");
    const adminToolbar = document.getElementById("admin-toolbar");
    const adminToggle = document.getElementById("admin-toggle");
    const resetMenuItem = document.getElementById("reset-menu-item");
    const uploadMenuItem = document.getElementById("upload-menu-item");
    const residentSearchInput = document.getElementById("resident-search");
    const residentSearchButton = document.getElementById("resident-search-button");
    const residentSuggestions = document.getElementById("resident-suggestions");
    function todayDateKey() {{
      return new Intl.DateTimeFormat("en-CA", {{
        timeZone: "Asia/Seoul",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      }}).format(new Date());
    }}
    function parseActiveDate() {{
      const historyDate = window.history.state?.date;
      if (historyDate && /^\\d{{4}}-\\d{{2}}-\\d{{2}}$/.test(historyDate)) {{
        return new Date(historyDate + "T12:00:00");
      }}
      const search = new URLSearchParams(window.location.search);
      const raw = search.get("date");
      const isInternalNavigation = search.get("nav") === "1";
      if (isInternalNavigation && raw && /^\\d{{4}}-\\d{{2}}-\\d{{2}}$/.test(raw)) {{
        return new Date(raw + "T12:00:00");
      }}
      return new Date(todayDateKey() + "T12:00:00");
    }}

    function currentSeoulHour() {{
      const parts = new Intl.DateTimeFormat("en-CA", {{
        timeZone: "Asia/Seoul",
        hour: "2-digit",
        hour12: false,
      }}).formatToParts(new Date());
      return Number(parts.find((part) => part.type === "hour")?.value || "0");
    }}

    function defaultMobileSide() {{
      return currentSeoulHour() >= 12 ? "dropoff" : "pickup";
    }}

    function getAdminSessionToken() {{
      try {{
        return window.sessionStorage.getItem("bandi_shuttle_admin_hash") || "";
      }} catch (error) {{
        return "";
      }}
    }}

    function setAdminSessionToken(value) {{
      try {{
        if (value) {{
          window.sessionStorage.setItem("bandi_shuttle_admin_hash", value);
        }} else {{
          window.sessionStorage.removeItem("bandi_shuttle_admin_hash");
        }}
      }} catch (error) {{
        // Ignore storage access failures and keep admin mode in-memory only.
      }}
    }}

    let activeDate = parseActiveDate();
    let state = {{
      data: null,
      isAdmin: getAdminSessionToken() === ADMIN_CONFIG.pinHash,
      activeModal: null,
      mobileSide: defaultMobileSide(),
      backendConfigured: false,
      remoteBootstrapping: true,
    }};
    let sharedRefreshHandle = null;

    function clone(value) {{
      return JSON.parse(JSON.stringify(value));
    }}

    function scheduleFingerprint(value) {{
      return value ? JSON.stringify(value) : "";
    }}

    function collectResidentNamesFromSchedules(bundle) {{
      const names = new Set();
      Object.values(bundle || {{}}).forEach((schedule) => {{
        (schedule.vehicles || []).forEach((vehicle) => {{
          ["pickup_rounds", "dropoff_rounds"].forEach((key) => {{
            (vehicle[key] || []).forEach((roundData) => {{
              (roundData.entries || []).forEach((entry) => {{
                if (entry.name) names.add(entry.name);
              }});
            }});
          }});
        }});
        ["self_pickup", "self_dropoff"].forEach((key) => {{
          ((schedule[key] || {{}}).entries || []).forEach((entry) => {{
            if (entry.name) names.add(entry.name);
          }});
        }});
      }});
      return Array.from(names).sort();
    }}

    function isValidScheduleData(candidate) {{
      return Boolean(
        candidate &&
        Array.isArray(candidate.vehicles) &&
        candidate.self_pickup &&
        Array.isArray(candidate.self_pickup.entries) &&
        candidate.self_dropoff &&
        Array.isArray(candidate.self_dropoff.entries) &&
        candidate.home &&
        Array.isArray(candidate.home.dropoff_order_cards)
      );
    }}

    function activeDateKey() {{
      return activeDate ? activeDate.toISOString().slice(0, 10) : null;
    }}

    function storageKey(schedule) {{
      return "bandi-shuttle-state:" + (schedule?.sheet_name || "schedule") + ":" + ((schedule?.source_file || "").split("/").pop() || "source");
    }}

    function baseScheduleForDate(dateKey = activeDateKey()) {{
      return dateKey ? scheduleStore[dateKey] || null : null;
    }}

    async function fetchRemoteSchedules() {{
      if (!state.backendConfigured) return null;
      try {{
        const response = await window.fetch(API_ENDPOINTS.schedules, {{ cache: "no-store" }});
        if (!response.ok) return null;
        const payload = await response.json();
        return payload && payload.schedules && typeof payload.schedules === "object" ? payload.schedules : null;
      }} catch (_error) {{
        return null;
      }}
    }}

    function loadPersistedData(schedule) {{
      if (!schedule) return null;
      const saved = localStorage.getItem(storageKey(schedule));
      if (!saved) return clone(schedule);
      try {{
        const parsed = JSON.parse(saved);
        return isValidScheduleData(parsed) ? parsed : clone(schedule);
      }} catch (_error) {{
        return clone(schedule);
      }}
    }}

    function persistLocalData() {{
      if (!state.data) return;
      localStorage.setItem(storageKey(baseScheduleForDate()), JSON.stringify(state.data));
    }}

    async function fetchBackendConfig() {{
      try {{
        const response = await window.fetch(API_ENDPOINTS.config, {{ cache: "no-store" }});
        if (!response.ok) return false;
        const payload = await response.json();
        return Boolean(payload && payload.configured);
      }} catch (_error) {{
        return false;
      }}
    }}

    async function loadRemoteOverride(dateKey) {{
      if (!state.backendConfigured || !dateKey) return null;
      try {{
        const response = await window.fetch(`${{API_ENDPOINTS.overrides}}?date=${{encodeURIComponent(dateKey)}}`, {{
          cache: "no-store",
        }});
        if (response.status === 404) return null;
        if (!response.ok) throw new Error("override fetch failed");
        const payload = await response.json();
        return isValidScheduleData(payload?.data) ? payload.data : null;
      }} catch (_error) {{
        return null;
      }}
    }}

    async function saveRemoteOverride(dateKey, data) {{
      if (!state.backendConfigured || !dateKey || !isValidScheduleData(data)) return true;
      try {{
        const response = await window.fetch(API_ENDPOINTS.overrides, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{
            date_key: dateKey,
            data,
            updated_by: state.isAdmin ? ADMIN_CONFIG.label : "viewer",
          }}),
        }});
        return response.ok;
      }} catch (_error) {{
        return false;
      }}
    }}

    async function clearRemoteOverride(dateKey) {{
      if (!state.backendConfigured || !dateKey) return true;
      try {{
        const response = await window.fetch(`${{API_ENDPOINTS.overrides}}?date=${{encodeURIComponent(dateKey)}}`, {{
          method: "DELETE",
        }});
        return response.ok;
      }} catch (_error) {{
        return false;
      }}
    }}

    async function persistData() {{
      if (!state.data) return;
      persistLocalData();
      const ok = await saveRemoteOverride(activeDateKey(), state.data);
      if (!ok) {{
        window.console.warn("Shared override save failed; kept local copy only.");
      }}
    }}

    async function syncScheduleForActiveDate() {{
      const baseSchedule = baseScheduleForDate();
      if (!baseSchedule) {{
        state.data = null;
        return;
      }}
      if (state.backendConfigured) {{
        const remoteState = await loadRemoteOverride(activeDateKey());
        state.data = remoteState || clone(baseSchedule);
        return;
      }}
      state.data = loadPersistedData(baseSchedule);
    }}

    async function refreshSharedState() {{
      if (!state.backendConfigured) return;
      const baseSchedule = baseScheduleForDate();
      if (!baseSchedule) return;
      const remoteState = await loadRemoteOverride(activeDateKey());
      const nextState = remoteState || clone(baseSchedule);
      if (scheduleFingerprint(nextState) !== scheduleFingerprint(state.data)) {{
        state.data = nextState;
        renderApp();
      }}
    }}

    async function refreshScheduleBundle() {{
      if (!state.backendConfigured) return;
      const remoteSchedules = await fetchRemoteSchedules();
      if (remoteSchedules && Object.keys(remoteSchedules).length) {{
        scheduleStore = remoteSchedules;
        persistScheduleBundleCache(scheduleStore);
        RESIDENT_NAMES = collectResidentNamesFromSchedules(scheduleStore);
      }}
    }}

    function ensureSharedRefreshLoop() {{
      if (sharedRefreshHandle) {{
        window.clearInterval(sharedRefreshHandle);
        sharedRefreshHandle = null;
      }}
      if (!state.backendConfigured) return;
      sharedRefreshHandle = window.setInterval(() => {{
        refreshSharedState();
      }}, 15000);
    }}

    function updateResidentSuggestions() {{
      if (!residentSuggestions || !residentSearchInput) return;
      const query = residentSearchInput.value.trim();
      residentSuggestions.innerHTML = "";
      if (query.length < 1) return;
      RESIDENT_NAMES
        .filter((name) => name.includes(query))
        .slice(0, 12)
        .forEach((name) => {{
          const option = document.createElement("option");
          option.value = name;
          residentSuggestions.appendChild(option);
        }});
    }}

    function residentMatchesForName(name) {{
      if (!state.data || !name) return [];
      const matches = [];
      state.data.vehicles.forEach((vehicle) => {{
        ["pickup", "dropoff"].forEach((side) => {{
          vehicle[`${{side}}_rounds`].forEach((roundData) => {{
            roundData.entries.forEach((entry) => {{
              if (entry.name === name) {{
                matches.push({{
                  side,
                  sourceKind: "vehicle",
                  vehicleName: vehicle.vehicle_name,
                  displayName: vehicle.display_name,
                  row: entry.row,
                  round: roundData.round,
                  time: entry.time,
                  absent: entry.absent,
                }});
              }}
            }});
          }});
        }});
      }});
      ["self_pickup", "self_dropoff"].forEach((selfKey) => {{
        state.data[selfKey].entries.forEach((entry) => {{
          if (entry.name === name) {{
            matches.push({{
              side: selfKey === "self_pickup" ? "pickup" : "dropoff",
              sourceKind: "self",
              vehicleName: selfKey,
              displayName: selfKey === "self_pickup" ? "자가 등영" : "자가 송영",
              row: entry.row,
              round: 1,
              time: entry.time,
              absent: entry.absent,
            }});
          }}
        }});
      }});
      return matches;
    }}

    function openResidentSearch() {{
      const query = residentSearchInput?.value.trim() || "";
      if (query.length < 2) {{
        window.alert("어르신 이름을 두 글자 이상 입력해 주세요.");
        residentSearchInput?.focus();
        return;
      }}
      const exactMatch = RESIDENT_NAMES.find((name) => name === query);
      const matchedName = exactMatch || RESIDENT_NAMES.find((name) => name.includes(query));
      if (!matchedName) {{
        window.alert("일치하는 어르신을 찾지 못했습니다.");
        return;
      }}
      const matches = residentMatchesForName(matchedName);
      if (!matches.length) {{
        window.alert("현재 날짜 운행표에 해당 어르신이 없습니다.");
        return;
      }}
      state.activeModal = {{ type: "resident-search", residentName: matchedName, matches }};
      renderModal();
    }}

    function simplifyAddress(address) {{
      if (!address) return "-";
      const tokens = address.trim().split(/\\s+/).filter(Boolean);
      const district = tokens.find((token) => token.endsWith("구"));
      const neighborhood = tokens.find((token) => token !== "용인시" && token !== district && (token.endsWith("동") || token.endsWith("읍") || token.endsWith("면") || token.endsWith("리") || token.endsWith("가") || token.includes("마을")));
      if (district && neighborhood) return `${{district}}-${{neighborhood}}`;
      if (neighborhood) return neighborhood;
      return tokens.find((token) => token !== "용인시") || address;
    }}

    function formatClock(value) {{
      if (!value) return "-";
      if (value === "결석") return value;
      if (value === "자가") return "-";
      const [hour, minute] = value.split(":");
      return `${{Number(hour)}}시 ${{minute}}분`;
    }}

    function activeCount(entries) {{
      return entries.filter((entry) => !entry.absent).length;
    }}

    function absentCount(entries) {{
      return entries.filter((entry) => entry.absent).length;
    }}

    function entryTimeRank(entry) {{
      if (entry.absent || !entry.time || !entry.time.includes(":")) return Number.MAX_SAFE_INTEGER;
      const [hour, minute] = entry.time.split(":").map(Number);
      return hour * 60 + minute;
    }}

    function sortEntries(entries) {{
      entries.sort((left, right) => {{
        const timeDiff = entryTimeRank(left) - entryTimeRank(right);
        if (timeDiff !== 0) return timeDiff;
        return (left.row || 0) - (right.row || 0);
      }});
    }}

    function flattenEntries(rounds) {{
      return rounds.flatMap((round) => round.entries);
    }}

    function primaryEntry(rounds) {{
      const entries = flattenEntries(rounds);
      return entries.find((entry) => !entry.absent) || entries[0] || null;
    }}

    function vehicleCardData(vehicle, side) {{
      const rounds = vehicle[`${{side}}_rounds`];
      const assignment = vehicle[`${{side}}_assignment`];
      const entries = flattenEntries(rounds);
      const first = primaryEntry(rounds);
      const companionDisplay =
        assignment.companion && assignment.companion_round
          ? `${{assignment.companion}}(${{assignment.companion_round}}차)`
          : assignment.companion;
      return {{
        vehicleName: vehicle.vehicle_name,
        displayName: vehicle.display_name,
        vehicleNumber: vehicle.vehicle_number,
        vehicleType: vehicle.vehicle_type,
        insuranceCompany: vehicle.insurance_company,
        insurancePhone: vehicle.insurance_phone,
        driver: assignment.driver,
        companion: companionDisplay,
        count: activeCount(entries),
        absentCount: absentCount(entries),
        roundCount: rounds.filter((round) => round.entries.length > 0).length,
        firstTime: first?.time || null,
        firstName: first?.name || null,
        firstAddressShort: simplifyAddress(first?.address || null),
      }};
    }}

    function renderHeroDate() {{
      if (!heroDateDisplay || !activeDate) return;
      heroDateDisplay.textContent = `${{activeDate.getMonth() + 1}}월 ${{activeDate.getDate()}}일 ${{weekdayNames[activeDate.getDay()]}}`;
    }}

    function updateMobileStickyOffset() {{
      const offset = window.innerWidth <= 768 && topShell ? Math.ceil(topShell.getBoundingClientRect().height + 18) : 12;
      document.documentElement.style.setProperty("--mobile-sticky-offset", `${{offset}}px`);
    }}

    function syncDateUrl(replace = false) {{
      if (!activeDate) return;
      const nextUrl = new URL(window.location.href);
      nextUrl.searchParams.set("date", activeDate.toISOString().slice(0, 10));
      nextUrl.searchParams.delete("nav");
      if (replace) {{
        window.history.replaceState({{ date: nextUrl.searchParams.get("date") }}, "", nextUrl);
      }} else {{
        window.history.pushState({{ date: nextUrl.searchParams.get("date") }}, "", nextUrl);
      }}
    }}

    function escapeHtml(value) {{
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }}

    function assignmentOptionsMarkup(role, side, vehicleName, currentValue) {{
      const selectedByOthers = new Set();
      state.data.vehicles.forEach((vehicle) => {{
        if (vehicle.vehicle_name === vehicleName) return;
        const assigned = vehicle[`${{side}}_assignment`]?.[role];
        if (assigned) selectedByOthers.add(assigned);
      }});
      const names = STAFF_OPTIONS[role].filter((name) => !selectedByOthers.has(name) || name === currentValue);
      if (currentValue && !names.includes(currentValue)) {{
        names.unshift(currentValue);
      }}
      return [
        `<option value="" ${{!currentValue ? "selected" : ""}}>선택 안 함</option>`,
        ...names.map((name) => `<option value="${{escapeHtml(name)}}" ${{name === currentValue ? "selected" : ""}}>${{escapeHtml(name)}}</option>`),
      ].join("");
    }}

    function vehicleCardMarkup(card, side) {{
      const vehicle = findVehicle(card.vehicleName);
      const assignment = vehicle?.[`${{side}}_assignment`] || {{ driver: "", companion: "" }};
      return `
        <article class="vehicle-card">
          <span class="vehicle-mark">${{card.vehicleName.replace("호차", "")}}</span>
          <div class="vehicle-card-top">
            <div class="vehicle-meta">
              <h3>${{card.vehicleName}}</h3>
              <div class="info-badges">
                <span class="info-badge">${{card.roundCount}}회차</span>
                ${{card.absentCount ? `<span class="info-badge is-alert">결석 ${{card.absentCount}}명</span>` : ""}}
              </div>
            </div>
            <button type="button" class="ghost-button" data-action="vehicle-info" data-side="${{side}}" data-vehicle="${{card.vehicleName}}">차량 정보</button>
          </div>
          <div class="vehicle-body">
            ${{card.driver ? `<div class="person-row driver"><span class="role-badge">운</span><strong>${{card.driver}}</strong></div>` : ""}}
            ${{card.companion ? `<div class="person-row companion"><span class="role-badge">동</span><strong>${{card.companion}}</strong></div>` : ""}}
            ${{state.isAdmin ? `
              <div class="assignment-editor">
                <div class="form-grid">
                  <label>운전자
                    <select data-field="assignment-driver">
                      ${{assignmentOptionsMarkup("driver", side, card.vehicleName, assignment.driver || "")}}
                    </select>
                  </label>
                  <label>동승자
                    <select data-field="assignment-companion">
                      ${{assignmentOptionsMarkup("companion", side, card.vehicleName, assignment.companion || "")}}
                    </select>
                  </label>
                </div>
                <div class="form-inline">
                  <button type="button" class="inline-button" data-action="save-assignment" data-side="${{side}}" data-vehicle="${{card.vehicleName}}">담당자 적용</button>
                </div>
              </div>
            ` : ""}}
            <div class="departure-line">
              <span class="departure-label">출발 시간</span>
              <strong>${{[formatClock(card.firstTime), card.firstName, card.firstAddressShort].filter(Boolean).join(" - ") || "-"}}</strong>
            </div>
            <div class="count-line">
              <span>${{side === "pickup" ? "등영" : "송영"}}</span>
              <strong>${{card.count}}</strong>
            </div>
          </div>
          <button type="button" class="schedule-link" data-action="open-schedule" data-side="${{side}}" data-vehicle="${{card.vehicleName}}">스케줄 보기</button>
        </article>
      `;
    }}

    function totalCountForSide(side) {{
      const selfKey = side === "pickup" ? "self_pickup" : "self_dropoff";
      return state.data.vehicles.reduce((sum, vehicle) => sum + activeCount(flattenEntries(vehicle[`${{side}}_rounds`])), 0) + activeCount(state.data[selfKey].entries);
    }}

    function transportSectionMarkup(side, mobile = false) {{
      const title = side === "pickup" ? "등영" : "송영";
      const eyebrow = side === "pickup" ? "Morning Route" : "Afternoon Route";
      const cards = state.data.vehicles.map((vehicle) => vehicleCardData(vehicle, side)).map((card) => vehicleCardMarkup(card, side)).join("");
      const total = totalCountForSide(side);
      const orderStrip = side === "dropoff"
        ? `
          <section class="order-strip-card">
            <div class="order-strip-copy"><p class="eyebrow">송영 운행 순서</p><h3>차량 출발 순서</h3></div>
            <div class="order-strip-items">${{state.data.home.dropoff_order_cards.map((item) => `<div class="order-item"><strong>${{item.vehicle_name.replace("호차", "")}}</strong><span>${{item.minute}}분</span></div>`).join("")}}</div>
          </section>
        `
        : "";
      const mobileTabs = mobile
        ? `
          <div class="mobile-side-tabs">
            <button type="button" class="mobile-side-tab ${{side === "pickup" ? "is-active" : ""}}" data-action="set-mobile-side" data-side="pickup">등영</button>
            <button type="button" class="mobile-side-tab ${{side === "dropoff" ? "is-active" : ""}}" data-action="set-mobile-side" data-side="dropoff">송영</button>
          </div>
        `
        : "";
      return `
        <section class="transport-section${{mobile ? " mobile-transport" : ""}}">
          ${{mobile ? `<div class="mobile-section-head">${{mobileTabs}}<p class="eyebrow">${{eyebrow}}</p></div>` : ""}}
          <div class="section-heading">
            <div><p class="eyebrow">${{eyebrow}}</p><h2>${{title}}</h2></div>
            <div class="section-total">${{title}} 인원 ${{total}}명</div>
          </div>
          ${{mobile ? `<div class="section-total mobile-total">${{title}} 인원 ${{total}}명</div>` : ""}}
          ${{orderStrip}}
          <div class="vehicle-grid">${{cards}}</div>
          <div class="self-row">${{selfCardMarkup(side)}}</div>
        </section>
      `;
    }}

    function selfCardMarkup(side) {{
      const title = side === "pickup" ? "자가 등영" : "자가 송영";
      const entries = state.data[side === "pickup" ? "self_pickup" : "self_dropoff"].entries;
      return `
        <button type="button" class="self-card" data-action="open-self" data-side="${{side}}">
          <span>${{title}}</span>
          <strong>명단 보기</strong>
          <small>${{activeCount(entries)}}명</small>
        </button>
      `;
    }}

    function renderApp() {{
      if (!state.data) {{
        const isLoading = state.remoteBootstrapping;
        appRoot.innerHTML = `
          <div class="mobile-only">
            <section class="transport-section">
              <div class="mobile-side-tabs">
                <button type="button" class="mobile-side-tab is-active" data-action="set-mobile-side" data-side="pickup">등영</button>
                <button type="button" class="mobile-side-tab" data-action="set-mobile-side" data-side="dropoff">송영</button>
              </div>
              <div class="section-heading">
                <div><p class="eyebrow">${{isLoading ? "Loading" : "No Schedule"}}</p><h2>${{isLoading ? "운행표 불러오는 중" : "운행표 없음"}}</h2></div>
              </div>
              <p class="empty-copy">${{isLoading ? "최신 운행표를 확인하고 있습니다." : "선택한 날짜의 셔틀 운행표 데이터가 없습니다."}}</p>
            </section>
          </div>
          <div class="desktop-only">
            <section class="transport-section">
              <div class="section-heading">
                <div><p class="eyebrow">${{isLoading ? "Loading" : "No Schedule"}}</p><h2>${{isLoading ? "운행표 불러오는 중" : "운행표 없음"}}</h2></div>
              </div>
              <p class="empty-copy">${{isLoading ? "최신 운행표를 확인하고 있습니다." : "선택한 날짜의 셔틀 운행표 데이터가 없습니다."}}</p>
            </section>
          </div>
        `;
        adminToggle.textContent = state.isAdmin ? "관리자 종료" : "관리자 로그인";
        resetMenuItem.hidden = !state.isAdmin;
        uploadMenuItem.hidden = !state.isAdmin || !state.backendConfigured;
        return;
      }}
      appRoot.innerHTML = `
        <div class="mobile-only">${{transportSectionMarkup(state.mobileSide, true)}}</div>
        <div class="desktop-only">
          ${{transportSectionMarkup("pickup")}}
          ${{transportSectionMarkup("dropoff")}}
        </div>
      `;
      adminToggle.textContent = state.isAdmin ? "관리자 종료" : "관리자 로그인";
      resetMenuItem.hidden = !state.isAdmin;
      uploadMenuItem.hidden = !state.isAdmin || !state.backendConfigured;
      if (state.activeModal) {{
        renderModal();
      }}
      updateMobileStickyOffset();
    }}

    function normalizeVehicleSide(vehicle, side) {{
      const rounds = vehicle[`${{side}}_rounds`].filter((round) => round.entries.length > 0);
      rounds.forEach((round, roundIndex) => {{
        round.round = roundIndex + 1;
        sortEntries(round.entries);
      }});
      let seq = 1;
      rounds.forEach((round) => {{
        round.entries.forEach((entry) => {{
          entry.sequence = seq++;
        }});
      }});
      vehicle[`${{side}}_rounds`] = rounds;
    }}

    function normalizeSelfEntries(key) {{
      const entries = state.data[key].entries;
      sortEntries(entries);
      entries.forEach((entry, index) => {{
        entry.sequence = index + 1;
      }});
    }}

    function ensureTargetRound(vehicle, side, targetRound) {{
      while (vehicle[`${{side}}_rounds`].length < targetRound) {{
        vehicle[`${{side}}_rounds`].push({{ round: vehicle[`${{side}}_rounds`].length + 1, fill_id: 0, entries: [] }});
      }}
      return vehicle[`${{side}}_rounds`][targetRound - 1];
    }}

    function findVehicle(vehicleName) {{
      return state.data.vehicles.find((vehicle) => vehicle.vehicle_name === vehicleName);
    }}

    function markOppositeSideAbsentByName(name, side, absent) {{
      if (!name || !absent) return;
      const oppositeSide = side === "pickup" ? "dropoff" : "pickup";
      const oppositeSelfKey = side === "pickup" ? "self_dropoff" : "self_pickup";
      state.data.vehicles.forEach((vehicle) => {{
        vehicle[`${{oppositeSide}}_rounds`].forEach((round) => {{
          round.entries.forEach((entry) => {{
            if (entry.name === name) {{
              entry.absent = true;
            }}
          }});
        }});
        normalizeVehicleSide(vehicle, oppositeSide);
      }});
      state.data[oppositeSelfKey].entries.forEach((entry) => {{
        if (entry.name === name) {{
          entry.absent = true;
        }}
      }});
      normalizeSelfEntries(oppositeSelfKey);
    }}

    function renderVehicleInfoModal(side, vehicleName) {{
      const vehicle = findVehicle(vehicleName);
      if (!vehicle) return "";
      const card = vehicleCardData(vehicle, side);
      return `
        <div class="modal-shell narrow">
          <div class="modal-header">
            <div><p class="eyebrow">차량 정보</p><h3>${{card.displayName}}</h3></div>
            <button type="button" class="modal-close" data-action="close-dialog">닫기</button>
          </div>
          <div class="modal-content meta-grid">
            <div><span>차종</span><strong>${{card.vehicleType}}</strong></div>
            <div><span>차량 번호</span><strong>${{card.vehicleNumber}}</strong></div>
            <div><span>보험사</span><strong>${{card.insuranceCompany}}</strong></div>
            <div><span>보험사 전화번호</span><strong>${{card.insurancePhone}}</strong></div>
          </div>
        </div>
      `;
    }}

    function renderExportModal(kind) {{
      const title = kind === "original" ? "원본 내보내기" : "수정본 내보내기";
      return `
        <div class="modal-shell narrow">
          <div class="modal-header">
            <div><p class="eyebrow">엑셀 내보내기</p><h3>${{title}}</h3></div>
            <button type="button" class="modal-close" data-action="close-dialog">닫기</button>
          </div>
          <div class="modal-content">
            <button type="button" class="schedule-link" data-action="confirm-export" data-kind="${{kind}}" data-scope="all">전체</button>
            <button type="button" class="schedule-link" data-action="confirm-export" data-kind="${{kind}}" data-scope="vehicle">호차별</button>
          </div>
        </div>
      `;
    }}

    function renderUploadModal() {{
      return `
        <div class="modal-shell narrow">
          <div class="modal-header">
            <div><p class="eyebrow">엑셀 업로드</p><h3>엑셀 파일 반영</h3></div>
            <button type="button" class="modal-close" data-action="close-dialog">닫기</button>
          </div>
          <div class="modal-content">
            <div class="meta-grid">
              <div>
                <span>파일</span>
                <input id="upload-workbook-file" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" />
              </div>
              <div>
                <span>업로드한 사람</span>
                <input id="upload-workbook-by" type="text" placeholder="예: 김은비" />
              </div>
            </div>
            <p class="empty-copy">월별 엑셀을 올리면 같은 달 일정이 DB 원본으로 갱신됩니다.</p>
            <button type="button" class="primary-button" data-action="submit-upload">업로드</button>
          </div>
        </div>
      `;
    }}

    function entryEditorMarkup(entry, side, vehicleName, roundNumber, sourceKind) {{
      if (!state.isAdmin) return "";
      const targetSelfValue = side === "pickup" ? "self_pickup" : "self_dropoff";
      const currentVehicle = sourceKind === "self" ? targetSelfValue : vehicleName;
      const vehicleOptions = state.data.vehicles.map((vehicle) => `<option value="${{vehicle.vehicle_name}}" ${{vehicle.vehicle_name === currentVehicle ? "selected" : ""}}>${{vehicle.vehicle_name}}</option>`).join("");
      return `
        <div class="entry-editor">
          <div class="form-grid">
            <label>시간<input type="time" data-field="time" value="${{entry.time && entry.time.includes(":") ? entry.time : ""}}" /></label>
            <label>이동 차량
              <select data-field="vehicle">
                ${{vehicleOptions}}
                <option value="${{targetSelfValue}}" ${{currentVehicle === targetSelfValue ? "selected" : ""}}>${{side === "pickup" ? "자가 등영" : "자가 송영"}}</option>
              </select>
            </label>
            <label>회차
              <select data-field="round" ${{currentVehicle === targetSelfValue ? "disabled" : ""}}>
                ${{[1, 2, 3, 4, 5].map((round) => `<option value="${{round}}" ${{round === roundNumber ? "selected" : ""}}>${{round}}차</option>`).join("")}}
              </select>
            </label>
            <label>결석
              <select data-field="absent">
                <option value="false" ${{!entry.absent ? "selected" : ""}}>출석</option>
                <option value="true" ${{entry.absent ? "selected" : ""}}>결석</option>
              </select>
            </label>
          </div>
          <div class="form-inline">
            <button type="button" class="inline-button" data-action="save-entry" data-source-kind="${{sourceKind}}" data-side="${{side}}" data-vehicle="${{vehicleName}}" data-round="${{roundNumber}}" data-row="${{entry.row}}">적용</button>
          </div>
        </div>
      `;
    }}

    function scheduleEntryMarkup(entry, side, vehicleName, roundNumber, sourceKind, highlightName = "") {{
      const isSearchHit = highlightName && entry.name === highlightName;
      return `
        <li class="entry-card ${{entry.absent ? "is-absent" : ""}} ${{isSearchHit ? "is-search-hit" : ""}}" data-entry-row="${{entry.row}}">
          <div class="entry-main"><strong>${{entry.sequence}}. ${{entry.name || "-"}}</strong><span>${{entry.absent ? "결석" : formatClock(entry.time)}}</span></div>
          <div class="entry-sub">${{entry.address || "-"}}</div>
          ${{entry.note ? `<div class="entry-note">${{entry.note}}</div>` : ""}}
          ${{entryEditorMarkup(entry, side, vehicleName, roundNumber, sourceKind)}}
        </li>
      `;
    }}

    function renderScheduleModal(side, vehicleName) {{
      const vehicle = findVehicle(vehicleName);
      if (!vehicle) return "";
      const rounds = vehicle[`${{side}}_rounds`];
      const sideLabel = side === "pickup" ? "등영" : "송영";
      const totalCount = activeCount(flattenEntries(rounds));
      const highlightName = state.activeModal?.highlightName || "";
      return `
        <div class="modal-shell">
          <div class="modal-header">
            <div><p class="eyebrow">스케줄 ${{state.isAdmin ? "편집" : "보기"}}</p><h3>${{vehicle.display_name}} ${{sideLabel}}</h3></div>
            <div class="role-pill">${{sideLabel}} 인원 ${{totalCount}}명</div>
            <button type="button" class="modal-close" data-action="close-dialog">닫기</button>
          </div>
          <div class="modal-content">
            ${{rounds.length ? rounds.map((round) => `
              <section class="schedule-round">
                <h4>${{round.round}}차</h4>
                <ul class="schedule-list">${{round.entries.map((entry) => scheduleEntryMarkup(entry, side, vehicleName, round.round, "vehicle", highlightName)).join("")}}</ul>
              </section>
            `).join("") : `<p class="empty-copy">명단이 없습니다.</p>`}}
          </div>
        </div>
      `;
    }}

    function renderSelfModal(side) {{
      const key = side === "pickup" ? "self_pickup" : "self_dropoff";
      const title = side === "pickup" ? "자가 등영" : "자가 송영";
      const entries = state.data[key].entries;
      const highlightName = state.activeModal?.highlightName || "";
      return `
        <div class="modal-shell">
          <div class="modal-header">
            <div><p class="eyebrow">자가 명단 ${{state.isAdmin ? "편집" : "보기"}}</p><h3>${{title}}</h3></div>
            <button type="button" class="modal-close" data-action="close-dialog">닫기</button>
          </div>
          <div class="modal-content">
            <section class="self-list-shell">
              <ul class="self-list">
                ${{entries.length ? entries.map((entry) => `
                  <li class="entry-card ${{entry.absent ? "is-absent" : ""}} ${{highlightName && entry.name === highlightName ? "is-search-hit" : ""}}" data-entry-row="${{entry.row}}">
                    <div class="entry-main"><strong>${{entry.sequence}}. ${{entry.name || "-"}}</strong><span>${{entry.absent ? "결석" : formatClock(entry.time)}}</span></div>
                    <div class="entry-sub">${{entry.address || "-"}}</div>
                    ${{entryEditorMarkup(entry, side, key, 1, "self")}}
                  </li>
                `).join("") : `<li class="entry-card empty-copy">명단이 없습니다.</li>`}}
              </ul>
            </section>
          </div>
        </div>
      `;
    }}

    function renderResidentSearchModal() {{
      const residentName = state.activeModal?.residentName || "";
      const matches = state.activeModal?.matches || [];
      const sideRow = (side) => {{
        const match = matches.find((item) => item.side === side && !item.absent) || matches.find((item) => item.side === side);
        if (!match) {{
          return `<div class="entry-card"><div class="entry-main"><strong>${{side === "pickup" ? "등영" : "송영"}}</strong><span>-</span></div></div>`;
        }}
        return `
          <button type="button" class="schedule-link search-match-button" data-action="open-search-result" data-source-kind="${{match.sourceKind}}" data-side="${{match.side}}" data-vehicle="${{match.vehicleName}}" data-row="${{match.row}}">
            <span>${{side === "pickup" ? "등영" : "송영"}} ${{match.displayName}} ${{match.absent ? "결석" : formatClock(match.time)}}</span>
            <span class="search-match-meta">→</span>
          </button>
        `;
      }};
      return `
        <div class="modal-shell narrow">
          <div class="modal-header">
            <div><p class="eyebrow">어르신 찾기</p><h3>이동 경로</h3></div>
            <button type="button" class="modal-close" data-action="close-dialog">닫기</button>
          </div>
          <div class="modal-content">
            ${{matches.length ? `
              <section class="self-list-shell">
                <div class="entry-card" style="padding-top:0;">
                  <div class="entry-main"><strong>${{residentName}} 어르신</strong></div>
                </div>
                ${{sideRow("pickup")}}
                ${{sideRow("dropoff")}}
              </section>
            ` : `<p class="empty-copy">현재 날짜 운행표에 해당 어르신이 없습니다.</p>`}}
          </div>
        </div>
      `;
    }}

    function renderModal() {{
      if (!state.activeModal) {{
        appDialog.dataset.sheet = "false";
        appDialog.close();
        appDialog.innerHTML = "";
        return;
      }}
      if (state.activeModal.type === "vehicle-info") {{
        appDialog.innerHTML = renderVehicleInfoModal(state.activeModal.side, state.activeModal.vehicleName);
      }}
      if (state.activeModal.type === "schedule") {{
        appDialog.innerHTML = renderScheduleModal(state.activeModal.side, state.activeModal.vehicleName);
      }}
      if (state.activeModal.type === "self") {{
        appDialog.innerHTML = renderSelfModal(state.activeModal.side);
      }}
      if (state.activeModal.type === "export") {{
        appDialog.innerHTML = renderExportModal(state.activeModal.kind);
      }}
      if (state.activeModal.type === "upload") {{
        appDialog.innerHTML = renderUploadModal();
      }}
      if (state.activeModal.type === "resident-search") {{
        appDialog.innerHTML = renderResidentSearchModal();
      }}
      appDialog.dataset.sheet = ["schedule", "self", "resident-search"].includes(state.activeModal.type) ? "true" : "false";
      if (!appDialog.open) {{
        appDialog.showModal();
      }}
    }}

    function syncEntryEditorFields(container) {{
      if (!container) return;
      const vehicleSelect = container.querySelector('[data-field="vehicle"]');
      const roundSelect = container.querySelector('[data-field="round"]');
      if (!vehicleSelect || !roundSelect) return;
      const isSelfTarget = vehicleSelect.value === "self_pickup" || vehicleSelect.value === "self_dropoff";
      roundSelect.disabled = isSelfTarget;
      if (isSelfTarget) {{
        roundSelect.value = "1";
      }}
    }}

    async function downloadExport(kind, scope) {{
      const response = await window.fetch("./export", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{
          kind,
          scope,
          data: kind === "edited" ? state.data : baseScheduleForDate(),
        }}),
      }});
      if (!response.ok) {{
        window.alert("엑셀 내보내기에 실패했습니다.");
        return;
      }}
      const blob = await response.blob();
      const contentDisposition = response.headers.get("Content-Disposition") || "";
      const fileNameMatch = contentDisposition.match(/filename\\*=UTF-8''([^;]+)/);
      const fileName = fileNameMatch ? decodeURIComponent(fileNameMatch[1]) : "등송영표_export.xlsx";
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = fileName;
      anchor.click();
      URL.revokeObjectURL(url);
    }}

    async function uploadWorkbookFromModal() {{
      if (!state.isAdmin) {{
        window.alert("관리자 로그인 후 업로드할 수 있습니다.");
        return;
      }}
      if (!state.backendConfigured) {{
        window.alert("업로드 서버 설정이 아직 완료되지 않았습니다.");
        return;
      }}
      const fileInput = document.getElementById("upload-workbook-file");
      const uploadedByInput = document.getElementById("upload-workbook-by");
      const file = fileInput?.files?.[0];
      if (!file) {{
        window.alert("업로드할 월별 엑셀 파일을 선택해 주세요.");
        return;
      }}
      const formData = new FormData();
      formData.append("workbook", file);
      formData.append("uploaded_by", uploadedByInput?.value?.trim() || ADMIN_CONFIG.label);
      try {{
        const response = await window.fetch(API_ENDPOINTS.upload, {{
          method: "POST",
          headers: {{
            "X-Bandi-Admin-Hash": getAdminSessionToken(),
          }},
          body: formData,
        }});
        const rawText = await response.text();
        let payload = {{}};
        if (rawText) {{
          try {{
            payload = JSON.parse(rawText);
          }} catch (error) {{
            payload = {{ error: rawText.trim() }};
          }}
        }}
        if (!response.ok) {{
          const reason = payload.error || rawText.trim() || `HTTP ${{response.status}}`;
          window.alert(`엑셀 업로드에 실패했습니다.\n\n${{reason}}`);
          return;
        }}
        await refreshScheduleBundle();
        activeDate = parseActiveDate();
        state.mobileSide = defaultMobileSide();
        await syncScheduleForActiveDate();
        renderHeroDate();
        syncDateUrl(true);
        state.activeModal = null;
        appDialog.close();
        renderApp();
        window.alert(`업로드 반영 완료: ${{payload.month_key || "-"}} (${{payload.updated_dates?.length || 0}}일)`);
      }} catch (error) {{
        window.alert(`엑셀 업로드에 실패했습니다.\n\n${{error?.message || "네트워크 연결 또는 서버 응답을 확인해 주세요."}}`);
      }}
    }}

    async function updateAssignmentFromForm(button) {{
      const card = button.closest(".vehicle-card");
      if (!card) return;
      const vehicle = findVehicle(button.dataset.vehicle);
      const side = button.dataset.side;
      if (!vehicle || !side) return;
      const driver = card.querySelector('[data-field="assignment-driver"]')?.value.trim() || null;
      const companion = card.querySelector('[data-field="assignment-companion"]')?.value.trim() || null;
      vehicle[`${{side}}_assignment`] = {{ driver, companion }};
      await persistData();
      renderApp();
    }}

    async function updateEntryFromForm(button) {{
      const card = button.closest(".entry-card");
      if (!card) return;
      const formValues = {{
        time: card.querySelector('[data-field="time"]')?.value || "",
        vehicle: card.querySelector('[data-field="vehicle"]')?.value,
        round: Number(card.querySelector('[data-field="round"]')?.value || "1"),
        absent: card.querySelector('[data-field="absent"]')?.value === "true",
      }};
      const sourceKind = button.dataset.sourceKind;
      const side = button.dataset.side;
      const sourceVehicle = button.dataset.vehicle;
      const sourceRound = Number(button.dataset.round || "1");
      const row = Number(button.dataset.row);
      let entry = null;

      if (sourceKind === "vehicle") {{
        const vehicle = findVehicle(sourceVehicle);
        const round = vehicle?.[`${{side}}_rounds`].find((item) => item.round === sourceRound);
        const index = round?.entries.findIndex((item) => item.row === row);
        if (index == null || index < 0) return;
        entry = round.entries.splice(index, 1)[0];
        normalizeVehicleSide(vehicle, side);
      }} else {{
        const selfKey = sourceVehicle;
        const entries = state.data[selfKey].entries;
        const index = entries.findIndex((item) => item.row === row);
        if (index < 0) return;
        entry = entries.splice(index, 1)[0];
        normalizeSelfEntries(selfKey);
      }}

      entry.absent = formValues.absent;
      if (formValues.time) {{
        entry.time = formValues.time;
      }} else if (!entry.absent && entry.time === "결석") {{
        entry.time = "";
      }}

      if (formValues.vehicle === "self_pickup" || formValues.vehicle === "self_dropoff") {{
        const selfEntries = state.data[formValues.vehicle].entries;
        selfEntries.push(entry);
        normalizeSelfEntries(formValues.vehicle);
      }} else {{
        const targetVehicle = findVehicle(formValues.vehicle);
        const targetRound = ensureTargetRound(targetVehicle, side, formValues.round);
        targetRound.entries.push(entry);
        normalizeVehicleSide(targetVehicle, side);
      }}

      markOppositeSideAbsentByName(entry.name, side, entry.absent);

      await persistData();
      renderApp();
    }}

    async function sha256(text) {{
      const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
      return Array.from(new Uint8Array(digest)).map((byte) => byte.toString(16).padStart(2, "0")).join("");
    }}

    heroDateRow.querySelectorAll("[data-shift-date]").forEach((button) => {{
      button.addEventListener("click", async () => {{
        activeDate.setDate(activeDate.getDate() + Number(button.dataset.shiftDate));
        state.mobileSide = defaultMobileSide();
        renderHeroDate();
        updateMobileStickyOffset();
        syncDateUrl();
        await syncScheduleForActiveDate();
        renderApp();
      }});
    }});

    window.addEventListener("popstate", async () => {{
      activeDate = parseActiveDate();
      state.mobileSide = defaultMobileSide();
      renderHeroDate();
      updateMobileStickyOffset();
      await syncScheduleForActiveDate();
      renderApp();
    }});

    function positionMenuPanel() {{
      if (!menuPanel || !menuToggle) return;
      const rect = menuToggle.getBoundingClientRect();
      const viewportWidth = window.innerWidth;
      const panelWidth = Math.min(248, Math.max(196, viewportWidth - 32));
      const desiredLeft = Math.max(16, rect.left - panelWidth - 12);
      const desiredTop = Math.max(16, rect.top + rect.height / 2);
      menuPanel.style.left = `${{desiredLeft}}px`;
      menuPanel.style.right = "auto";
      menuPanel.style.top = `${{desiredTop}}px`;
      menuPanel.style.width = `${{panelWidth}}px`;
    }}

    window.addEventListener("resize", () => {{
      updateMobileStickyOffset();
      if (menuPanel.classList.contains("is-open")) {{
        positionMenuPanel();
      }}
    }});
    document.addEventListener("visibilitychange", () => {{
      if (!document.hidden) {{
        refreshSharedState();
      }}
    }});

    window.addEventListener("mouseup", (event) => {{
      if (event.button === 3 && window.history.length > 1) {{
        event.preventDefault();
        window.history.back();
      }}
      if (event.button === 4) {{
        event.preventDefault();
        window.history.forward();
      }}
    }});

    async function toggleAdminMode() {{
      if (state.isAdmin) {{
        state.isAdmin = false;
        setAdminSessionToken("");
        menuPanel.classList.remove("is-open");
        renderApp();
        return;
      }}
      const pin = window.prompt(`${{ADMIN_CONFIG.label}} PIN을 입력하세요.`);
      if (!pin) return;
      const hash = await sha256(pin);
      if (hash !== ADMIN_CONFIG.pinHash) {{
        window.alert("관리자 PIN이 일치하지 않습니다.");
        return;
      }}
      state.isAdmin = true;
      setAdminSessionToken(ADMIN_CONFIG.pinHash);
      menuPanel.classList.remove("is-open");
      renderApp();
    }}

    menuToggle.addEventListener("click", (event) => {{
      event.stopPropagation();
      if (!menuPanel.classList.contains("is-open")) {{
        positionMenuPanel();
      }}
      menuPanel.classList.toggle("is-open");
    }});

    adminToggle.addEventListener("click", toggleAdminMode);
    resetMenuItem.addEventListener("click", async () => {{
      if (!window.confirm("수정 내용을 초기화하고 원본 상태로 되돌릴까요?")) return;
      const schedule = baseScheduleForDate();
      if (schedule) {{
        localStorage.removeItem(storageKey(schedule));
        await clearRemoteOverride(activeDateKey());
        state.data = clone(schedule);
      }} else {{
        state.data = null;
      }}
      menuPanel.classList.remove("is-open");
      renderApp();
    }});
    residentSearchInput.addEventListener("input", updateResidentSuggestions);
    residentSearchInput.addEventListener("change", () => {{
      residentSuggestions.innerHTML = "";
      residentSearchInput.blur();
    }});
    residentSearchInput.addEventListener("keydown", (event) => {{
      if (event.key === "Enter") {{
        event.preventDefault();
        residentSuggestions.innerHTML = "";
        openResidentSearch();
      }}
    }});
    residentSearchButton.addEventListener("click", openResidentSearch);

    appRoot.addEventListener("click", async (event) => {{
      const button = event.target.closest("[data-action]");
      if (!button) return;
      const action = button.dataset.action;
      if (action === "set-mobile-side") {{
        state.mobileSide = button.dataset.side === "dropoff" ? "dropoff" : "pickup";
        renderApp();
        return;
      }}
      if (action === "vehicle-info") {{
        state.activeModal = {{ type: "vehicle-info", side: button.dataset.side, vehicleName: button.dataset.vehicle }};
        renderModal();
      }}
      if (action === "open-schedule") {{
        state.activeModal = {{ type: "schedule", side: button.dataset.side, vehicleName: button.dataset.vehicle }};
        renderModal();
      }}
      if (action === "open-self") {{
        state.activeModal = {{ type: "self", side: button.dataset.side }};
        renderModal();
      }}
      if (action === "open-export") {{
        state.activeModal = {{ type: "export", kind: button.dataset.kind }};
        menuPanel.classList.remove("is-open");
        renderModal();
      }}
      if (action === "open-upload") {{
        if (!state.isAdmin) {{
          menuPanel.classList.remove("is-open");
          window.alert("관리자 로그인 후 업로드할 수 있습니다.");
          return;
        }}
        state.activeModal = {{ type: "upload" }};
        menuPanel.classList.remove("is-open");
        renderModal();
      }}
      if (action === "reset-schedule") {{
        resetMenuItem.click();
      }}
      if (action === "save-assignment") {{
        await updateAssignmentFromForm(button);
      }}
    }});

    document.addEventListener("click", (event) => {{
      const actionButton = event.target.closest("[data-action]");
      if (actionButton && actionButton.dataset.action === "open-export") {{
        state.activeModal = {{ type: "export", kind: actionButton.dataset.kind }};
        menuPanel.classList.remove("is-open");
        renderModal();
        return;
      }}
      if (actionButton && actionButton.dataset.action === "open-upload") {{
        if (!state.isAdmin) {{
          menuPanel.classList.remove("is-open");
          window.alert("관리자 로그인 후 업로드할 수 있습니다.");
          return;
        }}
        state.activeModal = {{ type: "upload" }};
        menuPanel.classList.remove("is-open");
        renderModal();
        return;
      }}
      if (!event.target.closest(".header-menu") && !event.target.closest("#menu-panel")) {{
        menuPanel.classList.remove("is-open");
      }}
    }});

    appDialog.addEventListener("click", async (event) => {{
      const button = event.target.closest("[data-action]");
      if (button && button.dataset.action === "close-dialog") {{
        state.activeModal = null;
        appDialog.close();
        return;
      }}
      if (button && button.dataset.action === "save-entry") {{
        await updateEntryFromForm(button);
      }}
      if (button && button.dataset.action === "confirm-export") {{
        downloadExport(button.dataset.kind, button.dataset.scope);
        state.activeModal = null;
        appDialog.close();
        return;
      }}
      if (button && button.dataset.action === "submit-upload") {{
        await uploadWorkbookFromModal();
        return;
      }}
      if (button && button.dataset.action === "open-search-result") {{
        const highlightName = state.activeModal?.residentName || "";
        state.activeModal =
          button.dataset.sourceKind === "self"
            ? {{ type: "self", side: button.dataset.side, highlightName }}
            : {{ type: "schedule", side: button.dataset.side, vehicleName: button.dataset.vehicle, highlightName }};
        renderModal();
        return;
      }}
      const rect = appDialog.getBoundingClientRect();
      const inside = rect.top <= event.clientY && event.clientY <= rect.bottom && rect.left <= event.clientX && event.clientX <= rect.right;
      if (!inside && event.target === appDialog) {{
        state.activeModal = null;
        appDialog.close();
      }}
    }});

    appDialog.addEventListener("change", (event) => {{
      const field = event.target;
      if (!(field instanceof HTMLElement)) return;
      if (field.matches('[data-field="vehicle"]')) {{
        syncEntryEditorFields(field.closest(".entry-editor"));
      }}
    }});

    async function initializeApp() {{
      activeDate = parseActiveDate();
      state.mobileSide = defaultMobileSide();
      await syncScheduleForActiveDate();
      renderHeroDate();
      updateMobileStickyOffset();
      syncDateUrl(true);
      renderApp();

      state.backendConfigured = await fetchBackendConfig();
      ensureSharedRefreshLoop();
      if (!state.backendConfigured) {{
        state.remoteBootstrapping = false;
        renderApp();
        return;
      }}
      await refreshScheduleBundle();
      await syncScheduleForActiveDate();
      state.remoteBootstrapping = false;
      renderHeroDate();
      updateMobileStickyOffset();
      syncDateUrl(true);
      renderApp();
    }}

    initializeApp();
  </script>
</body>
</html>
"""


def render_calendar_html(data: dict, schedule_bundle: dict[str, dict] | None = None) -> str:
    base_date = derive_base_date(data)
    if schedule_bundle is None:
        schedule_bundle = {base_date.isoformat(): data}
    base_date = latest_schedule_date(schedule_bundle, base_date)
    schedule_payload = load_schedule_calendar_payload(base_date)
    months = schedule_payload["months"]
    days = schedule_payload["days"]
    month_label = f"{base_date.year}년 {base_date.month}월"
    shuttle_counts = {date_key: parsed["totals"]["pickup"] for date_key, parsed in schedule_bundle.items()}
    schedule_json = (
        json.dumps(schedule_bundle, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("</script", "<\\/script")
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>반디 월별 캘린더</title>
  <style>
    :root {{
      --ink: #31353c;
      --ink-soft: #72767d;
      --paper-base: rgba(250, 245, 236, 0.88);
      --paper-strong: rgba(252, 248, 241, 0.96);
      --line: rgba(49, 53, 60, 0.12);
      --shadow-lg: 0 28px 48px rgba(95, 79, 58, 0.18);
      --shadow-sm: 0 10px 18px rgba(95, 79, 58, 0.11);
      --paper-lift: 0 2px 0 rgba(255, 255, 255, 0.5) inset, 0 -10px 18px rgba(95, 79, 58, 0.035) inset;
      --radius-xl: 34px;
      --radius-lg: 26px;
      --font-display: "Avenir Next", "Apple SD Gothic Neo", sans-serif;
      --font-body: "Pretendard", "Apple SD Gothic Neo", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: var(--font-body);
      color: var(--ink);
      background: linear-gradient(180deg, #efe6d8 0%, #f5eee4 40%, #ede3d4 100%);
    }}
    .page-shell {{
      max-width: 520px;
      margin: 0 auto;
      padding: 18px 12px 32px;
    }}
    .site-header,
    .calendar-header,
    .calendar-grid-shell {{
      position: relative;
      overflow: hidden;
      border: 1px solid rgba(49,53,60,0.08);
      background: var(--paper-base);
      border-radius: 28px;
      box-shadow: var(--shadow-lg), var(--paper-lift);
    }}
    .site-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 16px 20px;
      border-radius: 999px;
      margin-bottom: 28px;
      overflow: visible;
    }}
    .site-header::before,
    .calendar-header::before,
    .calendar-grid-shell::before {{
      content: "";
      position: absolute;
      inset: 0;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.34), transparent 32%),
        repeating-linear-gradient(0deg, transparent 0, transparent 10px, rgba(120,103,79,0.012) 10px, rgba(120,103,79,0.012) 11px);
      pointer-events: none;
    }}
    .brand {{
      display: inline-flex;
      gap: 14px;
      align-items: center;
      text-decoration: none;
      color: inherit;
      position: relative;
      z-index: 1;
    }}
    .brand img {{ width: 50px; height: 50px; object-fit: contain; }}
    .brand span {{ font-family: var(--font-display); font-size: 1.34rem; font-weight: 730; }}
    .nav-link {{
      display: inline-flex;
      align-items: center;
      min-height: 42px;
      padding: 0 16px;
      border-radius: 999px;
      border: 1px solid rgba(49,53,60,0.08);
      background: rgba(255,255,255,0.72);
      text-decoration: none;
      color: inherit;
      font-weight: 700;
      position: relative;
      z-index: 1;
    }}
    .calendar-layout {{ display: grid; gap: 28px; }}
    .eyebrow {{
      margin: 0 0 10px;
      color: var(--ink-soft);
      font-size: 0.82rem;
      font-weight: 740;
      letter-spacing: 0.18em;
      text-transform: uppercase;
    }}
    .calendar-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 22px 24px;
    }}
    .calendar-header > * {{ position: relative; z-index: 1; }}
    h1 {{
      margin: 0;
      font-family: var(--font-display);
      font-size: clamp(2rem, 3.4vw, 3.2rem);
      line-height: 1;
      letter-spacing: -0.04em;
    }}
    .calendar-controls {{ display: flex; gap: 12px; }}
    .month-select {{
      min-width: 210px;
      min-height: 48px;
      padding: 0 18px;
      border: 1px solid rgba(49,53,60,0.08);
      border-radius: 999px;
      background: rgba(252, 248, 241, 0.9);
      font-weight: 650;
    }}
    .calendar-grid-shell {{
      padding: 18px;
      background: rgba(247, 242, 234, 0.9);
    }}
    .calendar-grid {{
      position: relative;
      z-index: 1;
      display: grid;
      grid-template-columns: repeat(7, minmax(0, 1fr));
      gap: 14px;
    }}
    .weekday-chip {{
      min-height: 42px;
      display: grid;
      place-items: center;
      border-radius: 999px;
      background: rgba(255,255,255,0.72);
      border: 1px solid rgba(49, 53, 60, 0.08);
      font-size: 0.84rem;
      font-weight: 760;
      color: #6f747d;
    }}
    .weekday-chip.is-sunday {{ color: #8a7278; }}
    .day-card {{
      min-height: 178px;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      border: 1px solid rgba(49, 53, 60, 0.08);
      border-radius: 20px;
      background: #fbf7f0;
      box-shadow: var(--shadow-sm), var(--paper-lift);
      text-align: left;
      cursor: pointer;
      transition: transform 180ms ease, box-shadow 180ms ease, border-color 180ms ease;
    }}
    .day-card:hover {{
      transform: translateY(-2px);
      box-shadow: 0 16px 24px rgba(95,79,58,0.14), var(--paper-lift);
    }}
    .day-card.closed {{
      background: #efebe3;
    }}
    .day-card.empty-slot {{
      min-height: 0;
      padding: 0;
      background: transparent;
      border: 0;
      box-shadow: none;
      cursor: default;
    }}
    .day-card.selected {{
      border-color: rgba(143,115,92,0.38);
      box-shadow: 0 0 0 2px rgba(143,115,92,0.08), 0 18px 28px rgba(95,79,58,0.18), var(--paper-lift);
    }}
    .day-card.special-day .day-number {{ color: #8a7278; }}
    .day-card.special-day .day-number small {{ color: #988287; }}
    .day-number {{
      font-family: var(--font-display);
      font-size: 1.68rem;
      font-weight: 660;
      letter-spacing: -0.05em;
      color: #4a4f58;
    }}
    .day-number small {{
      margin-left: 6px;
      font-family: var(--font-body);
      font-size: 0.84rem;
      font-weight: 640;
      color: #7b8089;
      vertical-align: middle;
    }}
    .day-meta {{
      color: #7a7f87;
      font-size: 0.92rem;
      font-weight: 560;
    }}
    .day-note {{
      margin-top: auto;
      min-height: 3.1em;
      padding-top: 10px;
      border-top: 1px dashed rgba(49, 53, 60, 0.12);
      font-size: 0.88rem;
      line-height: 1.45;
      color: #7a7f87;
    }}
    .day-note.empty {{ opacity: 0.26; }}
    @media (max-width: 9999px) {{
      .page-shell {{ padding: 18px 12px 32px; }}
      .calendar-header {{ flex-direction: column; align-items: flex-start; }}
      .calendar-grid {{ gap: 8px; }}
      .weekday-chip {{ min-height: 34px; font-size: 0.72rem; }}
      .day-card {{ min-height: 116px; padding: 10px; border-radius: 16px; }}
      .day-number {{ font-size: 1.1rem; }}
      .day-number small {{ display: block; margin: 4px 0 0; font-size: 0.7rem; }}
      .day-meta {{ font-size: 0.74rem; }}
      .day-note {{ min-height: 2.2em; padding-top: 8px; font-size: 0.72rem; }}
    }}
  </style>
</head>
<body>
  <main class="page-shell">
    <header class="site-header">
      <a class="brand" href="./index.html">
        <img src="./logo.png" alt="반디 로고" />
        <span>반디</span>
      </a>
      <a class="nav-link" href="./index.html">셔틀 홈</a>
    </header>
    <section class="calendar-layout">
      <section class="calendar-header">
        <div>
          <p class="eyebrow">월별 셔틀 캘린더</p>
          <h1 id="calendar-title">{escape(month_label)}</h1>
        </div>
        <div class="calendar-controls">
          <select class="month-select" id="month-select"></select>
        </div>
      </section>
      <section class="calendar-grid-shell">
        <div class="calendar-grid" id="calendar-grid"></div>
      </section>
    </section>
  </main>
  <script id="calendar-schedules" type="application/json">{schedule_json}</script>
  <script>
    const API_ENDPOINTS = {{
      config: "./api/config",
      schedules: "./api/schedules",
    }};
    const initialSchedules = JSON.parse(document.getElementById("calendar-schedules").textContent);
    let scheduleStore = initialSchedules;
    const calendarData = {{
      months: {json.dumps(months, ensure_ascii=False)},
      days: {json.dumps(days, ensure_ascii=False)},
      shuttleCounts: {json.dumps(shuttle_counts, ensure_ascii=False)},
      baseDate: {json.dumps(base_date.isoformat(), ensure_ascii=False)},
    }};
    const weekdayNames = ["일", "월", "화", "수", "목", "금", "토"];
    const monthSelect = document.getElementById("month-select");
    const calendarGrid = document.getElementById("calendar-grid");
    const calendarTitle = document.getElementById("calendar-title");
    function daysInMonth(year, month) {{
      return new Date(year, month, 0).getDate();
    }}
    function buildMonthDays(monthKey, metaByDate, countsByDate) {{
      const [yearText, monthText] = monthKey.split("-");
      const year = Number(yearText);
      const month = Number(monthText);
      const totalDays = daysInMonth(year, month);
      const days = [];
      for (let day = 1; day <= totalDays; day += 1) {{
        const dateKey = `${{yearText}}-${{String(month).padStart(2, "0")}}-${{String(day).padStart(2, "0")}}`;
        const currentDate = new Date(dateKey + "T12:00:00");
        const meta = metaByDate[dateKey] || {{}};
        days.push({{
          date: dateKey,
          isHoliday: Boolean(meta.isHoliday),
          isSundayClosed: currentDate.getDay() === 0,
          holidayName: meta.holidayName || "",
          remarks: meta.remarks || "",
        }});
      }}
      return days;
    }}
    function buildCalendarDataFromSchedules(bundle) {{
      const dateKeys = Object.keys(bundle).sort();
      if (!dateKeys.length) return null;
      const months = [];
      const seenMonths = new Set();
      const days = [];
      const shuttleCounts = {{}};
      const metaByDate = Object.fromEntries((calendarData.days || []).map((day) => [day.date, day]));
      for (const dateKey of dateKeys) {{
        const currentDate = new Date(dateKey + "T12:00:00");
        const monthKey = dateKey.slice(0, 7);
        if (!seenMonths.has(monthKey)) {{
          seenMonths.add(monthKey);
          months.push({{ key: monthKey, label: `${{currentDate.getFullYear()}}년 ${{currentDate.getMonth() + 1}}월` }});
        }}
        shuttleCounts[dateKey] = bundle[dateKey]?.totals?.pickup ?? null;
      }}
      months.forEach((month) => {{
        days.push(...buildMonthDays(month.key, metaByDate, shuttleCounts));
      }});
      return {{
        months,
        days,
        shuttleCounts,
        baseDate: dateKeys[dateKeys.length - 1],
      }};
    }}
    async function fetchBackendConfig() {{
      try {{
        const response = await window.fetch(API_ENDPOINTS.config, {{ cache: "no-store" }});
        if (!response.ok) return false;
        const payload = await response.json();
        return Boolean(payload && payload.configured);
      }} catch (_error) {{
        return false;
      }}
    }}
    async function refreshRemoteSchedules() {{
      const configured = await fetchBackendConfig();
      if (!configured) return;
      try {{
        const response = await window.fetch(API_ENDPOINTS.schedules, {{ cache: "no-store" }});
        if (!response.ok) return;
        const payload = await response.json();
        if (payload && payload.schedules && Object.keys(payload.schedules).length) {{
          scheduleStore = payload.schedules;
          const derived = buildCalendarDataFromSchedules(scheduleStore);
          if (derived) {{
            calendarData.months = derived.months;
            calendarData.days = derived.days;
            calendarData.shuttleCounts = derived.shuttleCounts;
            calendarData.baseDate = derived.baseDate;
          }}
        }}
      }} catch (_error) {{
      }}
    }}
    function availableMonthKeys() {{
      return calendarData.months.map((month) => month.key);
    }}

    function currentSearchParams() {{
      return new URLSearchParams(window.location.search);
    }}

    function resolveActiveMonth() {{
      const requested = currentSearchParams().get("month");
      if (requested && availableMonthKeys().includes(requested)) return requested;
      const baseMonth = calendarData.baseDate.slice(0, 7);
      if (availableMonthKeys().includes(baseMonth)) return baseMonth;
      return availableMonthKeys()[0] || baseMonth;
    }}

    function resolveSelectedDate(monthKey) {{
      const requested = currentSearchParams().get("date");
      const monthDays = calendarData.days.filter((day) => day.date.startsWith(monthKey));
      if (requested && monthDays.some((day) => day.date === requested)) return requested;
      return monthDays.find((day) => calendarData.shuttleCounts[day.date] != null)?.date
        || monthDays.find((day) => !day.isSundayClosed)?.date
        || monthDays[0]?.date
        || calendarData.baseDate;
    }}

    let activeMonth = resolveActiveMonth();
    let selectedDate = resolveSelectedDate(activeMonth);

    function syncCalendarUrl(replace = false) {{
      const nextUrl = new URL(window.location.href);
      nextUrl.searchParams.set("month", activeMonth);
      nextUrl.searchParams.set("date", selectedDate);
      if (replace) {{
        window.history.replaceState({{ month: activeMonth, date: selectedDate }}, "", nextUrl);
      }} else {{
        window.history.pushState({{ month: activeMonth, date: selectedDate }}, "", nextUrl);
      }}
    }}

    function renderMonthOptions() {{
      const activeMonthLabel = calendarData.months.find((month) => month.key === activeMonth)?.label || "{escape(month_label)}";
      if (calendarTitle) {{
        calendarTitle.textContent = activeMonthLabel;
      }}
      monthSelect.innerHTML = calendarData.months
        .map((month) => `<option value="${{month.key}}" ${{month.key === activeMonth ? "selected" : ""}}>${{month.label}}</option>`)
        .join("");
    }}

    function renderCalendar() {{
      const days = calendarData.days.filter((day) => day.date.startsWith(activeMonth));
      const weekdayHeader = weekdayNames
        .map((weekday, index) => `<div class="weekday-chip ${{index === 0 ? "is-sunday" : ""}}">${{weekday}}</div>`)
        .join("");
      const firstWeekday = days.length ? new Date(days[0].date + "T12:00:00").getDay() : 0;
      const leadingSlots = Array.from({{ length: firstWeekday }}, () => `<div class="day-card empty-slot" aria-hidden="true"></div>`).join("");
      const dayCards = days
        .map((day) => {{
          const currentDate = new Date(day.date + "T12:00:00");
          const weekday = weekdayNames[currentDate.getDay()];
          const isSpecial = day.isHoliday || day.isSundayClosed;
          const count = calendarData.shuttleCounts[day.date];
          const note = day.holidayName || day.remarks || "\\u00A0";
          return `
            <button
              type="button"
              class="day-card ${{day.isSundayClosed ? "closed" : ""}} ${{isSpecial ? "special-day" : ""}} ${{selectedDate === day.date ? "selected" : ""}}"
              data-date="${{day.date}}"
            >
              <span class="day-number">${{currentDate.getDate()}} <small>${{weekday}}</small></span>
              <span class="day-meta">${{count != null ? `등영 인원 ${{count}}명` : "등영 인원 -"}}</span>
              <span class="day-note ${{note.trim() ? "" : "empty"}}">${{note}}</span>
            </button>
          `;
        }})
        .join("");
      calendarGrid.innerHTML = weekdayHeader + leadingSlots + dayCards;
    }}

    monthSelect.addEventListener("change", (event) => {{
      activeMonth = event.target.value;
      const monthDays = calendarData.days.filter((day) => day.date.startsWith(activeMonth));
      selectedDate = monthDays.find((day) => calendarData.shuttleCounts[day.date] != null)?.date
        || monthDays.find((day) => !day.isSundayClosed)?.date
        || monthDays[0]?.date
        || selectedDate;
      renderCalendar();
      syncCalendarUrl();
    }});

    calendarGrid.addEventListener("click", (event) => {{
      const card = event.target.closest("[data-date]");
      if (!card) return;
      selectedDate = card.dataset.date;
      renderCalendar();
      syncCalendarUrl();
      window.location.href = `./index.html?date=${{selectedDate}}&nav=1`;
    }});

    window.addEventListener("popstate", () => {{
      activeMonth = resolveActiveMonth();
      selectedDate = resolveSelectedDate(activeMonth);
      renderMonthOptions();
      renderCalendar();
    }});

    window.addEventListener("mouseup", (event) => {{
      if (event.button === 3 && window.history.length > 1) {{
        event.preventDefault();
        window.history.back();
      }}
      if (event.button === 4) {{
        event.preventDefault();
        window.history.forward();
      }}
    }});

    async function initializeCalendar() {{
      await refreshRemoteSchedules();
      activeMonth = resolveActiveMonth();
      selectedDate = resolveSelectedDate(activeMonth);
      renderMonthOptions();
      renderCalendar();
      syncCalendarUrl(true);
    }}

    initializeCalendar();
  </script>
</body>
</html>
"""


def build_webapp(
    xlsx_path: str | Path,
    output_path: str | Path,
    *,
    admin_pin: str = DEFAULT_ADMIN_PIN,
    admin_label: str = DEFAULT_ADMIN_LABEL,
) -> Path:
    schedule_bundle, parsed = build_schedule_bundle(xlsx_path)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_html(parsed, schedule_bundle=schedule_bundle, admin_pin=admin_pin, admin_label=admin_label), encoding="utf-8")
    output.with_name("calendar.html").write_text(render_calendar_html(parsed, schedule_bundle=schedule_bundle), encoding="utf-8")
    source_candidates = []
    script_dir = Path(__file__).resolve().parent
    for root in (script_dir, script_dir.parent):
        source_candidates.extend(sorted(root.glob("*.png")))
    for source_logo in source_candidates:
        if source_logo.exists():
            shutil.copyfile(source_logo, output.with_name("logo.png"))
            break
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the shuttle dashboard web app HTML.")
    parser.add_argument("xlsx_path", help="Path to the shuttle schedule workbook.")
    parser.add_argument(
        "output_path",
        nargs="?",
        default="webapp/index.html",
        help="HTML output path. Defaults to webapp/index.html",
    )
    parser.add_argument(
        "--admin-pin",
        default=DEFAULT_ADMIN_PIN,
        help="Admin PIN for enabling edit mode. Defaults to 2468.",
    )
    parser.add_argument(
        "--admin-label",
        default=DEFAULT_ADMIN_LABEL,
        help="Admin label shown in the UI.",
    )
    args = parser.parse_args()
    output = build_webapp(args.xlsx_path, args.output_path, admin_pin=args.admin_pin, admin_label=args.admin_label)
    print(output)


if __name__ == "__main__":
    main()
