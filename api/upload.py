from __future__ import annotations

import cgi
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_shuttle_webapp import DEFAULT_ADMIN_PIN, derive_base_date
from shuttle_schedule_parser import parse_schedule_workbook


SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "")
ADMIN_PIN_HASH = os.getenv("ADMIN_PIN_HASH", hashlib.sha256(DEFAULT_ADMIN_PIN.encode("utf-8")).hexdigest())


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def supabase_request(path: str, *, method: str = "GET", payload: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(
        f"{SUPABASE_URL}{path}",
        data=payload,
        method=method,
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def storage_path_for(month_key: str, filename: str) -> str:
    safe_name = re.sub(r"[^0-9A-Za-z._-]+", "_", filename).strip("._") or "workbook.xlsx"
    return f"monthly/{month_key}/{int(time.time())}_{safe_name}"


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        try:
            if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and SUPABASE_BUCKET):
                return json_response(self, 503, {"error": "Supabase environment variables are not configured"})
            admin_hash = (self.headers.get("x-bandi-admin-hash") or "").strip()
            if not admin_hash or admin_hash != ADMIN_PIN_HASH:
                return json_response(self, 403, {"error": "관리자 로그인 후 업로드할 수 있습니다."})

            content_type = self.headers.get("content-type", "")
            if "multipart/form-data" not in content_type:
                return json_response(self, 400, {"error": "multipart/form-data upload is required"})

            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                    "CONTENT_LENGTH": self.headers.get("content-length", "0"),
                },
            )
            workbook = form["workbook"] if "workbook" in form else None
            if workbook is None or not getattr(workbook, "file", None):
                return json_response(self, 400, {"error": "workbook file is required"})

            file_name = Path(getattr(workbook, "filename", "") or "schedule.xlsx").name
            file_bytes = workbook.file.read()
            if not file_bytes:
                return json_response(self, 400, {"error": "uploaded workbook is empty"})

            uploaded_by = (form.getfirst("uploaded_by") or "").strip() or None

            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as temp_file:
                    temp_file.write(file_bytes)
                    temp_path = Path(temp_file.name)

                parsed_sheets = parse_schedule_workbook(temp_path)
                if not parsed_sheets:
                    return json_response(self, 400, {"error": "등송영표 탭을 찾지 못했습니다."})

                schedule_row_map: dict[str, dict] = {}
                month_keys: set[str] = set()
                latest_date = None
                duplicate_dates: list[str] = []
                for parsed in parsed_sheets:
                    date_key = derive_base_date(parsed).isoformat()
                    month_key = date_key[:7]
                    month_keys.add(month_key)
                    latest_date = max(latest_date, date_key) if latest_date else date_key
                    if date_key in schedule_row_map:
                        duplicate_dates.append(date_key)
                    schedule_row_map[date_key] = {
                        "date_key": date_key,
                        "month_key": month_key,
                        "sheet_name": parsed.get("sheet_name"),
                        "source_file_name": file_name,
                        "schedule_json": parsed,
                    }

                schedule_rows = [schedule_row_map[key] for key in sorted(schedule_row_map)]

                primary_month_key = sorted(month_keys)[-1]
                upload_storage_path = storage_path_for(primary_month_key, file_name)
                encoded_storage_path = "/".join(urllib.parse.quote(part, safe="") for part in upload_storage_path.split("/"))

                status, body = supabase_request(
                    f"/storage/v1/object/{SUPABASE_BUCKET}/{encoded_storage_path}",
                    method="POST",
                    payload=file_bytes,
                    headers={
                        "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "x-upsert": "true",
                    },
                )
                if status >= 400:
                    return json_response(self, status, {"error": f"Storage upload failed: {body.decode('utf-8', 'ignore')}"})

                status, body = supabase_request(
                    "/rest/v1/schedule_uploads",
                    method="POST",
                    payload=json.dumps(
                        [
                            {
                                "file_name": file_name,
                                "storage_path": upload_storage_path,
                                "month_key": primary_month_key,
                                "uploaded_by": uploaded_by,
                            }
                        ],
                        ensure_ascii=False,
                    ).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Prefer": "return=representation",
                    },
                )
                if status >= 400:
                    return json_response(self, status, {"error": f"Upload log save failed: {body.decode('utf-8', 'ignore')}"})
                upload_rows = json.loads(body.decode("utf-8") or "[]")
                upload_id = upload_rows[0]["id"] if upload_rows else None

                for month_key in month_keys:
                    status, body = supabase_request(
                        f"/rest/v1/schedule_days?month_key=eq.{urllib.parse.quote(month_key, safe='')}",
                        method="DELETE",
                        headers={"Prefer": "return=minimal"},
                    )
                    if status >= 400:
                        return json_response(self, status, {"error": f"Existing month cleanup failed: {body.decode('utf-8', 'ignore')}"})

                for row in schedule_rows:
                    row["source_upload_id"] = upload_id

                status, body = supabase_request(
                    "/rest/v1/schedule_days?on_conflict=date_key",
                    method="POST",
                    payload=json.dumps(schedule_rows, ensure_ascii=False).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Prefer": "resolution=merge-duplicates,return=representation",
                    },
                )
                if status >= 400:
                    return json_response(self, status, {"error": f"Schedule day save failed: {body.decode('utf-8', 'ignore')}"})

                return json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "month_key": primary_month_key,
                        "latest_date": latest_date,
                        "updated_dates": [row["date_key"] for row in schedule_rows],
                        "source_file_name": file_name,
                        "collapsed_duplicate_dates": sorted(set(duplicate_dates)),
                    },
                )
            finally:
                if temp_path and temp_path.exists():
                    temp_path.unlink(missing_ok=True)
        except Exception as error:
            return json_response(
                self,
                500,
                {
                    "error": f"{type(error).__name__}: {error}",
                    "traceback": traceback.format_exc(limit=6),
                },
            )

    def do_GET(self) -> None:
        json_response(self, 405, {"error": "Method not allowed"})
