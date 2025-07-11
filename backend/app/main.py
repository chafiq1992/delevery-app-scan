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
import os
import datetime as dt
from typing import List, Optional
from datetime import timezone
import requests
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Form
from cachetools import TTLCache
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from .db import get_session, Driver, Order, Payout, EmployeeLog

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


DELIVERY_STATUSES   = [
    "Dispatched", "Livré", "En cours",
    "Pas de réponse 1", "Pas de réponse 2", "Pas de réponse 3",
    "Annulé", "Refusé", "Rescheduled", "Returned"
]
COMPLETED_STATUSES  = ["Livré", "Annulé", "Refusé", "Returned"]
NORMAL_DELIVERY_FEE = 20
EXCHANGE_DELIVERY_FEE = 10




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

async def load_drivers(session):
    result = await session.execute(select(Driver))
    return {d.id: d for d in result.scalars()}

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
    async for session in get_session():
        drivers = await load_drivers(session)
        if driver_id in drivers:
            response = RedirectResponse(url=f"/static/index.html?driver={driver_id}", status_code=302)
            return response
    return HTMLResponse("<h2>Invalid driver ID</h2>", status_code=401)


@app.post("/admin/login")
async def admin_login(password: str = Form(...)):
    if password == ADMIN_PASSWORD:
        return {"success": True}
    raise HTTPException(status_code=401, detail="Invalid admin password")

@app.get("/drivers")
async def list_drivers():
    async for session in get_session():
        drivers = await load_drivers(session)
        return list(drivers.keys())


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
# Core functions – Database logic
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


async def order_exists(session: AsyncSession, driver_id: str, order_name: str) -> bool:
    result = await session.scalar(
        select(Order).where(Order.driver_id == driver_id, Order.order_name == order_name)
    )
    return result is not None


async def get_order_row(session: AsyncSession, driver_id: str, order_name: str) -> Optional[Order]:
    return await session.scalar(
        select(Order).where(Order.driver_id == driver_id, Order.order_name == order_name)
    )


async def add_to_payout(session: AsyncSession, driver_id: str, order_name: str,
                        cash_amount: float, driver_fee: float) -> str:
    """Create (or extend) an open payout row and write payout ID back to order."""
    payout = await session.scalar(
        select(Payout).where(Payout.driver_id == driver_id, Payout.status != 'paid')
        .order_by(Payout.date_created.desc())
    )
    if not payout:
        payout_id = f"PO-{dt.datetime.now().strftime('%Y%m%d-%H%M')}"
        payout = Payout(
            driver_id=driver_id,
            payout_id=payout_id,
            orders=order_name,
            total_cash=cash_amount,
            total_fees=driver_fee,
            total_payout=cash_amount - driver_fee,
            status='pending'
        )
        session.add(payout)
    else:
        orders_list = [o.strip() for o in (payout.orders or '').split(',') if o.strip()]
        orders_list.append(order_name)
        payout.orders = ', '.join(orders_list)
        payout.total_cash = (payout.total_cash or 0) + cash_amount
        payout.total_fees = (payout.total_fees or 0) + driver_fee
        payout.total_payout = payout.total_cash - payout.total_fees
        payout_id = payout.payout_id

    await session.flush()
    return payout_id


async def remove_from_payout(session: AsyncSession, payout_id: str, order_name: str,
                             cash_amount: float, driver_fee: float) -> None:
    payout = await session.scalar(select(Payout).where(Payout.payout_id == payout_id))
    if not payout:
        return

    orders_list = [o.strip() for o in (payout.orders or '').split(',') if o.strip()]
    if order_name not in orders_list:
        return

    orders_list.remove(order_name)
    payout.orders = ', '.join(orders_list)
    payout.total_cash = (payout.total_cash or 0) - cash_amount
    payout.total_fees = (payout.total_fees or 0) - driver_fee
    payout.total_payout = payout.total_cash - payout.total_fees

    await session.flush()


# ───────────────────────────────────────────────────────────────
# FastAPI ROUTES
# ───────────────────────────────────────────────────────────────

async def get_driver(session, driver_id: str) -> Driver:
    result = await session.get(Driver, driver_id)
    if not result:
        raise HTTPException(status_code=400, detail="Invalid driver")
    return result

@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "time": dt.datetime.utcnow().isoformat()}

# -------------------------------  SCAN  -------------------------------
@app.post("/scan", response_model=ScanResult, tags=["orders"])
async def scan(
    payload: ScanIn,
    driver: str = Query(..., description="driver1 / driver2 / …")
):
    async for session in get_session():
        await get_driver(session, driver)
        barcode = payload.barcode.strip()
        order_number = "#" + "".join(filter(str.isdigit, barcode))

        if len(order_number) <= 1:
            raise HTTPException(status_code=400, detail="Invalid barcode")

        if await order_exists(session, driver, order_number):
            existing = await get_order_row(session, driver, order_number)
            return ScanResult(
                result="⚠️ Already scanned",
                order=order_number,
                tag=get_primary_display_tag(existing.tags),
                deliveryStatus=existing.delivery_status,
            )
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

    order = Order(
        driver_id=driver,
        timestamp=dt.datetime.strptime(now_ts, "%Y-%m-%d %H:%M:%S"),
        order_name=order_number,
        customer_name=customer_name,
        customer_phone=phone,
        address=address,
        tags=tags,
        fulfillment=fulfillment,
        order_status=order_status,
        store=chosen_store_name,
        delivery_status="Dispatched",
        notes="",
        scheduled_time="",
        scan_date=scan_day,
        cash_amount=cash_amount,
        driver_fee=driver_fee,
    )
    session.add(order)
    await session.commit()

    return ScanResult(
        result=result_msg,
        order=order_number,
        tag=get_primary_display_tag(tags),
        deliveryStatus="Dispatched",
    )

# -----------------------------  ORDERS  -------------------------------
@app.get("/orders", tags=["orders"])
async def list_active_orders(driver: str = Query(...)):
    if driver in orders_cache:
        return orders_cache[driver]

    async for session in get_session():
        await get_driver(session, driver)
        result = await session.execute(
            select(Order).where(
                Order.driver_id == driver,
                Order.delivery_status.not_in(COMPLETED_STATUSES)
            )
        )
        rows = result.scalars().all()

        active = []
        for o in rows:
            active.append({
                "timestamp":    o.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "orderName":    o.order_name,
                "customerName": o.customer_name,
                "customerPhone":o.customer_phone,
                "address":      o.address,
                "tags":         o.tags,
                "deliveryStatus": o.delivery_status or "Dispatched",
                "notes":        o.notes,
                "scheduledTime": o.scheduled_time,
                "scanDate":     o.scan_date,
                "cashAmount":   o.cash_amount or 0,
                "driverFee":    o.driver_fee or 0,
                "payoutId":     o.payout_id,
                "statusLog":    o.status_log,
                "commLog":      o.comm_log,
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
async def update_order_status(
    payload: StatusUpdate,
    bg: BackgroundTasks,
    driver: str = Query(...)
):
    if payload.new_status and payload.new_status not in DELIVERY_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status")

    async for session in get_session():
        await get_driver(session, driver)
        order = await get_order_row(session, driver, payload.order_name)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        prev_status = order.delivery_status

        if payload.new_status:
            order.delivery_status = payload.new_status
            ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            order.status_log = ((order.status_log or "") + f" | {payload.new_status} @ {ts}").strip(" |")
        if payload.note is not None:
            order.notes = payload.note
        if payload.scheduled_time is not None:
            order.scheduled_time = payload.scheduled_time
        if payload.cash_amount is not None:
            order.cash_amount = payload.cash_amount
        if payload.comm_log is not None:
            order.comm_log = payload.comm_log

        if payload.new_status == "Livré" and prev_status != "Livré":
            driver_fee = calculate_driver_fee(order.tags)
            cash_amt = payload.cash_amount or (order.cash_amount or 0)
            payout_id = await add_to_payout(session, driver, payload.order_name, cash_amt, driver_fee)
            order.payout_id = payout_id
            order.driver_fee = driver_fee
        elif payload.new_status and payload.new_status != "Livré" and prev_status == "Livré":
            driver_fee = order.driver_fee or 0
            cash_amt = payload.cash_amount if payload.cash_amount is not None else (order.cash_amount or 0)
            await remove_from_payout(session, order.payout_id, payload.order_name, cash_amt, driver_fee)
            order.payout_id = None

        await session.commit()

        orders_cache.pop(driver, None)
        payouts_cache.pop(driver, None)
        return {"success": True}

# ----------------------------  PAYOUTS  -------------------------------
@app.get("/payouts", tags=["payouts"])
async def get_payouts(driver: str = Query(...)):
    if driver in payouts_cache:
        return payouts_cache[driver]

    async for session in get_session():
        await get_driver(session, driver)
        result = await session.execute(
            select(Payout).where(Payout.driver_id == driver).order_by(Payout.date_created.desc())
        )
        rows = result.scalars().all()
        payouts = []
        for p in rows:
            orders_list = [o.strip() for o in (p.orders or '').split(',') if o.strip()]
            order_details = []
            for name in orders_list:
                order = await session.scalar(
                    select(Order).where(Order.driver_id == driver, Order.order_name == name)
                )
                if order:
                    order_details.append({
                        "name": name,
                        "cashAmount": order.cash_amount or 0,
                        "driverFee": order.driver_fee or 0,
                    })
                else:
                    order_details.append({"name": name, "cashAmount": 0.0, "driverFee": 0.0})

            payouts.append({
                "payoutId":   p.payout_id,
                "dateCreated": p.date_created.strftime("%Y-%m-%d %H:%M:%S"),
                "orders":     p.orders,
                "totalCash":  p.total_cash or 0,
                "totalFees":  p.total_fees or 0,
                "totalPayout":p.total_payout or 0,
                "status":     p.status or "pending",
                "datePaid":   p.date_paid.strftime("%Y-%m-%d %H:%M:%S") if p.date_paid else "",
                "orderDetails": order_details,
            })

        payouts_cache[driver] = payouts
        return payouts

@app.post("/payout/mark-paid/{payout_id}", tags=["payouts"])
async def mark_payout_paid(payout_id: str, driver: str = Query(...)):
    async for session in get_session():
        await get_driver(session, driver)
        payout = await session.scalar(
            select(Payout).where(Payout.driver_id == driver, Payout.payout_id == payout_id)
        )
        if not payout:
            raise HTTPException(status_code=404, detail="Payout not found")

        payout.status = 'paid'
        payout.date_paid = dt.datetime.utcnow()
        await session.commit()

        payouts_cache.pop(driver, None)
        orders_cache.pop(driver, None)
        return {"success": True}


# ----------------------------  STATS  -------------------------------
async def _compute_stats(session: AsyncSession, driver: str,
                         days: int | None = None,
                         start: str | None = None,
                         end: str | None = None) -> dict:
    await get_driver(session, driver)
    q = select(Order).where(Order.driver_id == driver)

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

    if start_date:
        q = q.where(Order.scan_date >= start_date.strftime("%Y-%m-%d"))
    if end_date:
        q = q.where(Order.scan_date <= end_date.strftime("%Y-%m-%d"))

    result = await session.execute(q)
    rows = result.scalars().all()

    total = delivered = returned = 0
    collect = fees = canceled_amount = 0.0
    for o in rows:
        sd = None
        try:
            sd = dt.datetime.strptime(o.scan_date, "%Y-%m-%d").date() if o.scan_date else None
        except Exception:
            sd = None
        if start_date and (not sd or sd < start_date):
            continue
        if end_date and (not sd or sd > end_date):
            continue
        total += 1
        status = o.delivery_status
        cash = o.cash_amount or 0
        fee = o.driver_fee or 0
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
async def get_stats(
    driver: str = Query(...),
    days: int | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    async for session in get_session():
        stats = await _compute_stats(session, driver, days, start, end)
        return stats


@app.get("/admin/stats", tags=["admin"])
async def admin_stats(
    days: int | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    async for session in get_session():
        drivers = await load_drivers(session)
        result = {}
        for d in drivers.keys():
            result[d] = await _compute_stats(session, d, days, start, end)
        return result


# -------------------------------------------------------------------
# Daily trend data for all drivers
# -------------------------------------------------------------------
@app.get("/admin/trends", tags=["admin"])
async def admin_trends(
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

    async for session in get_session():
        drivers = await load_drivers(session)
        counts: dict[dt.date, int] = {}
        for driver in drivers.keys():
            q = select(Order).where(Order.driver_id == driver, Order.delivery_status == "Livré")
            if start_date:
                q = q.where(Order.scan_date >= start_date.strftime("%Y-%m-%d"))
            if end_date:
                q = q.where(Order.scan_date <= end_date.strftime("%Y-%m-%d"))
            result = await session.execute(q)
            for o in result.scalars():
                try:
                    sd = dt.datetime.strptime(o.scan_date, "%Y-%m-%d").date() if o.scan_date else None
                except Exception:
                    continue
                if not sd:
                    continue
                counts[sd] = counts.get(sd, 0) + 1

        days_sorted = sorted(counts.keys())
        return [
            {"date": d.strftime("%Y-%m-%d"), "delivered": counts[d]}
            for d in days_sorted
        ]


@app.get("/admin/search", tags=["admin"])
async def admin_search(q: str = Query(...)):
    """Search orders across all drivers by order name or phone."""
    q_lower = q.lower()
    results: list[dict] = []
    async for session in get_session():
        drivers = await load_drivers(session)
        for driver in drivers.keys():
            result = await session.execute(
                select(Order).where(
                    Order.driver_id == driver,
                    or_(
                        Order.order_name.ilike(f"%{q_lower}%"),
                        Order.customer_phone.ilike(f"%{q_lower}%")
                    )
                )
            )
            for o in result.scalars():
                results.append({
                    "driver": driver,
                    "orderName": o.order_name,
                    "customerName": o.customer_name,
                    "customerPhone": o.customer_phone,
                    "deliveryStatus": o.delivery_status or "Dispatched",
                    "cashAmount": o.cash_amount or 0,
                    "address": o.address,
                })
        return results


# ---------------------------- EMPLOYEES -------------------------------
@app.post("/employee/log", tags=["employees"])
async def employee_log(entry: EmployeeLog):
    """Append an employee action row to the database."""
    async for session in get_session():
        log = EmployeeLog(
            timestamp=dt.datetime.utcnow(),
            employee=entry.employee,
            order=entry.order,
            amount=entry.amount,
        )
        session.add(log)
        await session.commit()
        return {"success": True}


@app.get("/employee/logs", tags=["employees"])
async def employee_logs():
    """Return all employee log rows as a list of dictionaries."""
    async for session in get_session():
        result = await session.execute(select(EmployeeLog).order_by(EmployeeLog.timestamp.desc()))
        logs = []
        for r in result.scalars():
            logs.append({
                "timestamp": r.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "employee": r.employee,
                "order": r.order,
                "amount": r.amount,
            })
        return logs


