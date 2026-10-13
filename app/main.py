"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (read from environment, with fallback to local .env)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import os
import time
from typing import Any, Dict, List, Literal, Optional

from bson import json_util

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def create_indexes():
    """Create all required indexes idempotently at startup."""
    # employees collection
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a datetime instant to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt(ms: int) -> datetime:
    """Convert integer epoch milliseconds (truncated to whole seconds) to UTC datetime."""
    # R1: Instants are truncated to whole seconds before any calculation
    sec = (ms // 1000)
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def round_half_up(val: float, decimals: int = 2) -> float:
    """R8 Rounding: Half-up rounding (not banker's rounding)."""
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    d = Decimal(str(val)).quantize(Decimal(fmt), rounding=ROUND_HALF_UP)
    return float(d)


def compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: A record's date is the IST calendar date of its punch_in instant.
    Overnight shifts (shift_end <= shift_start): a punch-in whose IST time
    is strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    dt_ist = punch_in_utc.astimezone(IST)
    ist_date = dt_ist.date()
    ist_time_str = dt_ist.strftime("%H:%M")

    if shift_end <= shift_start and ist_time_str < shift_end:
        return (ist_date - timedelta(days=1)).isoformat()
    return ist_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, date_str: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch-in is strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start.
    """
    shift_start_dt = datetime.fromisoformat(f"{date_str}T{shift_start}:00").replace(tzinfo=IST)
    diff_sec = (punch_in_utc - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    hours = Decimal(str(diff_sec)) / Decimal("3600")
    rounded = hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    For overnight shifts, shift_end is on the next calendar day.
    """
    if shift_end <= shift_start:
        next_date = (date.fromisoformat(date_str) + timedelta(days=1)).isoformat()
        shift_end_dt = datetime.fromisoformat(f"{next_date}T{shift_end}:00").replace(tzinfo=IST)
    else:
        shift_end_dt = datetime.fromisoformat(f"{date_str}T{shift_end}:00").replace(tzinfo=IST)

    diff_sec = (punch_out_utc - shift_end_dt).total_seconds()
    minutes = int(diff_sec // 60)
    return minutes if minutes >= 30 else 0


def count_working_days(joined_on_str: str, month_str: str) -> int:
    """R7: Working days are Mon-Fri in month_str counted from joined_on for mid-month joiners."""
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    first_day = date.fromisoformat(f"{month_str}-01")
    last_day = date.fromisoformat(f"{month_str}-{num_days:02d}")
    join_day = date.fromisoformat(joined_on_str)
    start_date = max(first_day, join_day)

    if start_date > last_day:
        return 0

    cur = start_date
    working_days = 0
    while cur <= last_day:
        if cur.weekday() < 5:
            working_days += 1
        cur += timedelta(days=1)
    return working_days


def format_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    formatted_changes = {}
    for field, change in entry.get("changes", {}).items():
        from_val = change.get("from")
        to_val = change.get("to")
        if field in ("punch_in", "punch_out"):
            formatted_changes[field] = {
                "from": dt_to_epoch_ms(from_val) if isinstance(from_val, datetime) else from_val,
                "to": dt_to_epoch_ms(to_val) if isinstance(to_val, datetime) else to_val,
            }
        else:
            formatted_changes[field] = {"from": from_val, "to": to_val}

    return {
        "at": dt_to_epoch_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": formatted_changes,
    }


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    history = [format_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc["created_at"]),
    }


# --------------------------------------------------------------------------- #
# Request & Response Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")


@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeCreate):
    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_dt(body.punched_at)
    else:
        now_sec = int(time.time())
        punch_in_dt = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    att_date = compute_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = epoch_ms_to_dt(body.punched_at)
    else:
        now_sec = int(time.time())
        punch_out_dt = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    # Find the employee's most recent record
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code},
        sort=[("punch_in", -1)],
    )

    if not record or record.get("punch_in") is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No punch-in found")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    if punch_out_dt <= punch_in_dt or punch_out_dt > (punch_in_dt + timedelta(hours=24)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at must be strictly greater than punch_in and within 24 hours",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record already punched out")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_min = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = work_hours < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record already punched out")

    return format_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from must not be greater than date_to",
        )

    if status_filter and status_filter not in ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid status filter",
        )

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status_filter:
        q["status"] = status_filter
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_attendance_doc(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date_str}")
def regularize_attendance(emp_code: str, date_str: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")

    # Rule: setting status ABSENT or LEAVE clears both punch times; supplying punch times with it is 422
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot supply punch times when setting status to ABSENT or LEAVE",
        )

    target_status = body.status if body.status is not None else record["status"]

    if target_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            new_punch_in = epoch_ms_to_dt(body.punch_in)
        else:
            new_punch_in = record.get("punch_in")

        if new_punch_in is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Presence status requires a punch_in",
            )

        if new_punch_in.tzinfo is None:
            new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)

        # punch_in must stay on the record's attendance date (R1)
        calc_date = compute_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date_str:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in must stay on the record's attendance date",
            )

        if body.punch_out is not None:
            new_punch_out = epoch_ms_to_dt(body.punch_out)
        else:
            new_punch_out = record.get("punch_out")

        if new_punch_out is not None:
            if new_punch_out.tzinfo is None:
                new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)

            if new_punch_out <= new_punch_in or new_punch_out > (new_punch_in + timedelta(hours=24)):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be after punch_in and within 24 hours",
                )

        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date_str)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_start"], emp["shift_end"], date_str)
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Compare old and new fields to detect changes
    old_punch_in = record.get("punch_in")
    if old_punch_in and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)

    old_punch_out = record.get("punch_out")
    if old_punch_out and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    db_changes = {}
    if record["status"] != target_status:
        db_changes["status"] = {"from": record["status"], "to": target_status}
    if old_punch_in != new_punch_in:
        db_changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if old_punch_out != new_punch_out:
        db_changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if record.get("work_hours") != new_work_hours:
        db_changes["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}
    if record.get("late_minutes", 0) != new_late_minutes:
        db_changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late_minutes}
    if record.get("overtime_minutes", 0) != new_overtime_minutes:
        db_changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_overtime_minutes}
    if record.get("half_day", False) != new_half_day:
        db_changes["half_day"] = {"from": record.get("half_day", False), "to": new_half_day}

    if not db_changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A request that changes nothing is 422",
        )

    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": db_changes,
    }

    # Optimistic concurrency control using history array size
    current_history_len = len(record.get("history", []))
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            f"history.{current_history_len}": {"$exists": False},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": history_entry,
            },
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent regularization modification conflict",
        )

    return format_attendance_doc(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    year, m_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m_num)[1]
    first_day = f"{month}-01"
    last_day = f"{month}-{num_days:02d}"

    working_days = count_working_days(emp["joined_on"], month)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day, "$lte": last_day},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                            [1, 2, 3, 4, 5],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    agg_result = list(db.attendance_logs.aggregate(pipeline))
    if agg_result:
        row = agg_result[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, m_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m_num)[1]
    first_day = f"{month}-01"
    last_day = f"{month}-{num_days:02d}"

    match_stage = {"joined_on": {"$lte": last_day}}
    if department is not None:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", first_day]},
                                    {"$lte": ["$date", last_day]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {
                                                "$in": [
                                                    {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}},
                                                    [1, 2, 3, 4, 5],
                                                ]
                                            },
                                        ]
                                    },
                                }
                            },
                            "as": "pl",
                            "in": {"$cond": [{"$eq": ["$$pl.half_day", True]}, 0.5, 1.0]},
                        }
                    }
                },
                "work_hours_list": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {
                                    "$and": [
                                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$$l.work_hours", None]},
                                    ]
                                },
                            }
                        },
                        "as": "wl",
                        "in": "$$wl.work_hours",
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "all_work_hours": {"$push": "$work_hours_list"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "flat_work_hours": {
                    "$reduce": {
                        "input": "$all_work_hours",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$flat_work_hours"}, 0]},
                        {"$avg": "$flat_work_hours"},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"department": 1}},
    ]

    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        avg_wh = r.get("avg_work_hours")
        if avg_wh is not None:
            avg_wh = round_half_up(float(avg_wh), 2)
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, m_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m_num)[1]
    first_day = f"{month}-01"
    last_day = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": first_day, "$lte": last_day},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}},
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    results = list(db.attendance_logs.aggregate(pipeline))
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in results
    ]

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    dept_emp = db.employees.find_one({"department": department})
    if not dept_emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    if to_date < from_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'to' must be greater than or equal to 'from'",
        )

    f_d = date.fromisoformat(from_date)
    t_d = date.fromisoformat(to_date)
    day_span = (t_d - f_d).days + 1
    if day_span > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Date range exceeds 92 days",
        )

    from_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    to_dt = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc)
    to_plus_one = to_dt + timedelta(days=1)

    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "present_count": 1,
                "late_count": 1,
                "d_date": {"$dateFromString": {"dateString": "$_id"}},
            }
        },
        {
            "$densify": {
                "field": "d_date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_plus_one],
                },
            }
        },
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d_date"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "day_of_week": {"$isoDayOfWeek": "$d_date"},
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$in": ["$day_of_week", [1, 2, 3, 4, 5]]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc_arr",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$first": "$hc_arr.count"}, 0]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"d_date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "d_date": 0,
                "day_of_week": 0,
                "hc_arr": 0,
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in results:
        att_rate = r.get("attendance_rate")
        if att_rate is not None:
            att_rate = round_half_up(float(att_rate), 4)

        m_avg = r.get("moving_avg_7d")
        if m_avg is not None:
            m_avg = round_half_up(float(m_avg), 4)

        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": att_rate,
            "moving_avg_7d": m_avg,
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint_name}")
def explain_endpoint(
    endpoint_name: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint_name == "attendance_list":
        collection_name = "attendance_logs"
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status_filter:
            q["status"] = status_filter
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        skip = (page - 1) * page_size
        plan = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )

    elif endpoint_name == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="emp_code and month are required for employee_monthly explain",
            )
        collection_name = "attendance_logs"
        year, m_num = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m_num)[1]
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": f"{month}-01", "$lte": f"{month}-{num_days:02d}"},
                }
            }
        ]
        plan = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    elif endpoint_name == "department_summary":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required for department_summary explain",
            )
        collection_name = "employees"
        year, m_num = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m_num)[1]
        last_day = f"{month}-{num_days:02d}"
        match_stage = {"joined_on": {"$lte": last_day}}
        if department is not None:
            match_stage["department"] = department
        pipeline = [{"$match": match_stage}]
        plan = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    elif endpoint_name == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required for late_leaderboard explain",
            )
        collection_name = "attendance_logs"
        year, m_num = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m_num)[1]
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": f"{month}-01", "$lte": f"{month}-{num_days:02d}"},
                    "late_minutes": {"$gt": 0},
                }
            }
        ]
        plan = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    elif endpoint_name == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="department, from, and to are required for department_trend explain",
            )
        collection_name = "attendance_logs"
        pipeline = [{"$match": {"date": {"$gte": from_date, "$lte": to_date}}}]
        plan = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown endpoint: {endpoint_name}",
        )

    return {
        "endpoint": endpoint_name,
        "collection": collection_name,
        "explain": json.loads(json_util.dumps(plan)),
    }
