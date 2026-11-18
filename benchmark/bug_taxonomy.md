# Echo Engine - Bug Taxonomy
 
This document defines the 5 core SQL bug types analyzed, detected, and resolved by the Echo Engine framework.
 
---
 
## 1. JOIN Fan-out
 
- **Definition:** Occurs when joining two tables where the join key is not unique in the target/right table (a One-to-Many relationship). This causes the rows from the primary/left table to duplicate unexpectedly.
- **Example Query:**
```sql
  SELECT o.order_id, o.amount, a.address_line
  FROM orders o
  JOIN customer_addresses a ON o.customer_id = a.customer_id;
```
 
- **How it Shows Up in Numbers:** Sudden, unexpected spike in overall row count (`COUNT(*)`) while the distinct count of primary entities (e.g., `COUNT(DISTINCT order_id)`) remains unchanged.
---
 
## 2. Inflated Totals
 
- **Definition:** A direct financial and mathematical consequence of a JOIN Fan-out. Aggregate calculations such as `SUM()`, `AVG()`, or `COUNT()` operate on duplicated rows, leading to vastly inflated business metrics.
- **Example Query:**
```sql
  SELECT SUM(o.amount) AS total_revenue
  FROM orders o
  JOIN customer_addresses a ON o.customer_id = a.customer_id;
```
 
- **How it Shows Up in Numbers:** The aggregated value (e.g., total sales revenue) multiplies beyond the actual baseline totals, yielding incorrect financial reporting.
---
 
## 3. Lost Rows in a LEFT JOIN
 
- **Definition:** Intending to keep all records from the left table using a `LEFT JOIN`, but unintentionally converting it into an `INNER JOIN` by adding a `WHERE` clause that filters on non-null columns of the right table.
- **Example Query:**
```sql
  SELECT c.customer_id, o.order_id
  FROM customers c
  LEFT JOIN orders o ON c.customer_id = o.customer_id
  WHERE o.status = 'Completed';
```
 
- **How it Shows Up in Numbers:** Unexplained drop in total output rows. Unmatched entities from the left table (e.g., customers with no completed orders) vanish entirely instead of appearing with `NULL` values.
---
 
## 4. NULL Join Keys
 
- **Definition:** Occurs when the joining key columns contain `NULL` values. In SQL standard logic, `NULL = NULL` evaluates to `UNKNOWN` (not `TRUE`), causing database engines to silently discard these records during joins.
- **Example Query:**
```sql
  SELECT o.order_id, e.employee_name
  FROM orders o
  JOIN employees e ON o.sales_rep_id = e.employee_id;
```
 
- **How it Shows Up in Numbers:** Unexpected drop in row counts without explicit errors, as records with `NULL` join key values are silently omitted from the resulting dataset.
---
 
## 5. Key Type Mismatch
 
- **Definition:** Joining tables on columns with incompatible or differing data types (e.g., joining an `INTEGER` primary key with a `VARCHAR` foreign key). This forces implicit type conversion by the database query engine.
- **Example Query:**
```sql
  -- Where o.customer_id is INT and c.customer_id is VARCHAR
  SELECT *
  FROM orders o
  JOIN customers c ON o.customer_id = c.customer_id;
```
 
- **How it Shows Up in Numbers:** Can result in `0 rows` returned, unexpected mismatching, or noticeable query performance degradation due to implicit type casting overhead.
 
