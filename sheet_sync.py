"""
Two-way sync between the app and an Excel workbook in OneDrive / SharePoint
(Microsoft 365), using Microsoft Graph.

* Sign-in: Microsoft "device code" sign-in (open microsoft.com/devicelogin,
  type the code). The login is remembered in ms_token_cache.json.
* The worksheet (e.g. CARD_DETAILS) keeps its own column layout. An extra
  "App ID" column links every row to its vendor in the app, so a vendor is
  never added twice.
* Sync rules (run daily and with "Sync now"):
    - App vendors without a row in the sheet  -> appended to the sheet
    - Vendor changed in the app                -> its sheet row is rewritten
    - Linked row changed in the sheet          -> vendor updated in the app
    - Changed in both places                   -> app version kept (reported)
    - Row typed in the sheet (no App ID)       -> matched with the duplicate
      rules; merged into the existing vendor or added as a new vendor; the
      App ID is then written into the row
    - Deletions are NOT copied either way (reported instead).
"""

import base64
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from dedupe import Matcher, merge_records
from importer import _cell_text, guess_target, row_to_vendor

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Files.ReadWrite.All", "User.Read"]

APP_ID_HEADER = "App ID"
SERIAL_KEYS = {"srno", "sno", "serialno", "slno", "no", "sr", "serial"}
LIST_FIELDS = ("phone_numbers", "emails", "destinations_covered", "websites")
WRITE_FIELDS = ("company", "person_name", "designation", "phone_numbers", "emails",
                "destinations_covered", "websites", "address", "services_notes")


class SyncError(Exception):
    """Something the user has to fix (shown on the sync page)."""


class NotSignedIn(SyncError):
    pass


# ------------------------------------------------------------- sign-in

class MicrosoftAuth:
    """Microsoft 365 sign-in with a remembered login (token cache file)."""

    def __init__(self, client_id, tenant_id, cache_path):
        self.client_id = (client_id or "").strip()
        self.tenant_id = (tenant_id or "").strip() or "organizations"
        self.cache_path = Path(cache_path)
        self.flow_state = {"state": "idle", "message": ""}
        self._lock = threading.Lock()

    def _app(self):
        try:
            import msal
        except ImportError:
            raise SyncError(
                "The Microsoft sign-in library is not installed. In the VS Code "
                "terminal run:  pip install msal  and restart the app."
            )
        if not self.client_id:
            raise SyncError("Enter the Application (client) ID in the setup section first.")
        cache = msal.SerializableTokenCache()
        if self.cache_path.exists():
            cache.deserialize(self.cache_path.read_text(encoding="utf-8"))
        app = msal.PublicClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            token_cache=cache,
        )
        return app, cache

    def _save(self, cache):
        if cache.has_state_changed:
            self.cache_path.write_text(cache.serialize(), encoding="utf-8")

    def account(self):
        """Signed-in user's email, or None."""
        try:
            app, _ = self._app()
        except SyncError:
            return None
        accounts = app.get_accounts()
        return accounts[0].get("username") if accounts else None

    def token(self):
        app, cache = self._app()
        accounts = app.get_accounts()
        if not accounts:
            raise NotSignedIn("Not signed in to Microsoft 365. Click 'Sign in'.")
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        self._save(cache)
        if not result or "access_token" not in result:
            raise NotSignedIn("The Microsoft sign-in has expired. Click 'Sign in' again.")
        return result["access_token"]

    def start_sign_in(self):
        """Begin device-code sign-in. Returns (code, url). Completes in the
        background once the user has entered the code."""
        app, cache = self._app()
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise SyncError(f"Could not start sign-in: {flow.get('error_description') or flow}")
        self.flow_state = {"state": "pending", "message": "Waiting for you to sign in…",
                           "code": flow["user_code"], "url": flow["verification_uri"]}

        def finish():
            result = app.acquire_token_by_device_flow(flow)
            with self._lock:
                if "access_token" in result:
                    self._save(cache)
                    self.flow_state = {"state": "done", "message": "Signed in."}
                else:
                    self.flow_state = {"state": "error",
                                       "message": result.get("error_description", "Sign-in failed.")}

        threading.Thread(target=finish, daemon=True).start()
        return flow["user_code"], flow["verification_uri"]

    def sign_out(self):
        if self.cache_path.exists():
            self.cache_path.unlink()
        self.flow_state = {"state": "idle", "message": ""}


# ------------------------------------------------- Excel Online access

def _col_letter(index):
    """0 -> A, 25 -> Z, 26 -> AA"""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _col_index(letters):
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


class ExcelOnlineSheet:
    """One worksheet of a workbook in OneDrive / SharePoint."""

    def __init__(self, auth, file_url, worksheet, cached_ids=None, save_ids=None):
        self.auth = auth
        self.file_url = (file_url or "").strip()
        self.worksheet = (worksheet or "").strip()
        self.ids = cached_ids or {}
        self.save_ids = save_ids or (lambda ids: None)
        if not self.file_url:
            raise SyncError("Enter the link to the Excel file in the setup section first.")
        if not self.worksheet:
            raise SyncError("Enter the worksheet (tab) name in the setup section first.")

    # -- HTTP
    def _request(self, method, path, body=None, retries=4):
        import requests
        url = path if path.startswith("http") else GRAPH + path
        for attempt in range(retries):
            response = requests.request(
                method, url, json=body, timeout=60,
                headers={"Authorization": f"Bearer {self.auth.token()}",
                         "Content-Type": "application/json"},
            )
            if response.status_code in (429, 503, 504) and attempt < retries - 1:
                time.sleep(int(response.headers.get("Retry-After", 3 * (attempt + 1))))
                continue
            if response.status_code >= 400:
                try:
                    detail = response.json().get("error", {}).get("message", response.text)
                except ValueError:
                    detail = response.text
                if response.status_code in (401, 403):
                    raise SyncError(f"Microsoft refused access ({response.status_code}): {detail}. "
                                    "Check that the signed-in account can edit the file.")
                if response.status_code == 404:
                    raise SyncError(f"Not found: {detail}. Check the file link and worksheet name.")
                raise SyncError(f"Microsoft Graph error {response.status_code}: {detail}")
            return response.json() if response.content else {}
        raise SyncError("Microsoft Graph did not respond. Try again later.")

    # -- find the file
    def _resolve(self):
        if self.ids.get("url") == self.file_url and self.ids.get("drive") and self.ids.get("item"):
            return self.ids["drive"], self.ids["item"]

        drive = item = None
        # 1) Works for "Copy link" sharing links
        share = "u!" + base64.urlsafe_b64encode(self.file_url.encode()).decode().rstrip("=")
        try:
            found = self._request("GET", f"/shares/{share}/driveItem?$select=id,name,parentReference")
            drive, item = found["parentReference"]["driveId"], found["id"]
        except SyncError:
            pass

        # 2) Works for the browser address of the file (…/_layouts/15/Doc.aspx?sourcedoc={GUID}&file=…)
        if not item:
            parsed = urlparse(self.file_url)
            query = parse_qs(parsed.query)
            guid = (query.get("sourcedoc", [""])[0]).strip("{}").lower()
            name = unquote(query.get("file", [""])[0])
            # e.g. /:x:/r/personal/operations_khannatravels_com/_layouts/15/Doc.aspx
            match = re.search(r"(/(?:personal|sites|teams)/[^/?]+)", parsed.path)
            if not (match and (guid or name)):
                raise SyncError("Could not understand the file link. Open the file in the browser "
                                "and copy the address from the address bar, or use Share > Copy link.")
            site = self._request("GET", f"/sites/{parsed.hostname}:{match.group(1)}?$select=id")
            drive_info = self._request("GET", f"/sites/{site['id']}/drive?$select=id")
            drive = drive_info["id"]
            query_name = quote(name.replace("'", "''")) if name else "xlsx"
            results = self._request(
                "GET", f"/drives/{drive}/root/search(q='{query_name}')?$select=id,name,sharepointIds")
            for entry in results.get("value", []):
                unique = (entry.get("sharepointIds") or {}).get("listItemUniqueId", "").lower()
                if (guid and unique == guid) or (not guid and entry.get("name") == name):
                    item = entry["id"]
                    break
            if not item:
                raise SyncError(f"Could not find '{name or 'the file'}' in that OneDrive. "
                                "Check that the signed-in account has access to it.")

        self.ids = {"url": self.file_url, "drive": drive, "item": item}
        self.save_ids(self.ids)
        return drive, item

    def _sheet_path(self):
        drive, item = self._resolve()
        name = quote(self.worksheet.replace("'", "''"))
        return f"/drives/{drive}/items/{item}/workbook/worksheets('{name}')"

    # -- read / write
    def read(self):
        """Return (first_row_number, rows) where rows[0] is the header row."""
        data = self._request("GET", self._sheet_path() + "/usedRange(valuesOnly=true)?$select=address,values")
        address = data.get("address", "")
        match = re.search(r"!?\$?([A-Z]+)\$?(\d+)", address.split("!")[-1])
        first_col = _col_index(match.group(1)) if match else 0
        first_row = int(match.group(2)) if match else 1
        rows = [[_cell_text(v) for v in row] for row in data.get("values", [])]
        if first_col:
            rows = [[""] * first_col + r for r in rows]
        if first_row > 1:      # header must be row 1 for this layout
            rows = [[] for _ in range(first_row - 1)] + rows
            first_row = 1
        return first_row, rows

    def write(self, row_number, first_col, rows, text_cols=()):
        """Write a block of rows starting at (row_number, first_col)."""
        if not rows:
            return
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        start = _col_letter(first_col)
        end = _col_letter(first_col + width - 1)
        address = f"{start}{row_number}:{end}{row_number + len(rows) - 1}"
        formats = [["@" if (first_col + c) in text_cols else "General" for c in range(width)]
                   for _ in rows]
        self._request("PATCH", self._sheet_path() + f"/range(address='{address}')",
                      {"values": rows, "numberFormat": formats})

    def copy_header_style(self, from_col, to_col):
        """Make a new header cell look like its neighbour (best effort)."""
        try:
            base = self._sheet_path()
            src, dst = f"{_col_letter(from_col)}1", f"{_col_letter(to_col)}1"
            fill = self._request("GET", base + f"/range(address='{src}')/format/fill")
            font = self._request("GET", base + f"/range(address='{src}')/format/font")
            if fill.get("color"):
                self._request("PATCH", base + f"/range(address='{dst}')/format/fill", {"color": fill["color"]})
            self._request("PATCH", base + f"/range(address='{dst}')/format/font",
                          {k: font[k] for k in ("bold", "color", "name", "size") if k in font})
            self._request("PATCH", base + f"/range(address='{dst}')/format",
                          {"horizontalAlignment": "Center", "verticalAlignment": "Center",
                           "columnWidth": 60})
        except SyncError:
            pass


# ------------------------------------------------------------- sync

def _norm(value):
    return " ".join(str(value or "").split())


def _hash(cells):
    return hashlib.sha1(json.dumps(sorted(cells.items()), ensure_ascii=False).encode()).hexdigest()


class Layout:
    """Which column holds what in the worksheet."""

    def __init__(self, headers):
        self.headers = [_norm(h) for h in headers]
        keys = [re.sub(r"[^a-z0-9]", "", h.lower()) for h in self.headers]
        self.app_col = keys.index("appid") if "appid" in keys else None
        self.serial_col = next((i for i, k in enumerate(keys) if k in SERIAL_KEYS), None)
        self.mapping = {}
        for i, header in enumerate(self.headers):
            if i in (self.app_col, self.serial_col) or not header:
                continue
            self.mapping[i] = guess_target(header)
        self.field_col = {}
        for i, target in self.mapping.items():
            if target and target not in self.field_col:
                self.field_col[target] = i
        if not any(t in self.field_col for t in ("company", "person_name", "first_name", "emails")):
            raise SyncError("The worksheet's first row doesn't look like column headers "
                            "(expected e.g. Company, Person Name, Email).")

    @property
    def width(self):
        return max(len(self.headers), (self.app_col or 0) + 1)

    def cells_from_vendor(self, vendor):
        """{column index: text} for the columns the app fills in."""
        cells = {}
        for field in WRITE_FIELDS:
            value = vendor.get(field)
            text = ", ".join(value) if isinstance(value, list) else _norm(value)
            if field == "person_name" and "person_name" not in self.field_col:
                parts = (vendor.get("person_name") or "").split()
                if "first_name" in self.field_col:
                    cells[self.field_col["first_name"]] = parts[0] if parts else ""
                if "last_name" in self.field_col:
                    cells[self.field_col["last_name"]] = " ".join(parts[1:])
                continue
            if field in self.field_col:
                cells[self.field_col[field]] = text
        return cells

    def cells_from_row(self, row):
        """Same columns as cells_from_vendor, read from a sheet row."""
        cols = set(self.cells_from_vendor({}).keys())
        return {c: _norm(row[c]) if c < len(row) else "" for c in cols}

    def vendor_from_row(self, row):
        return row_to_vendor(self.headers, row, self.mapping)


def run_sync(sheet, store, log=print):
    """
    Synchronise. `store` gives access to the app's data:
      store.vendors()                  -> list of vendor dicts (with "id")
      store.insert(vendor)             -> new id   (source = Excel sheet)
      store.update(vendor_id, vendor)  -> save changed fields
      store.links()                    -> {vendor_id: {"app_hash", "sheet_hash", "deleted"}}
      store.set_link(vendor_id, app_hash, sheet_hash)
      store.mark_removed(vendor_id)    -> its row was deleted from the sheet
    Returns a report dict.
    """
    report = {"to_sheet_added": 0, "to_sheet_updated": 0, "to_app_added": 0,
              "to_app_updated": 0, "to_app_merged": 0, "conflicts": 0,
              "details": [], "warnings": []}

    def note(text):
        report["details"].append(text)
        log(text)

    first_row, rows = sheet.read()
    if not rows or not any(rows[0]):
        raise SyncError("The worksheet is empty - it needs a header row (Company, Person Name, …).")
    headers = rows[0]
    layout = Layout(headers)

    # Add the "App ID" column if it is missing
    if layout.app_col is None:
        last_header = max(i for i, h in enumerate(layout.headers) if h)
        layout.app_col = last_header + 1
        while len(layout.headers) <= layout.app_col:
            layout.headers.append("")
        layout.headers[layout.app_col] = APP_ID_HEADER
        sheet.write(1, layout.app_col, [[APP_ID_HEADER]])
        sheet.copy_header_style(layout.app_col - 1, layout.app_col)
        note(f"Added the '{APP_ID_HEADER}' column ({_col_letter(layout.app_col)}) to link rows to the app.")

    text_cols = set(layout.cells_from_vendor({}).keys())
    vendors = {v["id"]: v for v in store.vendors()}
    links = store.links()
    width = layout.width

    data_rows = []
    for offset, row in enumerate(rows[1:]):
        row = list(row) + [""] * (width - len(row))
        number = first_row + 1 + offset
        raw_id = _norm(row[layout.app_col])
        has_data = any(_norm(c) for i, c in enumerate(row)
                       if i not in (layout.app_col, layout.serial_col))
        data_rows.append((number, row, raw_id, has_data))

    def write_row(number, row, vendor, vendor_id_text):
        cells = layout.cells_from_vendor(vendor)
        new_row = list(row)
        for col, text in cells.items():
            new_row[col] = text
        new_row[layout.app_col] = vendor_id_text
        sheet.write(number, 0, [new_row[:width]], text_cols)
        return cells

    seen = {}            # vendor id -> sheet row number
    row_of = {}          # vendor id -> (row number, row values) for linked rows
    refresh = set()      # vendors changed by a duplicate row -> rewrite their row
    unlinked = []

    # 1. Rows that already carry an App ID
    for number, row, raw_id, has_data in data_rows:
        if not raw_id:
            if has_data:
                unlinked.append((number, row))
            continue
        if raw_id.lower().startswith(("duplicate", "deleted")):
            continue
        try:
            vendor_id = int(float(raw_id))
        except ValueError:
            report["warnings"].append(f"Row {number}: '{raw_id}' in {APP_ID_HEADER} is not a number - skipped.")
            continue

        if vendor_id in seen:
            sheet.write(number, layout.app_col, [[f"Duplicate of #{vendor_id}"]])
            report["warnings"].append(f"Row {number} repeats vendor #{vendor_id} (also row {seen[vendor_id]}) - "
                                      "marked as duplicate; you can delete it.")
            continue
        seen[vendor_id] = number
        row_of[vendor_id] = (number, row)
        link = links.get(vendor_id)

        if vendor_id not in vendors:
            sheet.write(number, layout.app_col, [[f"Deleted in app (#{vendor_id})"]])
            report["warnings"].append(f"Row {number}: vendor #{vendor_id} was deleted in the app - the row "
                                      f"was kept and marked 'Deleted in app'; delete it in the sheet if not needed.")
            continue

        vendor = vendors[vendor_id]
        app_cells = layout.cells_from_vendor(vendor)
        sheet_cells = layout.cells_from_row(row)
        app_hash, sheet_hash = _hash(app_cells), _hash(sheet_cells)

        if link is None:
            # App ID present but never synced (e.g. typed by hand): app wins
            app_changed, sheet_changed = app_hash != sheet_hash, False
        else:
            app_changed = app_hash != link["app_hash"]
            sheet_changed = sheet_hash != link["sheet_hash"]

        if app_changed:
            written = write_row(number, row, vendor, str(vendor_id))
            store.set_link(vendor_id, app_hash, _hash(written))
            report["to_sheet_updated"] += 1
            if sheet_changed:
                report["conflicts"] += 1
                note(f"Row {number} ({vendor.get('company') or vendor_id}) was changed in both places - "
                     "the app's version was kept.")
            else:
                note(f"Row {number}: updated from the app ({vendor.get('company') or vendor_id}).")
        elif sheet_changed:
            from_sheet, problem = layout.vendor_from_row(row)
            if problem:
                report["warnings"].append(f"Row {number}: {problem} - not copied to the app.")
                continue
            updated = dict(vendor)
            for field in WRITE_FIELDS:
                if field in layout.field_col or (field == "person_name" and "first_name" in layout.field_col):
                    updated[field] = from_sheet.get(field, [] if field in LIST_FIELDS else "")
            store.update(vendor_id, updated)
            vendors[vendor_id] = {**vendor, **updated}
            store.set_link(vendor_id, _hash(layout.cells_from_vendor(updated)), sheet_hash)
            report["to_app_updated"] += 1
            note(f"Row {number}: changes copied into the app ({updated.get('company') or vendor_id}).")
        elif link is None:
            store.set_link(vendor_id, app_hash, sheet_hash)

    # 2. Rows typed into the sheet (no App ID yet)
    matcher = Matcher()
    for v in vendors.values():
        matcher.add(v, v["id"])
    for number, row in unlinked:
        from_sheet, problem = layout.vendor_from_row(row)
        if problem:
            report["warnings"].append(f"Row {number}: {problem} - skipped.")
            continue
        entry, reason = matcher.find(from_sheet)
        if entry:
            vendor_id = entry["ref"]
            merged = merge_records(vendors[vendor_id], from_sheet, mode="fill")
            merged_full = {**vendors[vendor_id], **merged}
            store.update(vendor_id, merged_full)
            vendors[vendor_id] = merged_full
            matcher.update(entry, merged_full)
            if vendor_id in seen:
                sheet.write(number, layout.app_col, [[f"Duplicate of #{vendor_id}"]])
                if vendor_id in row_of:
                    refresh.add(vendor_id)
                report["to_app_merged"] += 1
                report["warnings"].append(
                    f"Row {number} is the same vendor as row {seen[vendor_id]} ({reason}) - its details were "
                    f"merged into vendor #{vendor_id} and the row was marked as duplicate; you can delete it.")
                continue
            written = write_row(number, row, merged_full, str(vendor_id))
            store.set_link(vendor_id, _hash(layout.cells_from_vendor(merged_full)), _hash(written))
            seen[vendor_id] = number
            report["to_app_merged"] += 1
            note(f"Row {number}: matched existing vendor #{vendor_id} ({reason}) and linked.")
        else:
            vendor_id = store.insert(from_sheet)
            from_sheet = {**from_sheet, "id": vendor_id}
            vendors[vendor_id] = from_sheet
            matcher.add(from_sheet, vendor_id)
            sheet.write(number, layout.app_col, [[str(vendor_id)]])
            store.set_link(vendor_id, _hash(layout.cells_from_vendor(from_sheet)),
                          _hash(layout.cells_from_row(row)))
            seen[vendor_id] = number
            report["to_app_added"] += 1
            note(f"Row {number}: new vendor added to the app ({from_sheet.get('company') or vendor_id}).")

    # Rows whose vendor gained details from a duplicate row: rewrite them now
    for vendor_id in sorted(refresh):
        number, row = row_of[vendor_id]
        written = write_row(number, row, vendors[vendor_id], str(vendor_id))
        store.set_link(vendor_id, _hash(layout.cells_from_vendor(vendors[vendor_id])), _hash(written))
        report["to_sheet_updated"] += 1
        note(f"Row {number}: updated with the details merged from its duplicate row.")

    # 3. Vendors that are not in the sheet yet -> append
    last_row = first_row + len(rows) - 1
    serials = []
    if layout.serial_col is not None:
        for _, row, _, _ in data_rows:
            try:
                serials.append(int(float(row[layout.serial_col])))
            except (ValueError, TypeError):
                pass
    next_serial = (max(serials) if serials else len([r for r in data_rows if r[3]])) + 1

    new_rows, new_ids = [], []
    for vendor_id in sorted(vendors):
        if vendor_id in seen:
            continue
        link = links.get(vendor_id)
        if link:
            if not link.get("removed"):
                store.mark_removed(vendor_id)
                report["warnings"].append(
                    f"Vendor #{vendor_id} ({vendors[vendor_id].get('company')}) was removed from the sheet - "
                    "not added again. Delete it in the app too if it is no longer needed.")
            continue
        row = [""] * width
        for col, text in layout.cells_from_vendor(vendors[vendor_id]).items():
            row[col] = text
        if layout.serial_col is not None:
            row[layout.serial_col] = next_serial
            next_serial += 1
        row[layout.app_col] = str(vendor_id)
        new_rows.append(row)
        new_ids.append(vendor_id)

    if new_rows:
        sheet.write(last_row + 1, 0, new_rows, text_cols)
        for vendor_id, row in zip(new_ids, new_rows):
            cells = layout.cells_from_vendor(vendors[vendor_id])
            store.set_link(vendor_id, _hash(cells), _hash(layout.cells_from_row(row)))
        report["to_sheet_added"] = len(new_rows)
        note(f"{len(new_rows)} vendor(s) added to the sheet (rows {last_row + 1}-{last_row + len(new_rows)}).")

    return report
