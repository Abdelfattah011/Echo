import altair as alt
import html
import pandas as pd
import requests
import streamlit as st

st.set_page_config(
    page_title="ECHO | SQL Intelligence",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
)

DEFAULT_SQL = """SELECT
    o.status,
    SUM(o.amount) AS total
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
JOIN customer_addresses a ON c.customer_id = a.customer_id
GROUP BY o.status"""

STEPS_PER_ROW = 4


def format_number(value):
    if value is None or isinstance(value, bool):
        return "—"
    try:
        return f"{value:,.0f}"
    except (TypeError, ValueError, OverflowError):
        return "—"


def safe_text(value, fallback=""):
    if value is None or not str(value).strip():
        return fallback
    return html.escape(str(value))


st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Inter:wght@400;500;600;700;800&display=swap');

:root {
    --bg: #090b12;
    --panel: #111520;
    --panel-2: #141927;
    --border: #232a3c;
    --border-soft: rgba(255, 255, 255, 0.06);
    --text: #edf1ff;
    --muted: #8b95ad;
    --purple: #9b8cff;
    --cyan: #58e6d9;
    --red: #ff637d;
    --green: #6ce8b2;
    --radius: 18px;
    --shadow: 0 10px 30px -12px rgba(0, 0, 0, 0.6);
}

/* ---------- Base ---------- */
html, body, .stApp {
    font-family: 'Inter', sans-serif;
    scroll-behavior: smooth;
}

.stApp {
    background:
        radial-gradient(ellipse at 12% 0%, #21183b 0%, transparent 32%),
        radial-gradient(ellipse at 95% 12%, #102c35 0%, transparent 27%),
        var(--bg);
    color: var(--text);
}

header[data-testid="stHeader"] {
    background: transparent;
}

/* المسافة بين المحتوى والـ sidebar */
.block-container {
    max-width: 1400px;
    padding: 2.5rem 3.5rem 4rem 3.5rem;
}

@media (max-width: 900px) {
    .block-container {
        padding: 1.5rem 1.25rem 3rem 1.25rem;
    }
}

/* ---------- Sidebar ---------- */
[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0e1220 0%, #0a0d16 100%);
    border-right: 1px solid var(--border);
}

[data-testid="stSidebarContent"],
[data-testid="stSidebarUserContent"] {
    padding: 1.75rem 1.5rem 2rem 1.5rem;
}

[data-testid="stSidebar"] h4 {
    color: var(--muted) !important;
    font: 500 10.5px 'DM Mono', monospace !important;
    letter-spacing: 2px;
    text-transform: uppercase;
    margin: 1.4rem 0 0.7rem;
}

[data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
    color: var(--muted);
    line-height: 1.7;
}

[data-testid="stSidebar"] hr {
    margin: 1.2rem 0;
}

.brand {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 28px;
    font-weight: 800;
    letter-spacing: -0.5px;
    color: var(--text);
    margin-bottom: 4px;
}

.brand .mark {
    background: linear-gradient(120deg, #a99aff, #62e9dc);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
}

.nav-item {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    margin-bottom: 6px;
    border: 1px solid var(--border-soft);
    border-radius: 10px;
    background: rgba(255, 255, 255, 0.02);
    color: #c3cbe0;
    font: 500 11.5px 'DM Mono', monospace;
    letter-spacing: 0.8px;
    transition: all 0.2s ease;
}

.nav-item:hover {
    border-color: #3a4566;
    background: rgba(155, 140, 255, 0.08);
    transform: translateX(2px);
}

.nav-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--cyan);
    box-shadow: 0 0 8px var(--cyan);
    flex-shrink: 0;
}

/* ---------- Typography ---------- */
h1, h2, h3 {
    color: var(--text) !important;
    letter-spacing: -0.7px;
}

p, label {
    color: var(--muted);
}

.eyebrow {
    color: var(--cyan);
    font: 11px 'DM Mono', monospace;
    letter-spacing: 2.5px;
    text-transform: uppercase;
    margin-bottom: 14px;
}

.hero-title {
    font-size: clamp(36px, 5vw, 58px);
    font-weight: 800;
    line-height: 1.08;
    letter-spacing: -2.5px;
    margin: 0;
    color: #f4f5ff;
}

.hero-title span {
    background: linear-gradient(100deg, #a99aff, #62e9dc);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
}

.hero-subtitle {
    color: #9ba6bf;
    font-size: 15px;
    line-height: 1.85;
    margin-top: 16px;
    max-width: 680px;
}

.status-pill {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    border: 1px solid #285b51;
    background: linear-gradient(120deg, #0f2820, #102720);
    color: var(--green);
    padding: 8px 14px;
    border-radius: 30px;
    font: 11px 'DM Mono', monospace;
    letter-spacing: 1px;
    box-shadow: 0 0 18px -6px rgba(108, 232, 178, 0.4);
}

.status-pill::before {
    content: "";
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--green);
    animation: pulse 2s ease-in-out infinite;
}

@keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.4; transform: scale(0.8); }
}

@keyframes fadeUp {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
}

.section-heading {
    font-size: 20px;
    font-weight: 700;
    letter-spacing: -0.3px;
    margin: 32px 0 6px;
    color: var(--text);
}

.section-subtitle {
    font-size: 13px;
    color: var(--muted);
    margin-bottom: 18px;
}

/* ---------- Panels ---------- */
.panel {
    background: linear-gradient(145deg, var(--panel-2), #10131d);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 24px;
    margin: 4px 0 20px;
    box-shadow: var(--shadow);
    animation: fadeUp 0.5s ease both;
}

.panel h3 {
    font-size: 18px;
    margin: 10px 0 14px;
}

.panel p {
    margin: 0 0 8px;
    font-size: 13px;
    line-height: 1.6;
}

.panel hr {
    margin: 16px 0;
    border: none;
    border-top: 1px solid var(--border);
}

.panel-label {
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 1.6px;
    font: 10px 'DM Mono', monospace;
}

/* ---------- Finding ---------- */
.finding {
    background: linear-gradient(130deg, #301722, #191521 75%);
    border: 1px solid #a83e55;
    border-radius: var(--radius);
    padding: 28px;
    margin: 14px 0 26px;
    box-shadow: 0 14px 40px -18px rgba(255, 99, 125, 0.45);
    animation: fadeUp 0.5s ease both;
}

.finding-kicker {
    color: #ff8497;
    font: 11px 'DM Mono', monospace;
    letter-spacing: 2px;
}

.finding-title {
    font-size: 26px;
    font-weight: 800;
    margin: 12px 0;
    color: #fff0f2;
}

.finding-body {
    color: #cdbcc7;
    font-size: 14px;
    line-height: 1.9;
}

/* ---------- Step cards ---------- */
.step-card {
    background: linear-gradient(145deg, #131827, #10141f);
    border: 1px solid #293147;
    border-radius: 14px;
    padding: 18px 16px;
    min-height: 175px;
    margin-bottom: 14px;
    transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
    animation: fadeUp 0.5s ease both;
}

.step-card:hover {
    transform: translateY(-3px);
    box-shadow: 0 14px 30px -14px rgba(0, 0, 0, 0.7);
}

.step-card.bad {
    border-color: #a83e55;
    background: linear-gradient(145deg, #351923, #19131d);
}

.step-card.bad:hover {
    box-shadow: 0 14px 30px -14px rgba(255, 99, 125, 0.45);
}

.step-card.good {
    border-color: #245847;
}

.step-card.good:hover {
    box-shadow: 0 14px 30px -14px rgba(108, 232, 178, 0.3);
}

.step-number {
    font: 11px 'DM Mono', monospace;
    color: var(--muted);
    margin-bottom: 12px;
}

.step-card.bad .step-number {
    color: #ff8497;
}

.step-card.good .step-number {
    color: var(--green);
}

.step-name {
    font-size: 14px;
    font-weight: 700;
    margin-bottom: 8px;
    overflow-wrap: anywhere;
    color: var(--text);
}

.step-type {
    color: var(--muted);
    font: 10px 'DM Mono', monospace;
    letter-spacing: 0.8px;
    margin-bottom: 14px;
}

.step-rows {
    font-size: 24px;
    font-weight: 700;
    color: #f4f5ff;
}

.step-note {
    color: #b7c0d4;
    font-size: 11.5px;
    margin-top: 10px;
    line-height: 1.6;
}

/* ---------- Inputs ---------- */
div[data-testid="stTextArea"] textarea {
    background: #0b0e17;
    color: #c9d6ff;
    border: 1px solid #303951;
    border-radius: 14px;
    padding: 16px;
    font-family: 'DM Mono', monospace;
    font-size: 13px;
    line-height: 1.7;
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
}

div[data-testid="stTextArea"] textarea:focus {
    border-color: var(--purple);
    box-shadow: 0 0 0 3px rgba(155, 140, 255, 0.18);
}

div[data-testid="stTextInput"] input {
    background: #0b0e17;
    color: #c9d6ff;
    border: 1px solid #303951;
    border-radius: 10px;
    font-family: 'DM Mono', monospace;
    font-size: 12.5px;
    padding: 10px 12px;
}

div[data-testid="stTextInput"] input:focus {
    border-color: var(--purple);
    box-shadow: 0 0 0 3px rgba(155, 140, 255, 0.18);
}

/* ---------- Buttons ---------- */
.stButton > button {
    border-radius: 12px;
    min-height: 46px;
    font-weight: 700;
    letter-spacing: 0.2px;
    transition: all 0.2s ease;
}

.stButton > button[kind="primary"],
.stButton > button[data-testid="stBaseButton-primary"] {
    border: 1px solid #7565cb;
    background: linear-gradient(100deg, #7766dc, #4b82c8);
    color: white;
    box-shadow: 0 8px 22px -10px rgba(119, 102, 220, 0.8);
}

.stButton > button[kind="primary"]:hover,
.stButton > button[data-testid="stBaseButton-primary"]:hover {
    border-color: #a99aff;
    filter: brightness(1.12);
    transform: translateY(-1px);
}

.stButton > button[kind="secondary"],
.stButton > button[data-testid="stBaseButton-secondary"] {
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid #303951;
    color: #c3cbe0;
}

.stButton > button[kind="secondary"]:hover,
.stButton > button[data-testid="stBaseButton-secondary"]:hover {
    border-color: #4a5778;
    background: rgba(255, 255, 255, 0.06);
    color: white;
}

/* لون الكلام جوه الأزرار (كان بيتغطى بـ p { color: muted }) */
.stButton > button p,
.stButton > button span,
.stButton > button div {
    color: inherit !important;
    font-weight: 700;
}

.stButton > button[kind="primary"],
.stButton > button[data-testid="stBaseButton-primary"] {
    color: #ffffff !important;
}

/* ---------- Metrics ---------- */
div[data-testid="stMetric"] {
    background: linear-gradient(145deg, #131827, #10141f);
    border: 1px solid #293147;
    padding: 20px 22px;
    border-radius: 14px;
    box-shadow: var(--shadow);
    transition: border-color 0.2s ease, transform 0.2s ease;
}

div[data-testid="stMetric"]:hover {
    border-color: #3a4566;
    transform: translateY(-2px);
}

div[data-testid="stMetricLabel"] {
    color: #a0abc2;
}

div[data-testid="stMetricValue"] {
    color: #f4f5ff;
    font-weight: 700;
}

/* ---------- Misc ---------- */
details[data-testid="stExpander"] {
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid var(--border);
    border-radius: 14px;
}

div[data-testid="stAlert"] {
    border-radius: 14px;
}

hr {
    border-color: var(--border);
    margin: 1.5rem 0;
}

code {
    color: var(--cyan) !important;
}

::-webkit-scrollbar {
    width: 8px;
    height: 8px;
}

::-webkit-scrollbar-thumb {
    background: #2a3248;
    border-radius: 8px;
}

::-webkit-scrollbar-thumb:hover {
    background: #3a4566;
}

footer {
    visibility: hidden;
}
</style>
""", unsafe_allow_html=True)


# SIDEBAR
with st.sidebar:
    st.markdown(
        "<div class='brand'><span class='mark'>◈</span> ECHO</div>",
        unsafe_allow_html=True,
    )
    st.caption("DATA QUALITY INTELLIGENCE")
    st.divider()

    st.markdown("#### CONNECTION")
    api_url = st.text_input(
        "API endpoint",
        "http://127.0.0.1:8000",
    ).strip().rstrip("/")

    st.markdown("#### WORKSPACE")
    st.markdown("""
    <div class="nav-item"><span class="nav-dot"></span>SQL INVESTIGATION</div>
    <div class="nav-item"><span class="nav-dot"></span>JOIN ANALYSIS</div>
    <div class="nav-item"><span class="nav-dot"></span>TRANSFORMATION TRACE</div>
    """, unsafe_allow_html=True)

    st.divider()
    st.markdown("#### ABOUT ECHO")
    st.caption(
        "Find the step where your SQL transformations "
        "start producing suspicious results."
    )
    st.caption("ECHO • DEPI CAPSTONE PROJECT")


# HEADER
header_left, header_right = st.columns([4, 1], gap="large")

with header_left:
    st.markdown("""
    <div class="eyebrow">DATA ENGINEERING / INTELLIGENCE LAYER</div>
    <div class="hero-title">Your data is wrong.<br>
    <span>Find out why.</span></div>
    <div class="hero-subtitle">
    Investigate SQL transformations, trace suspicious steps,
    and understand where your data starts going wrong.
    </div>
    """, unsafe_allow_html=True)

with header_right:
    st.write("")
    st.markdown(
        '<div style="text-align:right;">'
        '<div class="status-pill">INVESTIGATION READY</div>'
        '</div>',
        unsafe_allow_html=True,
    )

st.write("")
st.divider()


# SQL WORKSPACE
st.markdown(
    '<div class="section-heading">01 / Query workspace</div>',
    unsafe_allow_html=True,
)
st.markdown(
    '<div class="section-subtitle">Paste your transformation SQL and let ECHO investigate it.</div>',
    unsafe_allow_html=True,
)

sql_col, info_col = st.columns([3, 1], gap="large")

with sql_col:
    sql = st.text_area(
        "SQL query",
        value=DEFAULT_SQL,
        height=280,
        label_visibility="collapsed",
        key="sql_editor",
    )

with info_col:
    st.markdown("""
    <div class="panel">
      <div class="panel-label">INVESTIGATION TARGET</div>
      <h3>SQL transformations</h3>
      <p>• JOIN fan-out</p>
      <p>• Unexpected row growth</p>
      <p>• Aggregation anomalies</p>
      <p>• Suspicious intermediate steps</p>
      <hr>
      <div class="panel-label">ENGINE</div>
      <p style="margin-top:8px;"><b style="color:#edf1ff;">ECHO Analysis API</b></p>
      <p>FastAPI · SQL diagnostics</p>
    </div>
    """, unsafe_allow_html=True)

st.write("")
run_col, clear_col, spacer = st.columns([1.3, 1, 4], gap="medium")

with run_col:
    investigate = st.button(
        "Investigate SQL",
        type="primary",
        use_container_width=True,
    )

with clear_col:
    if st.button("Clear results", use_container_width=True):
        st.session_state.pop("result", None)
        st.session_state.pop("api_error", None)
        st.rerun()


# API REQUEST
if investigate:
    if not sql.strip():
        st.warning("Add a SQL query before starting the investigation.")
        st.session_state.pop("result", None)
    else:
        try:
            with st.spinner("ECHO is investigating your query..."):
                response = requests.post(
                    f"{api_url}/investigate",
                    json={"sql": sql},
                    timeout=60,
                )
                response.raise_for_status()
                payload = response.json()

            if not isinstance(payload, dict):
                raise ValueError("The API response must be a JSON object.")

            st.session_state["result"] = payload
            st.session_state.pop("api_error", None)

        except requests.RequestException as error:
            st.session_state.pop("result", None)
            st.session_state["api_error"] = (
                f"Could not reach the API: {error}"
            )

        except (ValueError, TypeError) as error:
            st.session_state.pop("result", None)
            st.session_state["api_error"] = (
                f"Unexpected API response: {error}"
            )

if st.session_state.get("api_error"):
    st.error(st.session_state["api_error"])


# RESULTS
result = st.session_state.get("result")

if result:
    steps = result.get("steps") or []
    steps = [step for step in steps if isinstance(step, dict)]

    first = result.get("first_suspicious_step")
    bug_type = result.get("bug_type")
    explanation = result.get("explanation") or ""

    st.write("")
    st.divider()
    st.markdown(
        '<div class="section-heading">02 / Investigation report</div>',
        unsafe_allow_html=True,
    )
    st.write("")

    if result.get("is_demo"):
        st.warning(
            "DEMO MODE — These results are demo output, not a real "
            "investigation by the full ECHO engine."
        )

    suspicious_count = sum(
        bool(step.get("suspicious", False))
        for step in steps
    )

    k1, k2, k3, k4 = st.columns(4, gap="medium")

    k1.metric("Pipeline steps", len(steps))

    k2.metric(
        "Suspicious steps",
        suspicious_count,
        delta="Needs attention" if suspicious_count else "No flags",
        delta_color="inverse" if suspicious_count else "normal",
    )

    k3.metric(
        "First suspicious step",
        str(first) if first is not None else "—",
    )

    start_rows = steps[0].get("rows") if steps else None
    k4.metric("Initial row count", format_number(start_rows))

    # FINDING
    if bug_type:
        st.markdown(f"""
        <div class="finding">
          <div class="finding-kicker">ANOMALY DETECTED</div>
          <div class="finding-title">{safe_text(bug_type)}</div>
          <div class="finding-body">
            First suspicious step:
            <b>{safe_text(first, "Not specified")}</b><br>
            {safe_text(explanation, "No explanation was returned by the API.")}
          </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.write("")
        st.info(
            "The API did not report a bug type. "
            "This does not guarantee that the SQL is correct."
        )
        if explanation:
            st.caption(explanation)

    # PIPELINE
    st.markdown(
        '<div class="section-heading">03 / Transformation pipeline</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="section-subtitle">Follow the row count and inspect each step.</div>',
        unsafe_allow_html=True,
    )

    if steps:
        # صفوف من 4 كروت بدل ما الكروت تتكدس تحت بعض
        for row_start in range(0, len(steps), STEPS_PER_ROW):
            trace_columns = st.columns(STEPS_PER_ROW, gap="medium")
            row_steps = steps[row_start:row_start + STEPS_PER_ROW]

            for offset, step in enumerate(row_steps):
                index = row_start + offset
                with trace_columns[offset]:
                    suspicious = bool(step.get("suspicious", False))
                    css_class = "bad" if suspicious else "good"
                    status = "FLAGGED" if suspicious else "STEP OK"

                    rows_text = format_number(step.get("rows"))
                    step_id = safe_text(step.get("step_id"), str(index + 1))
                    step_name = safe_text(step.get("name"), "Unnamed step")
                    step_type = safe_text(
                        step.get("step_type"),
                        "TRANSFORMATION",
                    )
                    note = safe_text(step.get("note"), "")

                    st.markdown(f"""
                    <div class="step-card {css_class}">
                      <div class="step-number">{status} · {step_id}</div>
                      <div class="step-name">{step_name}</div>
                      <div class="step-type">{step_type}</div>
                      <div class="step-rows">{rows_text}</div>
                      <div class="step-type">ROWS</div>
                      <div class="step-note">{note}</div>
                    </div>
                    """, unsafe_allow_html=True)

        st.write("")

        # CHART — only use steps with numeric row counts
        valid_steps = [
            step for step in steps
            if isinstance(step.get("rows"), (int, float))
            and not isinstance(step.get("rows"), bool)
        ]

        if valid_steps:
            chart_df = pd.DataFrame(valid_steps)

            chart_df["label"] = chart_df.apply(
                lambda row: (
                    f"{row.get('step_id', '')}. "
                    f"{row.get('name') or 'Step'}"
                ),
                axis=1,
            )

            if "suspicious" not in chart_df.columns:
                chart_df["suspicious"] = False

            chart_df["suspicious"] = (
                chart_df["suspicious"].fillna(False)
            )

            chart = (
                alt.Chart(chart_df)
                .mark_bar(
                    cornerRadiusTopLeft=6,
                    cornerRadiusTopRight=6,
                    size=46,
                )
                .encode(
                    x=alt.X(
                        "label:N",
                        sort=None,
                        title=None,
                        axis=alt.Axis(
                            labelAngle=-25,
                            labelColor="#a0abc2",
                            labelPadding=8,
                        ),
                    ),
                    y=alt.Y(
                        "rows:Q",
                        title="Row count",
                        axis=alt.Axis(
                            labelColor="#a0abc2",
                            titleColor="#8b95ad",
                        ),
                    ),
                    color=alt.condition(
                        "datum.suspicious == true",
                        alt.value("#ff637d"),
                        alt.value("#8b7cf6"),
                    ),
                    tooltip=[
                        alt.Tooltip("step_id:N", title="Step ID"),
                        alt.Tooltip("name:N", title="Name"),
                        alt.Tooltip(
                            "rows:Q",
                            title="Rows",
                            format=",",
                        ),
                    ],
                )
                .properties(height=340, padding={"left": 10, "right": 20, "top": 14, "bottom": 10})
                .configure(
                    background="transparent",
                    view=alt.ViewConfig(stroke=None),
                )
                .configure_axis(
                    gridColor="#1d2335",
                    domainColor="#394156",
                    tickColor="#394156",
                )
            )

            st.altair_chart(chart, use_container_width=True)
        else:
            st.caption(
                "Row-count chart will appear when actual numeric "
                "row counts are returned by the API."
            )

    else:
        st.info("The API returned no transformation steps.")

    st.write("")
    with st.expander("Inspect raw API response"):
        st.json(result)

else:
    # EMPTY STATE
    st.markdown("""
    <div class="panel" style="text-align:center;padding:56px 24px;margin-top:28px;">
      <div style="font-size:44px;color:#9b8cff;">◈</div>
      <div style="font-size:22px;font-weight:700;margin:14px 0 10px;color:#edf1ff;">
        Your investigation starts here
      </div>
      <div style="color:#8b95ad;font-size:13.5px;line-height:1.9;max-width:520px;margin:0 auto;">
        Submit a SQL query to inspect the API response,
        visualize transformation steps, and review detected anomalies.
      </div>
    </div>
    """, unsafe_allow_html=True)


# FOOTER
st.markdown("""
<div style="border-top:1px solid #252c3e;padding-top:20px;
            margin-top:56px;color:#66718a;font-size:11px;
            font-family:'DM Mono',monospace;letter-spacing:1px;">
ECHO · SQL INTELLIGENCE · DEPI CAPSTONE
<span style="float:right;">TRACE THE BUG. PROVE THE FIX.</span>
</div>
""", unsafe_allow_html=True)