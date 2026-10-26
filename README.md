# ECHO

> **Your data is wrong. ECHO finds out why.**

You write a JOIN. The query runs. No errors, all green. But total sales just jumped from 1 million to 3 million. Where did the extra 2 million come from?

ECHO is a debugging tool for **silent SQL bugs**: duplicated rows, lost rows and inflated totals that SQL never complains about. It splits a query into steps, counts the rows after every step, finds the first step where things go wrong, explains why in plain words, and **proves** that its proposed fix works.

*DEPI Graduation Project.*

---

## Table of Contents

- [The Story](#the-story)
- [What Makes ECHO Different](#what-makes-echo-different)
- [How It Works](#how-it-works)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Data Layers](#data-layers)
- [Supported Bug Types](#supported-bug-types)
- [Getting Started](#getting-started)
- [Usage](#usage)
- [Benchmark and Quality Gate](#benchmark-and-quality-gate)
- [Team and Roles](#team-and-roles)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)

---

## The Story

Meet Sara, a data engineer. She joins the `customers` table with the `orders` table. The query finishes without a single error, but the numbers look strange. So she opens the query and checks it step by step, by hand, for hours.

This happens to every data engineer, all the time. SQL does exactly what you wrote, even when what you wrote is slightly wrong.

**Why the name ECHO?** A mistake is like a shout in a canyon. It starts in one place (a bad JOIN) and its echo travels into every table and report after it. ECHO follows the echo back to the original shout.

### The example we use everywhere

| Step | Operation | Rows | Status |
|------|-----------|------|--------|
| 1 | `orders` (SELECT) | 10,000 | OK |
| 2 | JOIN `customers` (many-to-one) | 10,000 | OK |
| 3 | JOIN `customer_addresses` (key repeats) | **30,000** | **First suspicious step** |
| 4 | GROUP BY month, `SUM(amount)` | total = **3.0M** | Repeats the damage |

Each customer has 3 address rows, so every order is copied 3 times at step 3. The fix is to keep one address per customer before the JOIN, which brings the result back to 10,000 rows and a total of 1,000,000.

> The numbers above come from our example scenario. Real benchmark numbers are published only after testing.

---

## What Makes ECHO Different

Other approaches tell you *that* something is wrong. ECHO shows **where it started**, **why it happened**, and **proves the fix**.

- **It debugs the thinking, not only the data.** Not "something is wrong", but "step 3 turned 10,000 rows into 30,000 because the key repeats in table B".
- **It proves its own fixes.** Before and after row counts on a safe copy. No blind trust.
- **It has a scoreboard.** We break data on purpose with known bugs and measure how many ECHO catches.
- **It protects itself.** If a code change makes ECHO worse at catching old bugs, the CI/CD blocks the change.
- **It remembers.** Every solved bug is saved and becomes a test forever.

### How ECHO compares

| Approach | Finds the bad step | Explains with evidence | Tests the fix | Remembers cases | Scored benchmark |
|----------|--------------------|------------------------|---------------|-----------------|------------------|
| Debugging by hand | Slow, manual | By hand | By hand | No | No |
| Rule tests (dbt tests, Great Expectations) | No, says a rule failed | No | No | No | No |
| sql-autopsy (open source) | Yes, row counts per CTE | Partly | Not found | Not found | Not found |
| sqlsure (open source) | Before the query runs | Partly | Not found | Not found | Not found |
| **ECHO (our goal)** | Yes | Yes | Yes | Yes | Yes |

> This table is based on a short look at each tool's public description. "Not found" means we did not see it in the description, not that it is impossible. The ECHO row shows our goals. **Verify before presenting.**

---

## How It Works

ECHO runs in four stages:

```
1. ANALYZE   ->  2. DECIDE   ->  3. STORE   ->  4. SHOW + GUARD
```

1. **Analyze:** `sqlglot` splits the query into steps. `DuckDB` runs every step on a data sample and counts the rows.
2. **Decide:** Python rules find the bug. The LLM explains it in plain words. `DuckDB` tests the proposed fix.
3. **Store:** `PostgreSQL` keeps cases and results. `dbt` builds the Bronze, Silver and Gold layers. The benchmark holds known bugs and scores.
4. **Show + Guard:** `FastAPI` serves the results, `Streamlit` shows the dashboard, and `GitHub Actions` runs the CI/CD quality gate.

**Design rule:** detection uses simple, deterministic rules so results can be measured. **The LLM only explains. It never decides.**

Kafka and Spark are left out on purpose, because ECHO works on small samples. Airflow can be added later to re-run old cases on a schedule.

---

## Tech Stack

| Layer | Tool | Purpose |
|-------|------|---------|
| Parsing | [sqlglot](https://github.com/tobymao/sqlglot) | Split a query into steps |
| Execution | [DuckDB](https://duckdb.org) | Run every step, count rows, test fixes |
| Detection | Python | Rule-based bug detectors |
| Explanation | LLM | Plain-words explanation of the evidence |
| Storage | [PostgreSQL](https://www.postgresql.org) | Cases, steps and results |
| Modeling | [dbt](https://www.getdbt.com) | Bronze, Silver, Gold layers |
| API | [FastAPI](https://fastapi.tiangolo.com) | Serve investigations and cases |
| Dashboard | [Streamlit](https://streamlit.io) | Step trace, report, scoreboard |
| CI/CD | [GitHub Actions](https://github.com/features/actions) | Tests and the quality gate |
| Packaging | Docker | Build and deploy |

---

## Project Structure

The project has four layers. Data flows from top to bottom, and the last layer protects everything above it.

```
INPUT            Your SQL query + a small safe sample of data
   |
ECHO ENGINE      Parser -> Step runner -> Detectors -> Explainer
   |
STORAGE + APP    Bug library, Case store (PostgreSQL), dbt + DuckDB, API + dashboard
   |
CI/CD            Tests -> Quality gate -> Build -> Deploy (staging, then live)
```

Suggested repository layout (adjust to match what the team creates):

```
echo/
├── engine/
│   ├── parser/          # sqlglot: split a query into steps
│   ├── runner/          # DuckDB: run each step, record rows and sums
│   ├── detectors/       # rule-based bug detectors
│   ├── explainer/       # LLM prompts and fix proof
│   └── fixes/           # rule-based fix generators
├── api/                 # FastAPI app
├── dashboard/           # Streamlit pages
├── dbt/                 # bronze, silver, gold models
├── benchmark/           # known bugs, injection scripts, expected answers
├── data/                # sample data generator
├── db/                  # PostgreSQL schema and migrations
├── tests/               # unit and integration tests
├── .github/workflows/   # CI and quality gate
├── docker-compose.yml
├── requirements.txt
└── README.md
```

---

## Data Layers

Every run of ECHO is saved. dbt turns the raw logs into clean tables, and then into the scoreboard that shows how well ECHO works.

| Layer | Content |
|-------|---------|
| **Bronze** | Raw run logs |
| **Silver** | Clean cases and steps |
| **Gold** | The scoreboard (detection rate, false alarms, right step, fix pass rate) |

---

## Bronze Schema

<p align="center">
  <img src="bronze.schema.png" alt="Star Schema" width="100%">
</p>

## Supported Bug Types

| Bug type | What happens |
|----------|--------------|
| **JOIN fan-out** | A key repeats in the joined table, so rows are copied |
| **Inflated totals** | Sums grow after a JOIN without a valid reason |
| **Lost rows** | A JOIN (for example a LEFT JOIN pattern) drops rows |
| **NULL join keys** | NULL keys fail to match and silently disappear |
| **Key type mismatch** | The two sides of the key have different types |

---

## Getting Started

> **Note:** the commands below describe the intended setup. Update them as the project is built.

### Prerequisites

- Python 3.11 or newer
- Docker and Docker Compose
- Git

### Installation

```bash
# 1. Clone the repository
git clone https://github.com/<your-org>/echo.git
cd echo

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Start PostgreSQL
docker compose up -d postgres

# 5. Create the sample data (10,000 orders, 3 addresses per customer)
python -m data.generate_sample

# 6. Apply the database schema
python -m db.migrate
```

### Configuration

Create a `.env` file in the project root:

```env
DATABASE_URL=postgresql://echo:echo@localhost:5432/echo
LLM_API_KEY=your-api-key-here
```

Never commit the `.env` file.

---

## Usage

### Run the API

```bash
uvicorn api.main:app --reload
```

Open the interactive docs at `http://localhost:8000/docs`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/investigate` | Send a query and a data sample, get an investigation back |
| `GET` | `/cases` | List saved cases |

### Run the dashboard

```bash
streamlit run dashboard/app.py
```

The dashboard has three screens:

- **Step trace:** the row count after every step, with the suspicious step marked.
- **Investigation report:** the finding, the evidence, a plain explanation, the proposed fix, and the before and after proof.
- **Benchmark scoreboard:** how well ECHO finds each type of known bug.

### Example investigation (Python)

```python
from engine.runner import run_steps
from engine.parser import split_query
from engine.detectors import detect

sql = open("examples/monthly_sales.sql").read()

steps = split_query(sql)
trace = run_steps(steps, sample="data/sample.duckdb")
finding = detect(trace)

print(finding)   # e.g. JOIN fan-out at step 3
```

---

## Benchmark and Quality Gate

We break data on purpose with known bugs and measure how many ECHO catches. These are the metrics we track:

| Metric | Meaning |
|--------|---------|
| **Detection rate** | How many known bugs ECHO catches |
| **False alarms** | How often ECHO flags a query that is fine |
| **Right step** | How often ECHO points to the correct step |
| **Fix pass rate** | How often the proposed fix passes all checks |

### Fix checks

Every proposed fix must pass:

- Row count equals the orders count
- Total equals the orders total
- No duplicate `order_id`

### CI quality gate

Every pull request runs:

1. Unit tests
2. A subset of the benchmark
3. The quality gate, which **blocks the merge if detection drops below the baseline**

```bash
# Run the tests locally
pytest

# Run the benchmark subset locally
python -m benchmark.run --subset
```

Every solved case can be saved to the benchmark and becomes a permanent test.

---

## Team and Roles

| Member | Focus area |
|--------|-----------|
| Member 1 | Parser and Step Runner |
| Member 2 | Detectors and Benchmark |
| Member 3 | Explainer and Fix Proof |
| Member 4 | Data, dbt and PostgreSQL |
| Member 5 | API, Dashboard and CI/CD |

*Replace "Member 1 to 5" with the real team names.*

---

## Roadmap

- [ ] **Week 1 – Foundation:** repo setup, sqlglot parser, bug taxonomy, sample data, PostgreSQL schema
- [ ] **Week 2 – Engine:** step runner, fan-out and inflated-totals detectors, fix generator and proof, Bronze loader, API skeleton, CI
- [ ] **Week 3 – Detectors, benchmark, storage:** remaining detectors, benchmark set, LLM explainer, dbt models, dashboard pages
- [ ] **Week 4 – Guard and demo:** end-to-end test, baseline, bug library, quality gate, staging deploy, docs and demo

Project tasks are tracked in the Notion board **ECHO – 4-Week Task Plan**.

---

## Contributing

1. Create a branch from `main`: `git checkout -b feature/short-description`
2. Make your change and add tests.
3. Run `pytest` and the benchmark subset locally.
4. Open a pull request. The quality gate must pass before merging.

Direct pushes to `main` are not allowed.

---

## License

Add a license for the project here (for example MIT), or state that it is for educational use only.

---

<sub>ECHO | DEPI Graduation Project</sub>
