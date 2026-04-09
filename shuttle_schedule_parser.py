from __future__ import annotations

import argparse
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
HEADER_LABELS = {"성명", "성 명"}
VALID_VEHICLES = {"1호차", "2호차", "3호차", "4호차", "5호차", "7호차"}
ANY_VEHICLE_PATTERN = re.compile(r"^[1-7]호차$")
EMPTY_MARKERS = {"", "-", "x", "0"}


VEHICLE_METADATA: dict[str, dict[str, str]] = {
    "1호차": {
        "vehicle_number": "716호1749",
        "display_name": "1호차(716호1749)",
        "vehicle_type": "승합차",
        "insurance_company": "현대캐피탈",
        "insurance_phone": "1588-2114",
    },
    "2호차": {
        "vehicle_number": "76호1735",
        "display_name": "2호차(76호1735)",
        "vehicle_type": "승합차",
        "insurance_company": "현대캐피탈",
        "insurance_phone": "1588-2114",
    },
    "3호차": {
        "vehicle_number": "75라3559",
        "display_name": "3호차(75라3559)",
        "vehicle_type": "승합차",
        "insurance_company": "KB손해보험",
        "insurance_phone": "1544-0114",
    },
    "4호차": {
        "vehicle_number": "76호5003",
        "display_name": "4호차(76호5003)",
        "vehicle_type": "승합차",
        "insurance_company": "현대캐피탈",
        "insurance_phone": "1588-2114",
    },
    "5호차": {
        "vehicle_number": "219가2466",
        "display_name": "5호차(219가2466)",
        "vehicle_type": "소형승용차",
        "insurance_company": "현대해상",
        "insurance_phone": "1588-5656",
    },
    "7호차": {
        "vehicle_number": "163하3128",
        "display_name": "7호차(163하3128)",
        "vehicle_type": "소형승용차",
        "insurance_company": "BNK캐피탈",
        "insurance_phone": "1644-2254",
    },
}


@dataclass(frozen=True)
class SideConfig:
    name: str
    seq_col: str
    name_col: str
    driver_col: str
    companion_col: str
    vehicle_cell_col: str
    time_col: str
    note_col: str
    address_col: str
    count_col: str
    header_person_col: str
    body_cols: tuple[str, ...]


@dataclass(frozen=True)
class LayoutConfig:
    name: str
    left_side: SideConfig
    right_side: SideConfig
    self_title_left_col: str
    self_title_right_col: str
    total_pickup_ref: str
    total_dropoff_ref: str


LEFT_SIDE = SideConfig(
    name="pickup",
    seq_col="A",
    name_col="B",
    driver_col="C",
    companion_col="D",
    vehicle_cell_col="E",
    time_col="F",
    note_col="G",
    address_col="H",
    count_col="F",
    header_person_col="H",
    body_cols=("B", "C", "D", "E", "F", "G", "H"),
)

RIGHT_SIDE = SideConfig(
    name="dropoff",
    seq_col="K",
    name_col="L",
    driver_col="M",
    companion_col="N",
    vehicle_cell_col="O",
    time_col="P",
    note_col="Q",
    address_col="R",
    count_col="P",
    header_person_col="R",
    body_cols=("L", "M", "N", "O", "P", "Q", "R"),
)


COMPACT_LEFT_SIDE = SideConfig(
    name="pickup",
    seq_col="A",
    name_col="B",
    driver_col="F",
    companion_col="G",
    vehicle_cell_col="F",
    time_col="C",
    note_col="D",
    address_col="E",
    count_col="C",
    header_person_col="E",
    body_cols=("B", "C", "D", "E"),
)

COMPACT_RIGHT_SIDE = SideConfig(
    name="dropoff",
    seq_col="H",
    name_col="I",
    driver_col="M",
    companion_col="N",
    vehicle_cell_col="M",
    time_col="J",
    note_col="K",
    address_col="L",
    count_col="J",
    header_person_col="L",
    body_cols=("I", "J", "K", "L"),
)

WIDE_LAYOUT = LayoutConfig(
    name="wide",
    left_side=LEFT_SIDE,
    right_side=RIGHT_SIDE,
    self_title_left_col="B",
    self_title_right_col="L",
    total_pickup_ref="F146",
    total_dropoff_ref="G146",
)

COMPACT_LAYOUT = LayoutConfig(
    name="compact",
    left_side=COMPACT_LEFT_SIDE,
    right_side=COMPACT_RIGHT_SIDE,
    self_title_left_col="B",
    self_title_right_col="I",
    total_pickup_ref="C146",
    total_dropoff_ref="D146",
)


def col_to_num(col: str) -> int:
    value = 0
    for char in col:
        value = value * 26 + ord(char) - 64
    return value


def num_to_col(number: int) -> str:
    value = ""
    while number:
        number, rem = divmod(number - 1, 26)
        value = chr(65 + rem) + value
    return value


def split_ref(ref: str) -> tuple[str, int]:
    match = re.fullmatch(r"([A-Z]+)(\d+)", ref)
    if not match:
        raise ValueError(f"Invalid cell reference: {ref}")
    return match.group(1), int(match.group(2))


def expand_range(ref: str) -> list[str]:
    start_ref, end_ref = ref.split(":")
    start_col, start_row = split_ref(start_ref)
    end_col, end_row = split_ref(end_ref)
    cells: list[str] = []
    for col_idx in range(col_to_num(start_col), col_to_num(end_col) + 1):
        for row_idx in range(start_row, end_row + 1):
            cells.append(f"{num_to_col(col_idx)}{row_idx}")
    return cells


class XlsxSheet:
    def __init__(self, path: Path, *, sheet_name: str | None = None, sheet_path: str | None = None) -> None:
        self.path = path
        self.sheet_name = ""
        self.sheet_path = ""
        self.max_row = 0
        self.max_col = 0
        self.shared_strings: list[str] = []
        self.fill_ids: list[int] = []
        self.cells: dict[str, dict[str, Any]] = {}
        self.merged_parent: dict[str, str] = {}
        self._selected_sheet_name = sheet_name
        self._selected_sheet_path = sheet_path
        self._load()

    def _load(self) -> None:
        with zipfile.ZipFile(self.path) as workbook:
            self.shared_strings = self._load_shared_strings(workbook)
            self.fill_ids = self._load_fill_ids(workbook)
            self.sheet_name, self.sheet_path = self._resolve_sheet(workbook)
            sheet_root = ET.fromstring(workbook.read(self.sheet_path))
            dimension = sheet_root.find("a:dimension", NS)
            if dimension is not None:
                dim_ref = dimension.attrib["ref"]
                end_ref = dim_ref.split(":")[-1]
                end_col, end_row = split_ref(end_ref)
                self.max_col = col_to_num(end_col)
                self.max_row = end_row
            self._load_cells(sheet_root)
            self._load_merged_cells(sheet_root)

    def _load_shared_strings(self, workbook: zipfile.ZipFile) -> list[str]:
        if "xl/sharedStrings.xml" not in workbook.namelist():
            return []
        root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
        values: list[str] = []
        for item in root.findall("a:si", NS):
            values.append("".join(node.text or "" for node in item.iterfind(".//a:t", NS)))
        return values

    def _load_fill_ids(self, workbook: zipfile.ZipFile) -> list[int]:
        styles_root = ET.fromstring(workbook.read("xl/styles.xml"))
        fill_ids: list[int] = []
        for xf in styles_root.find("a:cellXfs", NS) or []:
            fill_ids.append(int(xf.attrib.get("fillId", "0")))
        return fill_ids

    def _resolve_sheet(self, workbook: zipfile.ZipFile) -> tuple[str, str]:
        sheets = workbook_sheet_refs(self.path, workbook=workbook)
        if not sheets:
            return "sheet1", "xl/worksheets/sheet1.xml"
        if self._selected_sheet_path:
            for name, target in sheets:
                if target == self._selected_sheet_path:
                    return name, target
        if self._selected_sheet_name:
            for name, target in sheets:
                if name == self._selected_sheet_name:
                    return name, target
        return sheets[0]

    def _load_sheet_name(self, workbook: zipfile.ZipFile) -> str:
        root = ET.fromstring(workbook.read("xl/workbook.xml"))
        sheet = root.find("a:sheets/a:sheet", NS)
        return sheet.attrib.get("name", "sheet1") if sheet is not None else "sheet1"

    def _cell_value(self, cell: ET.Element) -> Any:
        cell_type = cell.attrib.get("t")
        value_node = cell.find("a:v", NS)
        inline_node = cell.find("a:is", NS)
        if cell_type == "s" and value_node is not None:
            return self.shared_strings[int(value_node.text)]
        if cell_type == "inlineStr" and inline_node is not None:
            return "".join(node.text or "" for node in inline_node.iterfind(".//a:t", NS))
        if value_node is None:
            return None
        return value_node.text

    def _load_cells(self, sheet_root: ET.Element) -> None:
        for cell in sheet_root.findall(".//a:c", NS):
            ref = cell.attrib["r"]
            style_idx = int(cell.attrib.get("s", "0"))
            fill_id = self.fill_ids[style_idx] if style_idx < len(self.fill_ids) else 0
            self.cells[ref] = {
                "value": self._cell_value(cell),
                "style": style_idx,
                "fill_id": fill_id,
            }

    def _load_merged_cells(self, sheet_root: ET.Element) -> None:
        merged = sheet_root.find("a:mergeCells", NS)
        if merged is None:
            return
        for region in merged:
            ref = region.attrib["ref"]
            start_ref = ref.split(":")[0]
            for cell_ref in expand_range(ref):
                self.merged_parent[cell_ref] = start_ref

    def cell(self, ref: str) -> dict[str, Any]:
        return self.cells.get(ref, {"value": None, "style": None, "fill_id": 0})

    def value(self, ref: str, *, merged: bool = True) -> Any:
        cell = self.cell(ref)
        if cell["value"] is not None:
            return cell["value"]
        if merged and ref in self.merged_parent:
            return self.cell(self.merged_parent[ref])["value"]
        return None

    def fill_id(self, ref: str) -> int:
        return int(self.cell(ref)["fill_id"])


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_optional(value: Any) -> str | None:
    text = normalize_text(value)
    return text or None


def normalize_person(value: Any) -> str | None:
    text = normalize_text(value)
    if not text or text.lower() in EMPTY_MARKERS:
        return None
    return text


def parse_int(value: Any) -> int | None:
    text = normalize_text(value)
    return int(text) if text.isdigit() else None


def parse_order(value: Any) -> list[str]:
    text = normalize_text(value)
    if not text:
        return []
    if ":" in text:
        text = text.split(":", 1)[1]
    if "(" in text:
        text = text.split("(", 1)[0]
    return re.findall(r"[1-7](?=(?:호차)?(?:\s|ㅡ|$))", text)


def parse_minutes(value: Any) -> list[int]:
    text = normalize_text(value)
    if not text or "순서" in text or "호차" in text:
        return []
    return [int(match) for match in re.findall(r"\((\d{1,2})\)", text)]


def workbook_sheet_refs(path: Path, *, workbook: zipfile.ZipFile | None = None) -> list[tuple[str, str]]:
    def _build_refs(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
        workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
        rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relationships = {
            node.attrib["Id"]: node.attrib["Target"]
            for node in rels_root
            if node.attrib.get("Type", "").endswith("/worksheet")
        }
        refs: list[tuple[str, str]] = []
        for sheet in workbook_root.findall("a:sheets/a:sheet", NS):
            rel_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            target = relationships.get(rel_id or "")
            if not target:
                continue
            refs.append((sheet.attrib.get("name", "sheet1"), f"xl/{target.lstrip('/')}"))
        return refs

    if workbook is not None:
        return _build_refs(workbook)
    with zipfile.ZipFile(path) as archive:
        return _build_refs(archive)


def detect_layout(sheet: XlsxSheet) -> LayoutConfig:
    if normalize_text(sheet.value("I5", merged=False)) in HEADER_LABELS or sheet.max_col <= col_to_num("N"):
        return COMPACT_LAYOUT
    return WIDE_LAYOUT


def is_header_row(sheet: XlsxSheet, row: int, side: SideConfig) -> bool:
    return normalize_text(sheet.value(f"{side.name_col}{row}")) in HEADER_LABELS


def dominant_body_fill(sheet: XlsxSheet, row: int, side: SideConfig) -> int:
    fills = [sheet.fill_id(f"{side.name_col}{row}")]
    fills.extend(sheet.fill_id(f"{col}{row}") for col in (side.time_col, side.note_col, side.address_col))
    for fill_id in fills:
        if fill_id != 0:
            return fill_id
    return 0


def build_record(sheet: XlsxSheet, row: int, side: SideConfig, round_fill_id: int) -> dict[str, Any] | None:
    sequence = parse_int(sheet.value(f"{side.seq_col}{row}", merged=False))
    if sequence is None:
        return None

    name = normalize_text(sheet.value(f"{side.name_col}{row}"))
    driver = normalize_text(sheet.value(f"{side.driver_col}{row}"))
    companion = normalize_text(sheet.value(f"{side.companion_col}{row}"))
    time_value = normalize_text(sheet.value(f"{side.time_col}{row}"))
    note = normalize_text(sheet.value(f"{side.note_col}{row}"))
    address = normalize_text(sheet.value(f"{side.address_col}{row}"))

    if not any([name, driver, companion, time_value, note, address]):
        return None

    emphasis_columns: list[str] = []
    for col in side.body_cols:
        fill_id = sheet.fill_id(f"{col}{row}")
        if fill_id and fill_id not in {round_fill_id, 4, 9, 10}:
            emphasis_columns.append(col)

    return {
        "row": row,
        "sequence": sequence,
        "name": normalize_optional(name),
        "driver": normalize_person(driver),
        "companion": normalize_person(companion),
        "time": normalize_optional(time_value),
        "note": normalize_optional(note),
        "address": normalize_optional(address),
        "absent": time_value == "결석",
        "emphasis": bool(emphasis_columns),
        "emphasis_columns": emphasis_columns,
    }


def parse_rounds(sheet: XlsxSheet, start_row: int, end_row: int, side: SideConfig) -> list[dict[str, Any]]:
    rounds: list[dict[str, Any]] = []
    current_round: dict[str, Any] | None = None
    previous_fill: int | None = None

    for row in range(start_row, end_row + 1):
        if is_header_row(sheet, row, side):
            previous_fill = None
            current_round = None
            continue

        fill_id = dominant_body_fill(sheet, row, side)
        record = build_record(sheet, row, side, fill_id)
        if record is None:
            continue

        if current_round is None or previous_fill != fill_id:
            current_round = {
                "round": len(rounds) + 1,
                "fill_id": fill_id,
                "entries": [],
            }
            rounds.append(current_round)

        current_round["entries"].append(record)
        previous_fill = fill_id

    return rounds


def parse_assignment(sheet: XlsxSheet, header_row: int, side: SideConfig) -> dict[str, str | None]:
    companion_label = normalize_text(sheet.value(f"{side.note_col}{header_row + 1}", merged=False))
    companion_round_match = re.search(r"\((\d+)차\)", companion_label)
    return {
        "driver": normalize_person(sheet.value(f"{side.header_person_col}{header_row}")),
        "companion": normalize_person(sheet.value(f"{side.header_person_col}{header_row + 1}")),
        "companion_round": int(companion_round_match.group(1)) if companion_round_match else None,
    }


def pick_primary_entry(rounds: list[dict[str, Any]]) -> dict[str, Any] | None:
    entries = [entry for round_data in rounds for entry in round_data["entries"]]
    if not entries:
        return None
    return next((entry for entry in entries if not entry["absent"]), entries[0])


def simplify_address(address: str | None) -> str | None:
    if not address:
        return None

    tokens = [token for token in re.split(r"\s+", address.strip()) if token]
    if not tokens:
        return None

    district = next((token for token in tokens if token.endswith("구")), None)
    candidates = [
        token
        for token in tokens
        if token != "용인시" and (token.endswith(("동", "읍", "면", "리", "가")) or "마을" in token)
    ]
    neighborhood = next((token for token in candidates if token != district), None)

    if district and neighborhood:
        return f"{district}-{neighborhood}"
    if neighborhood:
        return neighborhood

    fallback = [token for token in tokens if token != "용인시"]
    return fallback[0] if fallback else tokens[0]


def build_home_card(vehicle: dict[str, Any], side: str) -> dict[str, Any]:
    rounds = vehicle[f"{side}_rounds"]
    primary_entry = pick_primary_entry(rounds)
    assignment = vehicle[f"{side}_assignment"]

    return {
        "vehicle_name": vehicle["vehicle_name"],
        "display_name": vehicle["display_name"],
        "vehicle_number": vehicle["vehicle_number"],
        "vehicle_type": vehicle["vehicle_type"],
        "insurance_company": vehicle["insurance_company"],
        "insurance_phone": vehicle["insurance_phone"],
        "driver": assignment["driver"],
        "companion": assignment["companion"],
        "companion_round": assignment.get("companion_round"),
        "count": vehicle[f"{side}_count"],
        "first_time": primary_entry["time"] if primary_entry else None,
        "first_name": primary_entry["name"] if primary_entry else None,
        "first_address": primary_entry["address"] if primary_entry else None,
        "first_address_short": simplify_address(primary_entry["address"]) if primary_entry else None,
        "schedule_rounds": rounds,
    }


def parse_vehicle_block(sheet: XlsxSheet, vehicle_name: str, header_row: int, end_row: int, layout: LayoutConfig) -> dict[str, Any]:
    metadata = VEHICLE_METADATA[vehicle_name]
    pickup_rounds = parse_rounds(sheet, header_row + 2, end_row, layout.left_side)
    dropoff_rounds = parse_rounds(sheet, header_row + 2, end_row, layout.right_side)
    pickup_assignment = parse_assignment(sheet, header_row, layout.left_side)
    dropoff_assignment = parse_assignment(sheet, header_row, layout.right_side)

    return {
        "vehicle_name": vehicle_name,
        "display_name": metadata["display_name"],
        "vehicle_number": metadata["vehicle_number"],
        "vehicle_type": metadata["vehicle_type"],
        "insurance_company": metadata["insurance_company"],
        "insurance_phone": metadata["insurance_phone"],
        "pickup_count": parse_int(sheet.value(f"{layout.left_side.count_col}{header_row}", merged=False)),
        "dropoff_count": parse_int(sheet.value(f"{layout.right_side.count_col}{header_row}", merged=False)),
        "pickup_assignment": pickup_assignment,
        "dropoff_assignment": dropoff_assignment,
        "pickup_rounds": pickup_rounds,
        "dropoff_rounds": dropoff_rounds,
    }


def parse_named_table(sheet: XlsxSheet, title: str, title_row: int, side: SideConfig, end_row: int) -> dict[str, Any]:
    return {
        "title": title,
        "entries": [
            record
            for row in range(title_row + 3, end_row + 1)
            if (record := build_record(sheet, row, side, dominant_body_fill(sheet, row, side))) is not None
        ],
    }


def parse_long_term_absences(sheet: XlsxSheet, side: SideConfig, header_row: int | None) -> list[dict[str, Any]]:
    if header_row is None:
        header_row = 134
    entries: list[dict[str, Any]] = []
    for row in range(header_row + 2, sheet.max_row + 1):
        record = build_record(sheet, row, side, dominant_body_fill(sheet, row, side))
        if record is not None:
            entries.append(record)
    return entries


def find_title_row(sheet: XlsxSheet, left_title: str, right_title: str, left_col: str, right_col: str) -> int | None:
    for row in range(1, sheet.max_row + 1):
        if normalize_text(sheet.value(f"{left_col}{row}", merged=False)) == left_title and normalize_text(sheet.value(f"{right_col}{row}", merged=False)) == right_title:
            return row
    return None


def find_long_term_header_row(sheet: XlsxSheet, side: SideConfig, start_row: int) -> int | None:
    for row in range(start_row + 10, sheet.max_row + 1):
        name_label = normalize_text(sheet.value(f"{side.name_col}{row}", merged=False)).replace(" ", "")
        address_label = normalize_text(sheet.value(f"{side.address_col}{row}", merged=False)).replace(" ", "")
        if name_label == "성명" and address_label == "주소":
            return row
    return None


def find_totals(sheet: XlsxSheet, layout: LayoutConfig) -> dict[str, int | None]:
    for row in range(sheet.max_row, max(sheet.max_row - 25, 0), -1):
        label = normalize_text(sheet.value(f"E{row}", merged=False)).replace(" ", "")
        if label == "등영인원":
            return {
                "pickup": parse_int(sheet.value(f"F{row}", merged=False)),
                "dropoff": parse_int(sheet.value(f"G{row}", merged=False)),
            }
    return {
        "pickup": parse_int(sheet.value(layout.total_pickup_ref, merged=False)),
        "dropoff": parse_int(sheet.value(layout.total_dropoff_ref, merged=False)),
    }


def parse_schedule(path: str | Path, *, sheet_name: str | None = None, sheet_path: str | None = None) -> dict[str, Any]:
    workbook = XlsxSheet(Path(path), sheet_name=sheet_name, sheet_path=sheet_path)
    layout = detect_layout(workbook)
    all_vehicle_headers: list[tuple[int, str]] = []

    for row in range(1, workbook.max_row + 1):
        vehicle_name = normalize_text(workbook.value(f"B{row}", merged=False))
        if ANY_VEHICLE_PATTERN.match(vehicle_name):
            all_vehicle_headers.append((row, vehicle_name))

    self_title_row = find_title_row(workbook, "자가등영", "자가송영", layout.self_title_left_col, layout.self_title_right_col) or 120
    long_term_header_row = find_long_term_header_row(workbook, layout.right_side, self_title_row) or 134
    vehicles: list[dict[str, Any]] = []
    for index, (row, vehicle_name) in enumerate(all_vehicle_headers):
        if vehicle_name not in VALID_VEHICLES:
            continue
        next_row = all_vehicle_headers[index + 1][0] if index + 1 < len(all_vehicle_headers) else self_title_row
        vehicles.append(parse_vehicle_block(workbook, vehicle_name, row, next_row - 1, layout))

    totals = find_totals(workbook, layout)
    computed_pickup_total = sum(
        1
        for vehicle in vehicles
        for round_data in vehicle["pickup_rounds"]
        for entry in round_data["entries"]
        if not entry["absent"]
    )
    computed_dropoff_total = sum(
        1
        for vehicle in vehicles
        for round_data in vehicle["dropoff_rounds"]
        for entry in round_data["entries"]
        if not entry["absent"]
    )

    parsed = {
        "source_file": str(Path(path)),
        "sheet_name": workbook.sheet_name,
        "source_sheet_path": workbook.sheet_path,
        "layout_name": layout.name,
        "operation_order": parse_order(workbook.value(f"{layout.left_side.note_col}1")),
        "dropoff_departure_minutes": parse_minutes(workbook.value(f"{layout.left_side.note_col}2")),
        "dropoff_departure_base_time": (
            normalize_text(workbook.value(f"{layout.right_side.time_col}{all_vehicle_headers[0][0] + 1}"))
            if all_vehicle_headers
            and re.fullmatch(
                r"\d{1,2}:\d{2}",
                normalize_text(workbook.value(f"{layout.right_side.time_col}{all_vehicle_headers[0][0] + 1}")),
            )
            else None
        ),
        "vehicles": vehicles,
        "self_pickup": parse_named_table(workbook, "자가등영", self_title_row, layout.left_side, min(long_term_header_row - 1, workbook.max_row)),
        "self_dropoff": parse_named_table(workbook, "자가송영", self_title_row, layout.right_side, min(long_term_header_row - 1, workbook.max_row)),
        "long_term_absences": parse_long_term_absences(workbook, layout.right_side, long_term_header_row),
        "totals": totals,
    }
    if parsed["totals"]["pickup"] is None:
        parsed["totals"]["pickup"] = computed_pickup_total + sum(1 for entry in parsed["self_pickup"]["entries"] if not entry["absent"])
    if parsed["totals"]["dropoff"] is None:
        parsed["totals"]["dropoff"] = computed_dropoff_total + sum(1 for entry in parsed["self_dropoff"]["entries"] if not entry["absent"])
    parsed["home"] = {
        "pickup_total_label": f"등영 인원 {parsed['totals']['pickup']}명" if parsed["totals"]["pickup"] is not None else None,
        "pickup_cards": [build_home_card(vehicle, "pickup") for vehicle in parsed["vehicles"]],
        "dropoff_order_cards": [
            {
                "vehicle_name": f"{vehicle_number}호차",
                "minute": minute,
            }
            for vehicle_number, minute in zip(parsed["operation_order"], parsed["dropoff_departure_minutes"])
        ],
        "dropoff_cards": [build_home_card(vehicle, "dropoff") for vehicle in parsed["vehicles"]],
        "self_pickup_count": len(parsed["self_pickup"]["entries"]),
        "self_dropoff_count": len(parsed["self_dropoff"]["entries"]),
    }
    return parsed


def parse_schedule_workbook(path: str | Path) -> list[dict[str, Any]]:
    workbook_path = Path(path)
    parsed_sheets: list[dict[str, Any]] = []
    for sheet_name, sheet_path in workbook_sheet_refs(workbook_path):
        if "등송영표" not in sheet_name:
            continue
        parsed_sheets.append(parse_schedule(workbook_path, sheet_name=sheet_name, sheet_path=sheet_path))
    return parsed_sheets


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse shuttle schedule XLSX into JSON.")
    parser.add_argument("xlsx_path", help="Path to the schedule workbook.")
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    args = parser.parse_args()
    parsed = parse_schedule(args.xlsx_path)
    if args.pretty:
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(parsed, ensure_ascii=False))


if __name__ == "__main__":
    main()
