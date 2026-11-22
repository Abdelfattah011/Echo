"""Benchmark cases for the ECHO engine.

Every case is one SQL query plus the answer we already know:

* ``bug``  - which taxonomy bug the query contains (see bug_taxonomy.md), or ``"clean"``
             when the query is correct and ECHO should stay quiet.
* ``quick`` - part of the small subset that runs on every pull request.

The data is built by SETUP_SQL below. It is fully deterministic (no RANDOM()), so a
result can never change between two runs of the same code. Keep it that way: the CI
quality gate compares runs against a saved baseline.
"""

# --------------------------------------------------------------------------- #
# Deterministic in-memory dataset
# --------------------------------------------------------------------------- #
SETUP_SQL = """
CREATE TABLE customers AS
SELECT range AS customer_id,
       'Customer_' || range AS customer_name,
       CASE WHEN range % 2 = 0 THEN 'Active' ELSE 'Inactive' END AS status
FROM range(1000);

-- 3 addresses per customer: joining on customer_id multiplies rows (fan-out)
CREATE TABLE customer_addresses AS
SELECT c.customer_id,
       'Address_' || a.addr_id AS address_id,
       'Street ' || (c.customer_id * 10 + a.addr_id) AS address_line
FROM customers c
CROSS JOIN (SELECT * FROM range(1, 4) AS t(addr_id)) AS a;

-- exactly one row for every EVEN customer (1:1, unique key)
CREATE TABLE customer_loyalty AS
SELECT range AS customer_id,
       CASE WHEN (range / 2) % 2 = 0 THEN 'Gold' ELSE 'Silver' END AS tier,
       range * 3 AS points
FROM range(0, 1000, 2);

CREATE TABLE employees AS
SELECT range AS employee_id, 'Emp_' || range AS employee_name
FROM range(50);

-- 20% of orders have a NULL sales_rep_id (the NULL-join-key bug)
CREATE TABLE orders AS
SELECT range AS order_id,
       (range * 7) % 1000 AS customer_id,
       CASE WHEN range % 5 = 0 THEN NULL ELSE range % 50 END AS sales_rep_id,
       CAST(((range * 37) % 500) + 1 AS DECIMAL(10,2)) AS amount,
       CASE WHEN range % 10 < 7 THEN 'Completed' ELSE 'Pending' END AS status
FROM range(1, 3001);

-- same ids as customers, but stored as VARCHAR (the key-type-mismatch bug)
CREATE TABLE customers_text AS
SELECT CAST(customer_id AS VARCHAR) AS customer_id, customer_name
FROM customers;
"""

# --------------------------------------------------------------------------- #
# Which ECHO findings count as "caught" for each taxonomy bug.
#
# ECHO reports a detector label (bug_type). The taxonomy has 5 bug types, so the
# mapping lives here, in one visible place. Rules for editing it:
#   * a label may only be listed for a bug if it genuinely describes that bug;
#   * never add a label just to raise a score.
# "Inflated Totals" is the arithmetic result of a fan-out, so the fan-out finding is
# the correct detection for it.
# --------------------------------------------------------------------------- #
ACCEPTED_LABELS = {
    "join_fanout": ["Possible JOIN Fan-out"],
    "inflated_totals": ["Possible JOIN Fan-out"],
    "left_join_row_loss": ["Severe Row Loss at JOIN", "Silent Data Drop / Empty Result Set"],
    "null_join_keys": ["Severe Row Loss at JOIN", "Silent Data Drop / Empty Result Set"],
    "key_type_mismatch": ["Join Key Type Mismatch"],
}

# Informational notices, not bug findings. They do not count as false positives
# on clean queries.
INFORMATIONAL_LABELS = [
    "Performance Bottleneck / Heavy Step Warning",
    "Complex Aggregation & Join Workflow",
]

BUG_TITLES = {
    "join_fanout": "JOIN Fan-out",
    "inflated_totals": "Inflated Totals",
    "left_join_row_loss": "Lost Rows in a LEFT JOIN",
    "null_join_keys": "NULL Join Keys",
    "key_type_mismatch": "Key Type Mismatch",
}

# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #
CASES = [
    # ---- 1. JOIN Fan-out ----------------------------------------------------
    {"id": "fanout_01", "bug": "join_fanout", "quick": True,
     "sql": "SELECT o.order_id, o.amount, a.address_line "
            "FROM orders o JOIN customer_addresses a ON o.customer_id = a.customer_id;"},
    {"id": "fanout_02", "bug": "join_fanout",
     "sql": "SELECT c.customer_id, c.customer_name, a.address_line "
            "FROM customers c JOIN customer_addresses a ON c.customer_id = a.customer_id;"},
    {"id": "fanout_03", "bug": "join_fanout",
     "sql": "SELECT o.order_id, o.status, a.address_id "
            "FROM orders o JOIN customer_addresses a ON o.customer_id = a.customer_id "
            "WHERE o.status = 'Completed';"},

    # ---- 2. Inflated Totals -------------------------------------------------
    {"id": "inflated_01", "bug": "inflated_totals", "quick": True,
     "sql": "SELECT SUM(o.amount) AS total_revenue "
            "FROM orders o JOIN customer_addresses a ON o.customer_id = a.customer_id;"},
    {"id": "inflated_02", "bug": "inflated_totals",
     "sql": "SELECT COUNT(*) AS total_orders "
            "FROM orders o JOIN customer_addresses a ON o.customer_id = a.customer_id;"},
    {"id": "inflated_03", "bug": "inflated_totals",
     "sql": "SELECT o.status, SUM(o.amount) AS revenue "
            "FROM orders o JOIN customer_addresses a ON o.customer_id = a.customer_id "
            "GROUP BY o.status;"},

    # ---- 3. Lost rows in a LEFT JOIN (WHERE on the right table) -------------
    {"id": "leftjoin_01", "bug": "left_join_row_loss", "quick": True,
     "sql": "SELECT c.customer_id, l.tier "
            "FROM customers c LEFT JOIN customer_loyalty l ON c.customer_id = l.customer_id "
            "WHERE l.tier = 'Gold';"},
    {"id": "leftjoin_02", "bug": "left_join_row_loss",
     "sql": "SELECT c.customer_id, l.points "
            "FROM customers c LEFT JOIN customer_loyalty l ON c.customer_id = l.customer_id "
            "WHERE l.points > 100;"},
    {"id": "leftjoin_03", "bug": "left_join_row_loss",
     "sql": "SELECT c.customer_id, c.customer_name, l.tier "
            "FROM customers c LEFT JOIN customer_loyalty l ON c.customer_id = l.customer_id "
            "WHERE l.tier = 'Silver';"},

    # ---- 4. NULL join keys --------------------------------------------------
    {"id": "nullkey_01", "bug": "null_join_keys", "quick": True,
     "sql": "SELECT o.order_id, e.employee_name "
            "FROM orders o JOIN employees e ON o.sales_rep_id = e.employee_id;"},
    {"id": "nullkey_02", "bug": "null_join_keys",
     "sql": "SELECT COUNT(*) AS orders_with_rep "
            "FROM orders o JOIN employees e ON o.sales_rep_id = e.employee_id;"},
    {"id": "nullkey_03", "bug": "null_join_keys",
     "sql": "SELECT o.order_id, o.amount, e.employee_name "
            "FROM orders o JOIN employees e ON o.sales_rep_id = e.employee_id "
            "WHERE o.amount > 100;"},

    # ---- 5. Key type mismatch ----------------------------------------------
    {"id": "typemis_01", "bug": "key_type_mismatch", "quick": True,
     "sql": "SELECT * FROM orders o JOIN customers_text c ON o.customer_id = c.customer_id;"},
    {"id": "typemis_02", "bug": "key_type_mismatch",
     "sql": "SELECT o.order_id, c.customer_name "
            "FROM orders o JOIN customers_text c ON o.customer_id = c.customer_id;"},
    {"id": "typemis_03", "bug": "key_type_mismatch",
     "sql": "SELECT o.status, COUNT(*) AS n "
            "FROM orders o JOIN customers_text c ON o.customer_id = c.customer_id "
            "GROUP BY o.status;"},

    # ---- Clean queries: ECHO must stay quiet --------------------------------
    {"id": "clean_01", "bug": "clean", "quick": True,
     "sql": "SELECT o.order_id, o.amount, c.customer_name "
            "FROM orders o JOIN customers c ON o.customer_id = c.customer_id;"},
    {"id": "clean_02", "bug": "clean", "quick": True,
     "sql": "SELECT c.customer_id, l.tier "
            "FROM customers c LEFT JOIN customer_loyalty l ON c.customer_id = l.customer_id;"},
    {"id": "clean_03", "bug": "clean",
     "sql": "SELECT status, COUNT(*) AS n, SUM(amount) AS revenue FROM orders GROUP BY status;"},
    {"id": "clean_04", "bug": "clean",
     "sql": "SELECT o.order_id, e.employee_name "
            "FROM orders o JOIN employees e ON o.sales_rep_id = e.employee_id "
            "WHERE o.sales_rep_id IS NOT NULL AND o.amount > 100;"},
    {"id": "clean_05", "bug": "clean",
     "sql": "SELECT o.order_id, o.amount, a.address_line "
            "FROM orders o JOIN (SELECT customer_id, MIN(address_line) AS address_line "
            "FROM customer_addresses GROUP BY customer_id) a ON o.customer_id = a.customer_id;"},
    {"id": "clean_06", "bug": "clean",
     "sql": "SELECT order_id, amount FROM orders WHERE amount > 100 AND status = 'Completed';"},
]