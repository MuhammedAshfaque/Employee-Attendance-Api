# DECISIONS.md

1. **Indexes.**
We created:
- `employees`: unique `{"emp_code": 1}` to guarantee uniqueness and fast lookups; `{"department": 1, "joined_on": 1}` and `{"joined_on": 1}` to back department filtering and headcount ranges.
- `attendance_logs`: unique `{"emp_code": 1, "date": 1}` for the natural key constraint and race prevention; `{"date": -1, "emp_code": 1}` to satisfy list sorting and range filtering without memory sorting; `{"emp_code": 1, "date": -1}` for employee history; `{"emp_code": 1, "punch_in": -1}` to quickly find the open record on punch-out; and `{"date": 1, "late_minutes": 1, "emp_code": 1}` for the leaderboard.
We considered and rejected an index on `(date: 1, emp_code: 1)` in ascending order because MongoDB cannot satisfy the mixed-direction sort `{"date": -1, "emp_code": 1}` without an in-memory sort or collection scan.

2. **Punch-in race.**
Both concurrent requests compute the attendance date and attempt `db.attendance_logs.insert_one(doc)`. The unique compound index on `(emp_code, date)` guarantees atomicity at the storage engine level. Exactly one insert succeeds and returns HTTP 201; the second insert immediately triggers a `DuplicateKeyError`, which our error handler catches to return HTTP 409 Conflict.

3. **Ties.**
The pipeline computes ranks via `$setWindowFields` using the `$rank` operator (standard competition ranking: 1, 2, 2, 4) and filters by `rank <= limit` afterward. When employees tie at the cutoff, both share the same rank, so both are returned (exceeding `limit` rows), sorted by `total_late_minutes` descending, then `emp_code` ascending.

4. **Headcount.**
The aggregation pipeline starts from `employees` (matching `joined_on <= month_end`) instead of `attendance_logs`. It groups by department to compute `headcount` via `{$sum: 1}`, then incorporates attendance logs via `$lookup`. Employees with zero logs produce an empty array, so they contribute to headcount without altering presence counts.

5. **One thing you would change for 100x data.**
At 100x scale (~10 million records), cross-collection `$lookup` stages during ad-hoc analytics queries would cause cache pressure. We would maintain pre-aggregated monthly summary rollups updated incrementally on punch-out/regularization or via MongoDB change streams.
