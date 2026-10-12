# DECISIONS.md

1. **Indexes.** `employees`:
   - unique `emp_code`: identity, safe concurrent creates.
   - `(department, emp_code)`: filtered list, department lookups.
   - `joined_on`: the summary's "joined by month end" filter.

   `attendance_logs`:
   - unique `(emp_code, date)`: one record per day; punch-in/out, PATCH, monthly and every `$lookup` from employees.
   - `(date desc, emp_code)`: list sort, month range scans.

   I rejected an index on `status`: five values, not selective.

2. **Punch-in race.** Both requests compute the same date and call `insert_one`. The unique `(emp_code, date)` index lets one through. The other gets `DuplicateKeyError`, which I return as 409. There is no check-then-insert.

3. **Ties.** `$rank` gives equal totals the same rank and skips the next (1, 1, 3). I filter `rank <= limit` after ranking, so everyone tied at the cutoff is returned, even beyond `limit`. Ties are ordered by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (`joined_on` <= month end) and looks up their logs. `$unwind` keeps empty arrays, and headcount counts distinct `emp_code`s.

5. **100x data.** Keep a per-employee daily summary collection, updated on each punch/correction, so analytics read pre-aggregated documents instead of raw logs.
