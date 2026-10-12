# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/punch-out, manual corrections with an audit trail, and attendance analytics built on aggregation pipelines. All code is in `app/main.py`.

## Run

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
docker run -d -p 27017:27017 mongo:7                   # or use an Atlas cluster
cp .env.example .env                                   # MONGO_URI / MONGO_DB, real env vars take priority
uvicorn app.main:app --port 8000
```

Indexes are created on startup. Interactive docs are at `http://localhost:8000/docs`.

## Notes

- Everything in the contract is implemented, nothing is missing.
- Instants are stored as BSON dates (UTC) and exposed as epoch milliseconds, truncated to whole seconds. Rule checks are done in IST.
- Rounding is half-up on exact decimals (`Decimal` in Python, `Decimal128` in pipelines), so values like 9.125 become 9.13.
- Judgement calls where the contract leaves room:
  - The trend rejects ranges longer than 92 days counted inclusively (01 Jul - 30 Sep is allowed).
  - `moving_avg_7d` averages the reported, already rounded `attendance_rate` values.
  - Punch-out closes the latest record (by attendance date) whose `punch_in` is at or before `punched_at`.
  - A PATCH counts as "changes nothing" when the final record, including recomputed derived fields, equals the stored one.
  - A concurrent PATCH that loses the race gets 409 instead of overwriting.
