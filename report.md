# Project Run & Verification Guide (`report.md`)

This guide provides step-by-step instructions for running the **Employee Attendance & Analytics API**, inspecting its endpoints, and verifying that all business rules (R1–R10), concurrency controls, and MongoDB aggregation pipelines are working correctly.

---

## 1. Prerequisites

1. **Python 3.11 or newer**:
   Check your Python version:
   ```powershell
   python --version
   ```

2. **MongoDB 6.0+ (Local Service or Atlas)**:
   The application works seamlessly with either a local MongoDB instance or MongoDB Atlas.

   - **Option A: Local MongoDB**
     Verify the local MongoDB service is running on port `27017`:
     ```powershell
     Get-Service MongoDB
     ```
     If stopped, start it:
     ```powershell
     Start-Service MongoDB
     ```

   - **Option B: MongoDB Atlas (Cloud)**
     If connecting to MongoDB Atlas, ensure:
     1. **Database Access**: A database user is created in Atlas with **Read and write to any database** (or `readWriteAnyDatabase`).
     2. **Network Access**: Your current IP address (or `0.0.0.0/0` - Allow access from anywhere) is added to the IP Access List in Atlas.

---

## 2. Installation & Setup

1. **Open PowerShell / Terminal** in the project directory:
   ```powershell
   cd "c:\Users\ashfa\Downloads\SDT_candidate_kit (1)\candidate_kit"
   ```

2. **Install Dependencies**:
   ```powershell
   pip install -r requirements.txt
   ```

3. **Configure Database Connection**:
   You can configure the database via a local `.env` file or terminal environment variables.

   - **Using `.env` file (recommended for local use)**:
     Create or update `.env` in the project root:
     ```env
     # For MongoDB Atlas:
     MONGO_URI=mongodb+srv://<username>:<password>@cluster0.exsqavg.mongodb.net/?appName=Cluster0
     MONGO_DB=attendance_db

     # Or for Local MongoDB:
     # MONGO_URI=mongodb://localhost:27017
     # MONGO_DB=attendance_db
     ```
     > **Note:** `.env` is already included in `.gitignore` so secrets are never committed to Git.

   - **Using PowerShell Environment Variables (overrides `.env`)**:
     ```powershell
     $env:MONGO_URI = "mongodb+srv://<username>:<password>@cluster0.exsqavg.mongodb.net/?appName=Cluster0"
     $env:MONGO_DB = "attendance_db"
     ```

4. **Seed Sample Data**:
   Populate your target MongoDB database (local or Atlas) with the initial 6 employees and 9 sample attendance records:
   ```powershell
   python sample_seed.py
   ```
   *Expected Output:*
   ```text
   loaded  6 documents into 'attendance_db.employees'
   loaded  9 documents into 'attendance_db.attendance_logs'
   ```

---

## 3. Starting the Server

Start Uvicorn from the repository root:
```powershell
uvicorn app.main:app --port 8000 --reload
```
*Expected Output:*
```text
INFO:     Started server process
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```
> **Note:** During application startup, all required indexes on `employees` and `attendance_logs` are created automatically and idempotently.

---

## 4. Quick Browser / Interactive Verification

Open your browser and navigate to:
- **Interactive Swagger UI**: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **ReDoc API Documentation**: [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)
- **Health Check**: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health) (Returns `{"status": "ok"}` when MongoDB is connected)

---

## 5. Step-by-Step Endpoint Testing (PowerShell / curl)

You can run these commands in a separate terminal window to test every core feature:

### Step 5.1: Health Probe
```powershell
curl.exe -s http://127.0.0.1:8000/health
```
**Expected Response (200 OK):**
```json
{"status": "ok"}
```

---

### Step 5.2: Create and List Employees
1. **Create a new employee (PowerShell Native):**
   ```powershell
   Invoke-RestMethod -Uri "http://127.0.0.1:8000/employees" -Method Post -ContentType "application/json" -Body '{"emp_code": "EMP0010", "name": "Aarav Sharma", "email": "aarav@example.com", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-01"}'
   ```
   *(Or with `curl.exe` on Windows with escaped quotes):*
   ```powershell
   curl.exe -s -X POST http://127.0.0.1:8000/employees -H "Content-Type: application/json" -d "{\"emp_code\": \"EMP0010\", \"name\": \"Aarav Sharma\", \"email\": \"aarav@example.com\", \"department\": \"Engineering\", \"shift_start\": \"09:30\", \"shift_end\": \"18:30\", \"joined_on\": \"2026-07-01\"}"
   ```
   *Expected Response:* HTTP 201 with the created employee object including `created_at` as epoch milliseconds.

2. **Test Duplicate Prevention (Race Safety):**
   Run the exact same command again:
   *Expected Response (409 Conflict):*
   ```json
   {"detail": "emp_code already exists"}
   ```

3. **List employees in Engineering department:**
   ```powershell
   Invoke-RestMethod -Uri "http://127.0.0.1:8000/employees?department=Engineering&page=1&page_size=10"
   ```

---

### Step 5.3: Punch-In (Business Rules R1 & R2)
Punch in employee `EMP0010` for `2026-07-20 09:40:01 IST` (which is strictly > 10:00 after shift start, giving 10 minutes late):
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/attendance/punch-in" -Method Post -ContentType "application/json" -Body '{"emp_code": "EMP0010", "punched_at": 1784520601000, "status": "PRESENT"}'
```
**Expected Response (201 Created):**
```json
{
  "emp_code": "EMP0010",
  "date": "2026-07-20",
  "status": "PRESENT",
  "punch_in": 1784520601000,
  "punch_out": null,
  "work_hours": null,
  "late_minutes": 10,
  "overtime_minutes": 0,
  "half_day": false,
  "history": []
}
```

*Duplicate Punch-in Test:*
Run the same command again for the same date:
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/attendance/punch-in" -Method Post -ContentType "application/json" -Body '{"emp_code": "EMP0010", "punched_at": 1784520601000, "status": "PRESENT"}'
```
**Expected Response:** HTTP 409 `{"detail": "Already punched in for this date"}`.

---

### Step 5.4: Punch-Out (Business Rules R3, R4 & R5)
Punch out employee `EMP0010` at `2026-07-20 19:15:00 IST` (45 min after shift end 18:30 -> 45 min overtime):
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/attendance/punch-out" -Method Post -ContentType "application/json" -Body '{"emp_code": "EMP0010", "punched_at": 1784555100000}'
```
**Expected Response (200 OK):**
```json
{
  "emp_code": "EMP0010",
  "date": "2026-07-20",
  "status": "PRESENT",
  "punch_in": 1784520601000,
  "punch_out": 1784555100000,
  "work_hours": 9.58,
  "late_minutes": 10,
  "overtime_minutes": 45,
  "half_day": false,
  "history": []
}
```

*Double Punch-Out Test:*
Run the same punch-out command again:
**Expected Response:** HTTP 409 `{"detail": "Record already punched out"}`.

---

### Step 5.5: Manual Regularization with Audit Trail (PATCH)
Modify the status of `EMP0010` on `2026-07-20` to `WFH`:
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/attendance/EMP0010/2026-07-20" -Method Patch -ContentType "application/json" -Body '{"status": "WFH", "reason": "Biometric system correction", "regularized_by": "hr_admin"}'
```
**Expected Response (200 OK):**
Record status updated to `"WFH"`, and `history` array contains 1 entry recording:
- `at`: timestamp of change
- `by`: `"hr_admin"`
- `reason`: `"Biometric system correction"`
- `changes`: `{"status": {"from": "PRESENT", "to": "WFH"}}`

---

### Step 5.6: Employee Monthly Analytics
Calculate monthly metrics for `EMP0001` in July 2026:
```powershell
curl.exe -s "http://127.0.0.1:8000/analytics/employees/EMP0001/monthly?month=2026-07"
```
**Expected Response (200 OK):**
```json
{
  "emp_code": "EMP0001",
  "month": "2026-07",
  "working_days": 23,
  "present_days": 2.0,
  "leave_days": 0,
  "late_count": 0,
  "total_late_minutes": 0,
  "total_overtime_minutes": 0,
  "attendance_pct": 8.7
}
```

---

### Step 5.7: Department Summary Analytics
Get summary across departments for July 2026:
```powershell
curl.exe -s "http://127.0.0.1:8000/analytics/departments/summary?month=2026-07"
```
**Expected Response (200 OK):**
Shows breakdown for Engineering, Sales, and Support with:
- `headcount`: includes employees even if they have 0 logs (R9).
- `avg_work_hours`: record-weighted mean rounded to 2 decimals half-up (R8).

---

### Step 5.8: Late-Comers Leaderboard (Window Function & Ties)
Rank employees with late arrivals for July 2026:
```powershell
curl.exe -s "http://127.0.0.1:8000/analytics/leaderboard/late?month=2026-07&limit=10"
```
**Expected Response (200 OK):**
- Standard competition ranking (`1, 2, 2, 4`).
- Tied rows ordered by `emp_code` ascending.

---

### Step 5.9: Department Trend (Time-Series & Gap Filling)
Retrieve daily trend with 7-day moving average:
```powershell
curl.exe -s "http://127.0.0.1:8000/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-10"
```
**Expected Response (200 OK):**
- Exactly one row per calendar day (gap filled using MongoDB `$densify`).
- `attendance_rate`: present_count / headcount on working days; `null` on weekends.
- `moving_avg_7d`: mean over preceding valid window.

---

### Step 5.10: Admin Explain Verification
Verify that the main queries use index scans (`IXSCAN`) and no collection scans (`COLLSCAN`):
```powershell
curl.exe -s "http://127.0.0.1:8000/admin/explain/department_trend?department=Engineering&from=2026-07-06&to=2026-07-10" | Select-String "IXSCAN"
```
**Expected Output:** Match found containing `IXSCAN` and zero `COLLSCAN`.

---

## 6. Automated One-Command Verification

To run an automated test across all core endpoints directly from the terminal (using standard Python libraries):

```powershell
python -c "import urllib.request; endpoints = ['/health', '/employees', '/attendance', '/analytics/departments/summary?month=2026-07', '/analytics/leaderboard/late?month=2026-07', '/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-10']; [urllib.request.urlopen(f'http://127.0.0.1:8000{ep}') for ep in endpoints]; print('ALL ENDPOINTS RESPONDED WITH HTTP 200 OK!')"
```
*Expected Output:*
```text
ALL ENDPOINTS RESPONDED WITH HTTP 200 OK!
```

To verify that the database explain plans use index scans (`IXSCAN`) and no collection scans (`COLLSCAN`):
```powershell
python -c "import urllib.request, json; data = json.loads(urllib.request.urlopen('http://127.0.0.1:8000/admin/explain/department_trend?department=Engineering&from=2026-07-06&to=2026-07-10').read()); s = json.dumps(data['explain']); assert 'IXSCAN' in s and 'COLLSCAN' not in s; print('EXPLAIN PLAN VERIFIED: USES IXSCAN WITHOUT COLLSCAN!')"
```
*Expected Output:*
```text
EXPLAIN PLAN VERIFIED: USES IXSCAN WITHOUT COLLSCAN!
```

---

## 7. Troubleshooting

1. **MongoDB Atlas Authentication Failure (`bad auth : authentication failed`)**:
   - Check **Database Access** in MongoDB Atlas dashboard.
   - Ensure the user specified in `MONGO_URI` exists and that the password matches.
   - Ensure the user has the role **"Read and write to any database"** (or `readWriteAnyDatabase`).
   - If your password contains special characters like `@`, `:`, `/`, or `%`, ensure they are URL-encoded.

2. **MongoDB Atlas Connection Timeout (`ServerSelectionTimeoutError`)**:
   - In MongoDB Atlas, navigate to **Security** -> **Network Access**.
   - Add your current IP address or add `0.0.0.0/0` (allow access from anywhere) to the IP Access List.

3. **503 Service Unavailable on `/health`**:
   - MongoDB is not running or unreachable from the current connection string.
   - For local MongoDB, verify the Windows service is running: `Get-Service MongoDB`.

4. **Port 8000 conflict (`[WinError 10013]`)**:
   - Another process (or a previous Uvicorn instance) is already using port 8000.
   - Either terminate the previous instance or run on another port:
     ```powershell
     uvicorn app.main:app --port 8001 --reload
     ```

5. **Restoring Clean Sample Database**:
   - Reset your database to the 9 original sample records anytime:
     ```powershell
     python sample_seed.py
     ```
