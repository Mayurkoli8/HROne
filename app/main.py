"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience, real env vars win)
"""
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db.employees
logs = db.attendance_logs

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]
DERIVED_DEFAULTS = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


@asynccontextmanager
async def lifespan(app: FastAPI):
    employees.create_index("emp_code", unique=True)
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    employees.create_index("joined_on")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    logs.create_index(ATTENDANCE_SORT)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #
def _real_date(value: str) -> str:
    date.fromisoformat(value)
    return value


DateStr = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$"), AfterValidator(_real_date)]
Month = Annotated[str, StringConstraints(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
HHMM = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOut(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class Regularize(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def invalid(msg: str, loc: tuple = ("body",)):
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


def require(**params):
    missing = [name for name, value in params.items() if value is None]
    if missing:
        raise RequestValidationError(
            [{"type": "missing", "loc": ("query", name), "msg": "Field required", "input": None} for name in missing]
        )


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_dt(ms: int) -> datetime:
    return EPOCH + timedelta(seconds=ms // 1000)


def to_ms(value):
    if isinstance(value, datetime):
        return (value - EPOCH) // timedelta(milliseconds=1)
    return value


def half_up(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP))


def get_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]


def shift_time(day: str, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}").replace(tzinfo=IST)


def attendance_date(punch_in: datetime, emp: dict) -> str:
    local = punch_in.astimezone(IST)
    if is_overnight(emp) and local.time() < time.fromisoformat(emp["shift_end"]):
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def compute_late_minutes(punch_in: datetime, emp: dict, day: str) -> int:
    seconds = (punch_in - shift_time(day, emp["shift_start"])).total_seconds()
    return int(seconds // 60) if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return half_up(Decimal(int((punch_out - punch_in).total_seconds())) / 3600, 2)


def compute_overtime(punch_out: datetime, emp: dict, day: str) -> int:
    end = shift_time(day, emp["shift_end"])
    if is_overnight(emp):
        end += timedelta(days=1)
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def punch_out_fields(punch_in: datetime, punch_out: Optional[datetime], emp: dict, day: str) -> dict:
    if punch_out is None:
        return {"work_hours": None, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {"work_hours": hours, "overtime_minutes": compute_overtime(punch_out, emp, day), "half_day": hours < 4.5}


def employee_out(doc: dict) -> dict:
    fields = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")
    return {**{f: doc.get(f) for f in fields}, "created_at": to_ms(doc.get("created_at"))}


def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": [
            {
                "at": to_ms(h["at"]),
                "by": h["by"],
                "reason": h["reason"],
                "changes": {k: {"from": to_ms(c["from"]), "to": to_ms(c["to"])} for k, c in h["changes"].items()},
            }
            for h in doc.get("history") or []
        ],
    }


def month_range(month: str) -> tuple[str, str]:
    first = date.fromisoformat(f"{month}-01")
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def working_days(start: str, end: str) -> int:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return sum((first + timedelta(days=i)).weekday() < 5 for i in range((last - first).days + 1))


# --------------------------------------------------------------------------- #
# Queries and pipelines (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
IS_PRESENT = {"$in": ["$status", PRESENCE]}
DAY_VALUE = {"$cond": [IS_PRESENT, {"$cond": ["$half_day", 0.5, 1]}, 0]}
IS_WEEKDAY = {"$lt": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}

LOG_FACTS = {"$project": {
    "_id": 0,
    "date": 1,
    "present": DAY_VALUE,
    "weekday_present": {"$cond": [IS_WEEKDAY, DAY_VALUE, 0]},
    "late": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]},
    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
    "leave": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
    "on_duty": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]},
    "work_hours": {"$cond": [{"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}, {"$toDecimal": "$work_hours"}, None]},
}}

EMPTY_MONTH = dict.fromkeys(("present_days", "leave_days", "late_count", "total_late_minutes", "total_overtime_minutes"), 0)


def half_up_expr(expr, places: int) -> dict:
    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, scale]}, Decimal128("0.5")]}}, scale]}


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    if date_from and date_to and date_from > date_to:
        invalid("date_from must not be after date_to", ("query", "date_from"))
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {op: v for op, v in (("$gte", date_from), ("$lte", date_to)) if v}
    if status is not None:
        q["status"] = status
    return q


def monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_range(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        LOG_FACTS,
        {"$group": {
            "_id": None,
            "present_days": {"$sum": "$weekday_present"},
            "leave_days": {"$sum": "$leave"},
            "late_count": {"$sum": "$late"},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
        {"$project": {"_id": 0}},
    ]


def summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last = month_range(month)
    match = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": first, "$lte": last}}}, LOG_FACTS],
            "as": "log",
        }},
        {"$unwind": {"path": "$log", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "employees": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": "$log.weekday_present"},
            "avg_work_hours": {"$avg": "$log.work_hours"},
            "late_count": {"$sum": "$log.late"},
            "total_late_minutes": {"$sum": "$log.late_minutes"},
            "leave_count": {"$sum": "$log.leave"},
            "on_duty_count": {"$sum": "$log.on_duty"},
        }},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$employees"},
            "present_days": "$present_days",
            "avg_work_hours": {"$toDouble": half_up_expr("$avg_work_hours", 2)},
            "late_count": "$late_count",
            "total_late_minutes": "$total_late_minutes",
            "leave_count": "$leave_count",
            "on_duty_count": "$on_duty_count",
        }},
    ]


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_range(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    return pipeline + [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": "$rank",
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": "$total_late_minutes",
            "late_count": "$late_count",
        }},
    ]


def check_trend_range(start: str, end: str):
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    if days < 0:
        invalid("to must not be before from", ("query", "to"))
    if days >= 92:
        invalid("range cannot exceed 92 days", ("query", "to"))


def trend_pipeline(department: str, start: str, end: str) -> list:
    first = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    after_last = datetime.fromisoformat(end).replace(tzinfo=timezone.utc) + timedelta(days=1)
    has_rate = {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}
    rate = half_up_expr({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4)
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": start, "$lte": end}}}, LOG_FACTS],
            "as": "log",
        }},
        {"$unwind": "$log"},
        {"$group": {"_id": "$log.date", "present_count": {"$sum": "$log.present"}, "late_count": {"$sum": "$log.late"}}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [first, after_last]}}},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}}}},
        {"$lookup": {
            "from": "employees",
            "let": {"date": "$date"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$date"]}}},
                {"$count": "n"},
            ],
            "as": "joined",
        }},
        {"$set": {
            "is_working_day": {"$lt": [{"$isoDayOfWeek": "$day"}, 6]},
            "headcount": {"$ifNull": [{"$first": "$joined.n"}, 0]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$set": {"attendance_rate": {"$cond": [has_rate, rate, None]}}},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$project": {
            "_id": 0,
            "date": "$date",
            "is_working_day": "$is_working_day",
            "headcount": "$headcount",
            "present_count": "$present_count",
            "late_count": "$late_count",
            "attendance_rate": {"$toDouble": "$attendance_rate"},
            "moving_avg_7d": {"$toDouble": half_up_expr("$moving_avg_7d", 4)},
        }},
    ]


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = {**body.model_dump(), "created_at": now()}
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {} if department is None else {"department": department}
    cursor = employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    items = [employee_out(e) for e in cursor]
    return {"items": items, "total": employees.count_documents(q), "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchIn):
    emp = get_employee(body.emp_code)
    ts = to_dt(body.punched_at) if body.punched_at is not None else now()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp, day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOut):
    emp = get_employee(body.emp_code)
    ts = to_dt(body.punched_at) if body.punched_at is not None else now()
    record = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("date", DESCENDING)])
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= record["punch_in"] or ts - record["punch_in"] > timedelta(hours=24):
        invalid("punched_at must be after punch_in and within 24 hours", ("body", "punched_at"))

    updated = logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **punch_out_fields(record["punch_in"], ts, emp, record["date"])}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    cursor = logs.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    items = [record_out(d) for d in cursor]
    return {"items": items, "total": logs.count_documents(q), "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: DateStr, body: Regularize):
    emp = get_employee(emp_code)
    record = logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "no attendance record for that date")

    status = body.status or record["status"]
    punch_in = to_dt(body.punch_in) if body.punch_in is not None else record.get("punch_in")
    punch_out = to_dt(body.punch_out) if body.punch_out is not None else record.get("punch_out")
    if status in PRESENCE:
        if punch_in is None:
            invalid(f"{status} needs a punch_in", ("body", "punch_in"))
        if body.punch_in is not None and attendance_date(punch_in, emp) != date:
            invalid("punch_in must stay on the record's attendance date", ("body", "punch_in"))
        if punch_out is not None and not punch_in < punch_out <= punch_in + timedelta(hours=24):
            invalid("punch_out must be after punch_in and within 24 hours", ("body", "punch_out"))
        late = compute_late_minutes(punch_in, emp, date)
    else:
        if body.punch_in is not None or body.punch_out is not None:
            invalid(f"{status} cannot have punch times")
        punch_in = punch_out = None
        late = 0

    new = {
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "late_minutes": late,
        **punch_out_fields(punch_in, punch_out, emp, date),
    }
    changes = {}
    for field, value in new.items():
        old = record.get(field, DERIVED_DEFAULTS.get(field))
        if old != value:
            changes[field] = {"from": old, "to": value}
    if not changes:
        invalid("the request does not change anything")

    entry = {"at": datetime.now(timezone.utc), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": record.get("punch_out"), "history": record.get("history")},
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified by another request, please retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    emp = get_employee(emp_code)
    first, last = month_range(month)
    working = working_days(max(first, emp["joined_on"]), last)
    totals = next(logs.aggregate(monthly_pipeline(emp_code, month)), EMPTY_MONTH)
    pct = half_up(Decimal(totals["present_days"]) * 100 / working, 2) if working else None
    return {"emp_code": emp_code, "month": month, "working_days": working, **totals, "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    return {"month": month, "items": list(employees.aggregate(summary_pipeline(month, department)))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: Limit = 10, department: Optional[str] = None):
    return {"month": month, "items": list(logs.aggregate(leaderboard_pipeline(month, limit, department)))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    start: Annotated[DateStr, Query(alias="from")],
    end: Annotated[DateStr, Query(alias="to")],
):
    check_trend_range(start, end)
    if employees.find_one({"department": department}) is None:
        raise HTTPException(404, "unknown department")
    return {"department": department, "items": list(employees.aggregate(trend_pipeline(department, start, end)))}


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[Month] = None,
    department: Optional[str] = None,
    limit: Limit = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    start: Annotated[Optional[DateStr], Query(alias="from")] = None,
    end: Annotated[Optional[DateStr], Query(alias="to")] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            require(emp_code=emp_code, month=month)
            collection, pipeline = "attendance_logs", monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            require(month=month)
            collection, pipeline = "employees", summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            require(month=month)
            collection, pipeline = "attendance_logs", leaderboard_pipeline(month, limit, department)
        else:
            require(department=department, **{"from": start, "to": end})
            check_trend_range(start, end)
            collection, pipeline = "employees", trend_pipeline(department, start, end)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    plan = db.command("explain", command, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(plan))}
