"""
delivery-app FastAPI backend
───────────────────────────
✓ Barcode / manual scan → Google Sheet (+ Shopify lookup)
✓ Re-usable Google Sheet rows & data-validation
✓ Driver-fee calculation + Payout roll-up
✓ Order & payout queries for the mobile / web app
✓ Status update incl. “Returned” handling
✓ Daily archive of yesterday’s rows

Déployé sur Render via the Dockerfile you created earlier.
"""
from dotenv import load_dotenv
load_dotenv()
import base64, json, os
import datetime as dt
from typing import List, Optional
from datetime import timezone          
import requests
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, Request, Form
from cachetools import TTLCache
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware
import gspread
from google.oauth2.service_account import Credentials

# ---   Google secret handling  ---------------------------------
cred_b64 = os.getenv("GOOGLE_CREDENTIALS_B64", "")
if not cred_b64:
    raise RuntimeError("Missing GOOGLE_CREDENTIALS_B64 env-var")

cred_json      = base64.b64decode(cred_b64).decode("utf-8")
creds_dict     = json.loads(cred_json)
SCOPES         = ["https://www.googleapis.com/auth/spreadsheets"]
credentials    = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc             = gspread.authorize(credentials)

spreadsheet_id = os.getenv("SPREADSHEET_ID")
if not spreadsheet_id:
    raise RuntimeError("Missing SPREADSHEET_ID env-var")
ss = gc.open_by_key(spreadsheet_id)

# ───────────────────────────────────────────────────────────────
# CONFIGURATION  ––––– edit via env-vars in Render dashboard
# ───────────────────────────────────────────────────────────────

SHOPIFY_STORES = [
    {
        "name": "irrakids",
        "api_key": os.getenv("IRRAKIDS_API_KEY", ""),
        "password": os.getenv("IRRAKIDS_PASSWORD", ""),
        "domain": "nouralibas.myshopify.com",
    },
    {
        "name": "irranova",
        "api_key": os.getenv("IRRANOVA_API_KEY", ""),
        "password": os.getenv("IRRANOVA_PASSWORD", ""),
        "domain": "fdd92b-2e.myshopify.com",
    },
]

# Sheet configuration (default names can be overridden via env vars)
SHEET_NAME = os.getenv("SHEET_NAME")
DELIVERY_GUY_NAME = os.getenv("DELIVERY_GUY_NAME", "delivery")

DELIVERY_STATUSES   = [
    "Dispatched", "Livré", "En cours",
    "Pas de réponse 1", "Pas de réponse 2", "Pas de réponse 3",
    "Annulé", "Refusé", "Rescheduled", "Returned"
]
COMPLETED_STATUSES  = ["Livré", "Annulé", "Refusé", "Returned"]
NORMAL_DELIVERY_FEE = 20
EXCHANGE_DELIVERY_FEE = 10

# Employee log configuration
EMPLOYEE_TAB = os.getenv("EMPLOYEE_TAB", "Employee_Log")


# Cache for opened worksheets to avoid repeated API calls
sheet_cache = TTLCache(maxsize=32, ttl=300)



def _get_or_create_sheet(sheet_name: str, header: List[str]) -> gspread.Worksheet:
    # return cached worksheet if available
    ws = sheet_cache.get(sheet_name)
    if ws is None:
        try:
            ws = ss.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            ws = ss.add_worksheet(title=sheet_name, rows="1", cols=str(len(header)))
            ws.append_row(header)
        sheet_cache[sheet_name] = ws

    existing_header = ws.row_values(1)
    if existing_header != header:
        # extend or update header to match expected columns
        for idx, val in enumerate(header, start=1):
            if idx > len(existing_header) or existing_header[idx-1] != val:
                ws.update_cell(1, idx, val)
        # refresh cache entry after header update
        sheet_cache[sheet_name] = ws

    return ws


# ───────────────────────────────────────────────────────────────
# Pydantic models
# ───────────────────────────────────────────────────────────────

class ScanIn(BaseModel):
    barcode: str

class ScanResult(BaseModel):
    result: str
    order: str
    tag: str = ""
    deliveryStatus: str = "Dispatched"


class StatusUpdate(BaseModel):
    order_name: str
    new_status: Optional[str] = None   # one of DELIVERY_STATUSES
    note: Optional[str] = None
    cash_amount: Optional[float] = None
    scheduled_time: Optional[str] = None
    comm_log: Optional[str] = None


class ManualAdd(BaseModel):
    order_name: str


class EmployeeLog(BaseModel):
    employee: str
    order: Optional[str] = None
    amount: Optional[float] = None


# ───────────────────────────────────────────────────────────────
# FastAPI init + CORS (mobile apps & localhost dev)
# ───────────────────────────────────────────────────────────────
app = FastAPI(title="Delivery FastAPI backend")

# ✅ Define the path correctly
static_path = os.path.join(os.path.dirname(__file__), "static")

# ✅ Mount the /static directory
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
# 👇 ADD THIS EXACTLY BELOW

DRIVERS = {
    "abderrehman": {
        "sheet_id": spreadsheet_id,
        "order_tab": "abderrehman_Orders",
        "payouts_tab": "abderrehman_Payouts"
    },
    "anouar": {
        "sheet_id": spreadsheet_id,
        "order_tab": "anouar_Orders",
        "payouts_tab": "anouar_Payouts"
    },
    "mohammed": {
        "sheet_id": spreadsheet_id,
        "order_tab": "mohammed_Orders",
        "payouts_tab": "mohammed_Payouts"
    },
    "nizar": {
        "sheet_id": spreadsheet_id,
        "order_tab": "nizar_Orders",
        "payouts_tab": "nizar_Payouts"
    },
}

# Simple admin password (override via env var)
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# In-memory caches for orders and payouts
# shorter TTLs for near real-time updates
orders_cache = TTLCache(maxsize=8, ttl=60)
payouts_cache = TTLCache(maxsize=8, ttl=60)
orders_data_cache = TTLCache(maxsize=8, ttl=60)

@app.get("/", response_class=HTMLResponse)
async def show_login():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


@app.get("/admin", response_class=HTMLResponse)
async def show_admin_login():
    return FileResponse(os.path.join(STATIC_DIR, "admin_login.html"))

@app.post("/login", response_class=HTMLResponse)
async def login(driver_id: str = Form(...)):
    if driver_id in DRIVERS:
        response = RedirectResponse(url=f"/static/index.html?driver={driver_id}", status_code=302)
        return response
    return HTMLResponse("<h2>Invalid driver ID</h2>", status_code=401)


@app.post("/admin/login")
async def admin_login(password: str = Form(...)):
    if password == ADMIN_PASSWORD:
        return {"success": True}
    raise HTTPException(status_code=401, detail="Invalid admin password")

@app.get("/drivers")
def list_drivers():
    """Return list of driver IDs."""
    return list(DRIVERS.keys())


# Allow cross-origin requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ───────────────────────────────────────────────────────────────
# Utility functions – exact ports of your Apps Script logic
# ───────────────────────────────────────────────────────────────
def calculate_driver_fee(tags: str) -> int:
    return EXCHANGE_DELIVERY_FEE if "ch" in (tags or "").lower() else NORMAL_DELIVERY_FEE


def get_primary_display_tag(tags: str) -> str:
    l = (tags or "").lower()
    if "big" in l:
        return "big"
    if "k" in l:
        return "k"
    if "12livery" in l:
        return "12livery"
    if "12livrey" in l:
        return "12livrey"
    if "fast" in l:
        return "fast"
    if "oscario" in l:
        return "oscario"
    if "sand" in l:
        return "sand"
    return ""


def safe_float(val):
    """Return a float or 0.0 for falsy/non-numeric values."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def get_cell(row, idx, default=""):
    """Safely access a cell from a row, providing a default if missing."""
    return row[idx] if idx < len(row) else default


def parse_timestamp(val: str) -> dt.datetime:
    """Parse timestamp strings with optional microseconds and timezone."""
    val = (val or "").strip()
    parts = val.split()
    if len(parts) > 2 and parts[-1].isalpha():
        val = " ".join(parts[:-1])
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(val, fmt)
        except ValueError:
            continue
    return dt.datetime.fromisoformat(val)


def get_order_from_store(order_name: str, store_cfg: dict) -> Optional[dict]:
    """Call Shopify Admin API by order name (#1234)."""
    auth = (store_cfg["api_key"], store_cfg["password"])
    url = f"https://{store_cfg['domain']}/admin/api/2023-07/orders.json"
    params = {"name": order_name}
    r = requests.get(url, auth=auth, params=params, timeout=10)
    try:
        r.raise_for_status()
    except requests.HTTPError:
        return None
    data = r.json()
    return data.get("orders", [{}])[0] if data.get("orders") else None


# ───────────────────────────────────────────────────────────────
# Core functions – Sheets logic
# ───────────────────────────────────────────────────────────────
ORDER_HEADER = [
    "Timestamp", "Order Name", "Customer Name", "Customer Phone",
    "Address", "Tags", "Fulfillment", "Order Status",
    "Store", "Delivery Status", "Notes", "Scheduled Time", "Scan Date",
    "Cash Amount", "Driver Fee", "Payout ID", "Status Log", "Comm Log"
]

PAYOUT_HEADER = [
    "Payout ID", "Date Created", "Orders", "Total Cash",
    "Total Fees", "Total Payout", "Status", "Date Paid"
]

EMPLOYEE_HEADER = [
    "Timestamp", "Employee", "Order Number", "Amount"
]


def order_exists(ws: gspread.Worksheet, order_name: str) -> bool:
    col = ws.col_values(2)  # column B
    return order_name in col


def get_order_row(ws: gspread.Worksheet, order_name: str) -> Optional[List]:
    data = ws.get_all_values()
    for row in data[1:]:
        if row[1] == order_name:
            return row
    return None


def add_to_payout(ws_orders: gspread.Worksheet, payout_ws: gspread.Worksheet,
                  order_name: str, cash_amount: float, driver_fee: float) -> str:
    """Create (or extend) an open payout row and write payout ID back to orders sheet."""
    data = payout_ws.get_all_values()
    open_row_idx = None
    open_payout_id = None
    # search from bottom up
    for idx in range(len(data) - 1, 0, -1):
        if data[idx][6].lower() != "paid":      # status column
            open_row_idx = idx
            open_payout_id = data[idx][0]
            break

    if open_row_idx is None:
        # create new payout
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payout_id = f"PO-{dt.datetime.now().strftime('%Y%m%d-%H%M')}"
        new_row = [
            payout_id, now, order_name,
            cash_amount, driver_fee,
            cash_amount - driver_fee,
            "pending", ""
        ]
        payout_ws.append_row(new_row)
    else:
        # update existing payout line
        orders_cell = payout_ws.cell(open_row_idx + 1, 3)  # 1-based API
        cash_cell = payout_ws.cell(open_row_idx + 1, 4)
        fee_cell = payout_ws.cell(open_row_idx + 1, 5)
        payout_cell = payout_ws.cell(open_row_idx + 1, 6)

        orders_cell.value = f"{orders_cell.value}, {order_name}" if orders_cell.value else order_name
        cash_total = float(cash_cell.value or 0) + cash_amount
        fee_total = float(fee_cell.value or 0) + driver_fee
        payout_total = cash_total - fee_total

        cash_cell.value = cash_total
        fee_cell.value = fee_total
        payout_cell.value = payout_total
        payout_ws.update_cells([orders_cell, cash_cell, fee_cell, payout_cell])

        payout_id = open_payout_id

    # write back to orders sheet
    order_cells = ws_orders.findall(order_name)
    if order_cells:
        row_idx = order_cells[0].row
        ws_orders.update_cell(row_idx, 16, payout_id)

    return payout_id


def remove_from_payout(ws_orders: gspread.Worksheet, payout_ws: gspread.Worksheet,
                       payout_id: str, order_name: str,
                       cash_amount: float, driver_fee: float) -> None:
    """Remove an order from an existing payout line."""
    if not payout_id:
        return
    cells = payout_ws.findall(payout_id)
    if not cells:
        return
    row_idx = cells[0].row
    row = payout_ws.row_values(row_idx)

    orders_list = [o.strip() for o in (row[2] or "").split(',') if o.strip()]
    if order_name not in orders_list:
        return
    orders_list.remove(order_name)

    cash_total = safe_float(get_cell(row, 3)) - cash_amount
    fee_total = safe_float(get_cell(row, 4)) - driver_fee
    payout_total = cash_total - fee_total

    payout_ws.update("C{}:F{}".format(row_idx, row_idx),
                     [[", ".join(orders_list), cash_total, fee_total, payout_total]])

    # clear payout ID from order row
    order_cells = ws_orders.findall(order_name)
    if order_cells:
        ws_orders.update_cell(order_cells[0].row, 16, "")


# ───────────────────────────────────────────────────────────────
# FastAPI ROUTES
# ───────────────────────────────────────────────────────────────
def _tabs_for(driver_id: str):
    cfg = DRIVERS.get(driver_id)
    if not cfg:
        raise HTTPException(status_code=400, detail="Invalid driver")
    return (
        _get_or_create_sheet(cfg["order_tab"],  ORDER_HEADER),
        _get_or_create_sheet(cfg["payouts_tab"], PAYOUT_HEADER)
    )

@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "time": dt.datetime.utcnow().isoformat()}

# -------------------------------  SCAN  -------------------------------
@app.post("/scan", response_model=ScanResult, tags=["orders"])
def scan(
    payload: ScanIn,
    driver: str = Query(..., description="driver1 / driver2 / …")
):
    ws_orders, _ = _tabs_for(driver)
    barcode = payload.barcode.strip()
    order_number = "#" + "".join(filter(str.isdigit, barcode))

    if len(order_number) <= 1:
        raise HTTPException(status_code=400, detail="Invalid barcode")

    # already scanned?
    if order_exists(ws_orders, order_number):
        existing = get_order_row(ws_orders, order_number)
        return ScanResult(
            result="⚠️ Already scanned",
            order=order_number,
            tag=get_primary_display_tag(existing[5]),
            deliveryStatus=existing[9],
        )

    # --- Shopify look-up (unchanged) ----------------------------------
    window_start = dt.datetime.now(timezone.utc) - dt.timedelta(days=50)
    chosen_order, chosen_store_name = None, ""
    for store in SHOPIFY_STORES:
        order = get_order_from_store(order_number, store)
        if order:
            created_at = dt.datetime.fromisoformat(
                order["created_at"].replace("Z", "+00:00")
            )
            if created_at >= window_start and (
                not chosen_order
                or created_at > dt.datetime.fromisoformat(
                    chosen_order["created_at"].replace("Z", "+00:00")
                )
            ):
                chosen_order, chosen_store_name = order, store["name"]

    # --- sheet append (same logic, but to the driver tab) -------------
    tags = chosen_order.get("tags", "") if chosen_order else ""
    fulfillment = chosen_order.get("fulfillment_status", "unfulfilled") if chosen_order else ""
    order_status = "closed" if (chosen_order and chosen_order.get("cancelled_at")) else "open"
    customer_name = phone = address = ""
    cash_amount = 0.0
    result_msg = "❌ Not found"

    if chosen_order:
        result_msg = (
            "⚠️ Cancelled" if chosen_order.get("cancelled_at")
            else "❌ Unfulfilled" if fulfillment != "fulfilled"
            else "✅ OK"
        )
        cash_amount = float(chosen_order.get("total_outstanding") or chosen_order.get("total_price") or 0)
        if chosen_order.get("shipping_address"):
            sa = chosen_order["shipping_address"]
            customer_name = sa.get("name", "")
            phone = sa.get("phone", "") or chosen_order.get("phone", "")
            address = ", ".join(filter(None, [
                sa.get("address1"), sa.get("address2"),
                sa.get("city"), sa.get("province")
            ]))

    now_ts   = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scan_day = dt.datetime.now().strftime("%Y-%m-%d")
    driver_fee = calculate_driver_fee(tags)

    ws_orders.append_row([
        now_ts, order_number, customer_name, phone, address, tags, fulfillment,
        order_status, chosen_store_name, "Dispatched", "", "", scan_day,
        cash_amount, driver_fee, ""
    ])

    # invalidate caches for this driver
    orders_cache.pop(driver, None)
    payouts_cache.pop(driver, None)
    orders_data_cache.pop(driver, None)

    return ScanResult(
        result=result_msg,
        order=order_number,
        tag=get_primary_display_tag(tags),
        deliveryStatus="Dispatched",
    )

# -----------------------------  ORDERS  -------------------------------
@app.get("/orders", tags=["orders"])
def list_active_orders(driver: str = Query(...)):
    if driver in orders_cache:
        return orders_cache[driver]

    data = orders_data_cache.get(driver)
    if data is None:
        ws_orders, _ = _tabs_for(driver)
        data = ws_orders.get_all_values()
        orders_data_cache[driver] = data
    data = data[1:]  # skip header
    active = []
    for r in data:
        if not r or r[9] in COMPLETED_STATUSES:
            continue
        active.append({
            "timestamp":    r[0],
            "orderName":    r[1],
            "customerName": r[2],
            "customerPhone":r[3],
            "address":      r[4],
            "tags":         r[5],
            "deliveryStatus": r[9] or "Dispatched",
            "notes":        r[10],
            "scheduledTime": r[11],
            "scanDate":     r[12],
            "cashAmount":   safe_float(get_cell(r, 13)),
            "driverFee":    safe_float(get_cell(r, 14)),
            "payoutId":     r[15],
            "statusLog":    get_cell(r, 16),
            "commLog":      get_cell(r, 17),
        })
    def sort_key(o):
        if o["scheduledTime"]:
            try:
                return parse_timestamp(o["scheduledTime"])
            except Exception:
                pass
        return parse_timestamp(o["timestamp"])

    active.sort(key=sort_key)

    now = dt.datetime.now()
    for o in active:
        if o["scheduledTime"]:
            try:
                st = parse_timestamp(o["scheduledTime"])
                o["urgent"] = (st - now).total_seconds() <= 3600
            except Exception:
                o["urgent"] = False
        else:
            o["urgent"] = False

    orders_cache[driver] = active
    return active

@app.put("/order/status", tags=["orders"])
def update_order_status(
    payload: StatusUpdate,
    bg: BackgroundTasks,
    driver: str = Query(...)
):
    if payload.new_status and payload.new_status not in DELIVERY_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status")

    ws_orders, ws_payouts = _tabs_for(driver)
    cells = ws_orders.findall(payload.order_name)
    if not cells:
        raise HTTPException(status_code=404, detail="Order not found")

    row = cells[0].row
    row_vals = ws_orders.row_values(row)

    # ensure row has at least 18 columns
    if len(row_vals) < len(ORDER_HEADER):
        row_vals += [""] * (len(ORDER_HEADER) - len(row_vals))

    if payload.new_status:
        ws_orders.update_cell(row, 10, payload.new_status)
        ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status_cell = ws_orders.cell(row, 17)
        status_cell.value = ((status_cell.value or "") + f" | {payload.new_status} @ {ts}").strip(" |")
        ws_orders.update_cells([status_cell])
    if payload.note is not None:
        ws_orders.update_cell(row, 11, payload.note)
    if payload.scheduled_time is not None:
        ws_orders.update_cell(row, 12, payload.scheduled_time)
    if payload.cash_amount is not None:
        ws_orders.update_cell(row, 14, payload.cash_amount)
    if payload.comm_log is not None:
        ws_orders.update_cell(row, 18, payload.comm_log)

    # add or remove from payout depending on status change
    if payload.new_status == "Livré" and row_vals[9] != "Livré":
        driver_fee = calculate_driver_fee(row_vals[5])
        cash_amt = payload.cash_amount or safe_float(get_cell(row_vals, 13))
        add_to_payout(ws_orders, ws_payouts,
                      payload.order_name, cash_amt, driver_fee)
    elif payload.new_status and payload.new_status != "Livré" and row_vals[9] == "Livré":
        driver_fee = safe_float(get_cell(row_vals, 14))
        cash_amt = payload.cash_amount if payload.cash_amount is not None else safe_float(get_cell(row_vals, 13))
        payout_id = get_cell(row_vals, 15)
        remove_from_payout(ws_orders, ws_payouts, payout_id,
                           payload.order_name, cash_amt, driver_fee)

    # clean up list if returned
    if payload.new_status == "Returned":
        pass

    # invalidate caches for this driver
    orders_cache.pop(driver, None)
    payouts_cache.pop(driver, None)
    orders_data_cache.pop(driver, None)

    return {"success": True}

# ----------------------------  PAYOUTS  -------------------------------
@app.get("/payouts", tags=["payouts"])
def get_payouts(driver: str = Query(...)):
    if driver in payouts_cache:
        return payouts_cache[driver]

    ws_orders, ws_payouts = _tabs_for(driver)
    # Fetch orders sheet once and build a lookup dictionary
    orders_data = orders_data_cache.get(driver)
    if orders_data is None:
        orders_data = ws_orders.get_all_values()
        orders_data_cache[driver] = orders_data
    order_lookup = {row[1]: row for row in orders_data[1:]}

    rows = ws_payouts.get_all_values()[1:]
    payouts = []
    for r in reversed(rows):
        orders_list = [o.strip() for o in (r[2] or "").split(',') if o.strip()]
        order_details = []
        for name in orders_list:
            row = order_lookup.get(name)
            if row:
                order_details.append({
                    "name": name,
                    "cashAmount": safe_float(get_cell(row, 13)),
                    "driverFee": safe_float(get_cell(row, 14))
                })
            else:
                order_details.append({"name": name, "cashAmount": 0.0, "driverFee": 0.0})

        payouts.append({
            "payoutId":   r[0],
            "dateCreated":r[1],
            "orders":     r[2],
            "totalCash":  float(r[3] or 0),
            "totalFees":  float(r[4] or 0),
            "totalPayout":float(r[5] or 0),
            "status":     r[6] or "pending",
            "datePaid":   r[7],
            "orderDetails": order_details
        })

    payouts_cache[driver] = payouts
    return payouts

@app.post("/payout/mark-paid/{payout_id}", tags=["payouts"])
def mark_payout_paid(payout_id: str, driver: str = Query(...)):
    _, ws_payouts = _tabs_for(driver)
    cells = ws_payouts.findall(payout_id)
    if not cells:
        raise HTTPException(status_code=404, detail="Payout not found")

    row = cells[0].row
    ws_payouts.update_cell(row, 7, "paid")
    ws_payouts.update_cell(row, 8, dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # invalidate caches for this driver
    payouts_cache.pop(driver, None)
    orders_cache.pop(driver, None)
    orders_data_cache.pop(driver, None)

    return {"success": True}


# ----------------------------  STATS  -------------------------------
def _compute_stats(
    driver: str,
    days: int | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    data = orders_data_cache.get(driver)
    if data is None:
        ws_orders, _ = _tabs_for(driver)
        data = ws_orders.get_all_values()
        orders_data_cache[driver] = data
    rows = data[1:]

    if start:
        try:
            start_date = dt.datetime.strptime(start, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid start date")
    elif days and days > 0:
        start_date = dt.datetime.now().date() - dt.timedelta(days=days - 1)
    else:
        start_date = None

    if end:
        try:
            end_date = dt.datetime.strptime(end, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid end date")
    else:
        end_date = None

    total = delivered = returned = 0
    collect = fees = canceled_amount = 0.0
    for r in rows:
        scan_day = r[12]
        try:
            sd = dt.datetime.strptime(scan_day, "%Y-%m-%d").date()
        except Exception:
            sd = None
        if start_date and (not sd or sd < start_date):
            continue
        if end_date and (not sd or sd > end_date):
            continue
        total += 1
        status = r[9]
        cash = safe_float(get_cell(r, 13))
        fee = safe_float(get_cell(r, 14))
        if status == "Livré":
            delivered += 1
            collect += cash
            fees += fee
        elif status in ("Returned", "Annulé", "Refusé"):
            returned += 1
            canceled_amount += cash

    rate = (delivered / total * 100) if total else 0
    return {
        "totalOrders": total,
        "delivered": delivered,
        "returned": returned,
        "totalCollect": collect,
        "totalFees": fees,
        "deliveryRate": rate,
        "canceledAmount": canceled_amount,
    }


@app.get("/stats", tags=["stats"])
def get_stats(
    driver: str = Query(...),
    days: int | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    return _compute_stats(driver, days, start, end)


@app.get("/admin/stats", tags=["admin"])
def admin_stats(
    days: int | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    return {d: _compute_stats(d, days, start, end) for d in DRIVERS.keys()}


# -------------------------------------------------------------------
# Daily trend data for all drivers
# -------------------------------------------------------------------
@app.get("/admin/trends", tags=["admin"])
def admin_trends(
    start: str | None = Query(None),
    end: str | None = Query(None),
    days: int | None = Query(None),
):
    """Return delivered count per day across all drivers."""
    if start:
        try:
            start_date = dt.datetime.strptime(start, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid start date")
    elif days and days > 0:
        start_date = dt.datetime.now().date() - dt.timedelta(days=days - 1)
    else:
        start_date = None

    if end:
        try:
            end_date = dt.datetime.strptime(end, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid end date")
    else:
        end_date = dt.datetime.now().date()

    counts: dict[dt.date, int] = {}
    for driver in DRIVERS.keys():
        data = orders_data_cache.get(driver)
        if data is None:
            ws_orders, _ = _tabs_for(driver)
            data = ws_orders.get_all_values()
            orders_data_cache[driver] = data
        rows = data[1:]
        for r in rows:
            scan_day = get_cell(r, 12)
            status = get_cell(r, 9)
            try:
                sd = dt.datetime.strptime(scan_day, "%Y-%m-%d").date()
            except Exception:
                continue
            if start_date and sd < start_date:
                continue
            if end_date and sd > end_date:
                continue
            if status == "Livré":
                counts[sd] = counts.get(sd, 0) + 1

    days_sorted = sorted(counts.keys())
    return [
        {"date": d.strftime("%Y-%m-%d"), "delivered": counts[d]}
        for d in days_sorted
    ]


@app.get("/admin/search", tags=["admin"])
def admin_search(q: str = Query(...)):
    """Search orders across all drivers by order name or phone."""
    q_lower = q.lower()
    results: list[dict] = []
    for driver in DRIVERS.keys():
        data = orders_data_cache.get(driver)
        if data is None:
            ws_orders, _ = _tabs_for(driver)
            data = ws_orders.get_all_values()
            orders_data_cache[driver] = data
        rows = data[1:]
        for r in rows:
            order_name = get_cell(r, 1)
            phone = get_cell(r, 3)
            if q_lower in str(order_name).lower() or q_lower in str(phone).lower():
                results.append({
                    "driver": driver,
                    "orderName": order_name,
                    "customerName": get_cell(r, 2),
                    "customerPhone": phone,
                    "deliveryStatus": get_cell(r, 9) or "Dispatched",
                    "cashAmount": safe_float(get_cell(r, 13)),
                    "address": get_cell(r, 4),
                })
    return results


# ---------------------------- EMPLOYEES -------------------------------
@app.post("/employee/log", tags=["employees"])
def employee_log(entry: EmployeeLog):
    """Append an employee action row to the configured sheet."""
    ws = _get_or_create_sheet(EMPLOYEE_TAB, EMPLOYEE_HEADER)
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ws.append_row([ts, entry.employee, entry.order or "", entry.amount or ""])
    return {"success": True}


@app.get("/employee/logs", tags=["employees"])
def employee_logs():
    """Return all employee log rows as a list of dictionaries."""
    ws = _get_or_create_sheet(EMPLOYEE_TAB, EMPLOYEE_HEADER)
    rows = ws.get_all_values()[1:]
    logs = []
    for r in rows:
        logs.append({
            "timestamp": get_cell(r, 0),
            "employee": get_cell(r, 1),
            "order": get_cell(r, 2),
            "amount": safe_float(get_cell(r, 3)) if get_cell(r, 3) else None,
        })
    return logs


@app.post("/archive-yesterday", tags=["maintenance"])
def archive_yesterday():
    ws = _get_or_create_sheet(SHEET_NAME, ORDER_HEADER)
    data = ws.get_all_values()
    if len(data) <= 1:
        return {"archived": 0}

    header = data[0]
    today = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    archive_rows = []
    keep_rows = [header]

    for row in data[1:]:
        ts_str = row[0]
        try:
            ts = parse_timestamp(ts_str)
        except ValueError:
            keep_rows.append(row)
            continue
        if ts < today:
            archive_rows.append(row)
        else:
            keep_rows.append(row)

    if archive_rows:
        y_date = (today - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        archive_name = f"{DELIVERY_GUY_NAME}_Archive_{y_date}"
        archive_ws = _get_or_create_sheet(archive_name, header)
        archive_ws.append_rows(archive_rows)
        ws.clear()
        ws.append_rows(keep_rows)
    return {"archived": len(archive_rows)}
