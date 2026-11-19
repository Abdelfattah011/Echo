import altair as alt
import html
import pandas as pd
import requests
import streamlit as st

# إعداد الصفحة بمقاييس إنتربرايز واسعة
st.set_page_config(
    page_title="ECHO | Enterprise SQL Intelligence & Deep Analytics",
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
    if value is None or not str(value).setItem if hasattr(value, 'setItem') else not str(value).strip():
        return fallback
    return html.escape(str(value))


# --- تصميم الـ CSS المتقدم والـ Animations الكبيرة والمكتملة ---
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Mono:ital,wght@0,400;0,500;1,400&family=Inter:wght@300;400;500;600;700;800;900&display=swap');

:root {
    --bg-primary: #07090f;
    --bg-secondary: #0d111a;
    --panel-bg: #121722;
    --panel-hover: #171e2e;
    --border-color: #21293d;
    --border-glow: rgba(155, 140, 255, 0.35);
    --text-main: #f0f3ff;
    --text-muted: #8b95ad;
    --accent-purple: #9b8cff;
    --accent-cyan: #58e6d9;
    --accent-red: #ff637d;
    --accent-green: #6ce8b2;
    --radius-lg: 20px;
    --radius-md: 14px;
    --radius-sm: 10px;
    --shadow-subtle: 0 10px 30px -12px rgba(0, 0, 0, 0.7);
    --shadow-glow: 0 0 35px -8px rgba(155, 140, 255, 0.25);
}

html, body, .stApp {
    font-family: 'Inter', sans-serif;
    scroll-behavior: smooth;
    background-color: var(--bg-primary);
    color: var(--text-main);
}

.stApp {
    background:
        radial-gradient(circle at 10% 5%, rgba(33, 24, 59, 0.6) 0%, transparent 35%),
        radial-gradient(circle at 90% 15%, rgba(16, 44, 53, 0.5) 0%, transparent 30%),
        radial-gradient(circle at 50% 90%, rgba(20, 15, 35, 0.4) 0%, transparent 40%),
        var(--bg-primary);
}

header[data-testid="stHeader"] {
    background: transparent;
}

.block-container {
    max-width: 1450px;
    padding: 3rem 4rem 5rem 4rem;
}

@media (max-width: 900px) {
    .block-container {
        padding: 1.5rem 1.25rem 3rem 1.25rem;
    }
}

/* --- SIDEBAR DESIGN --- */
[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0b0f19 0%, #06080e 100%);
    border-right: 1px solid var(--border-color);
}

[data-testid="stSidebarContent"],
[data-testid="stSidebarUserContent"] {
    padding: 2rem 1.5rem;
}

.brand-container {
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: 30px;
    font-weight: 900;
    letter-spacing: -1px;
    color: var(--text-main);
    margin-bottom: 6px;
}

.brand-mark {
    background: linear-gradient(135deg, #a99aff 0%, #58e6d9 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    filter: drop-shadow(0 0 12px rgba(88, 230, 217, 0.4));
}

.sidebar-caption {
    font: 500 11px 'DM Mono', monospace;
    color: var(--text-muted);
    letter-spacing: 1.5px;
    text-transform: uppercase;
}

.nav-box {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 14px;
    margin-bottom: 8px;
    border: 1px solid rgba(255, 255, 255, 0.04);
    border-radius: var(--radius-sm);
    background: rgba(255, 255, 255, 0.015);
    color: #d0d7de;
    font: 500 12px 'DM Mono', monospace;
    letter-spacing: 0.8px;
    transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
}

.nav-box:hover {
    border-color: var(--accent-purple);
    background: rgba(155, 140, 255, 0.07);
    transform: translateX(4px);
    box-shadow: 0 0 20px rgba(155, 140, 255, 0.15);
}

.nav-dot-active {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--accent-cyan);
    box-shadow: 0 0 10px var(--accent-cyan);
    flex-shrink: 0;
    animation: pulse-dot 2s infinite;
}

@keyframes pulse-dot {
    0%, 100% { transform: scale(1); opacity: 1; }
    50% { transform: scale(1.4); opacity: 0.6; }
}

/* --- TYPOGRAPHY & HERO --- */
.eyebrow-tag {
    color: var(--accent-cyan);
    font: 600 11.5px 'DM Mono', monospace;
    letter-spacing: 3px;
    text-transform: uppercase;
    margin-bottom: 16px;
    display: inline-block;
    padding: 4px 10px;
    background: rgba(88, 230, 217, 0.08);
    border: 1px solid rgba(88, 230, 217, 0.2);
    border-radius: 20px;
}

.hero-main-title {
    font-size: clamp(40px, 5.5vw, 64px);
    font-weight: 900;
    line-height: 1.05;
    letter-spacing: -2.5px;
    margin: 0 0 18px 0;
    color: #ffffff;
}

.hero-main-title span {
    background: linear-gradient(120deg, #b4a7ff 0%, #58e6d9 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
}

.hero-description {
    color: var(--text-muted);
    font-size: 16px;
    line-height: 1.8;
    max-width: 720px;
    margin-bottom: 10px;
}

/* --- STATUS PILL ANIMATION --- */
.engine-status-badge {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    border: 1px solid rgba(108, 232, 178, 0.3);
    background: linear-gradient(135deg, rgba(15, 40, 32, 0.8), rgba(16, 39, 32, 0.9));
    color: var(--accent-green);
    padding: 10px 18px;
    border-radius: 30px;
    font: 600 12px 'DM Mono', monospace;
    letter-spacing: 1.2px;
    box-shadow: 0 0 25px -5px rgba(108, 232, 178, 0.35);
    animation: float-badge 3s ease-in-out infinite;
}

@keyframes float-badge {
    0%, 100% { transform: translateY(0); }
    50% { transform: translateY(-4px); }
}

.engine-status-badge::before {
    content: "";
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--accent-green);
    box-shadow: 0 0 10px var(--accent-green);
    animation: pulse 1.5s ease-in-out infinite;
}

@keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.3; transform: scale(0.7); }
}

/* --- PANELS & CARDS ANIMATION --- */
@keyframes fadeInUp {
    from { opacity: 0; transform: translateY(16px); }
    to { opacity: 1; transform: translateY(0); }
}

.enterprise-panel {
    background: linear-gradient(145deg, var(--panel-bg), var(--bg-secondary));
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    padding: 28px;
    margin: 10px 0 24px 0;
    box-shadow: var(--shadow-subtle);
    animation: fadeInUp 0.6s cubic-bezier(0.16, 1, 0.3, 1) both;
    position: relative;
    overflow: hidden;
}

.enterprise-panel::after {
    content: "";
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 2px;
    background: linear-gradient(90deg, transparent, rgba(155, 140, 255, 0.4), transparent);
}

.panel-header-label {
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 2px;
    font: 600 10.5px 'DM Mono', monospace;
    margin-bottom: 10px;
}

/* --- FINDING / BUG REPORT CARD --- */
.anomaly-alert-card {
    background: linear-gradient(135deg, #321623 0%, #17121f 100%);
    border: 1px solid rgba(255, 99, 125, 0.5);
    border-radius: var(--radius-lg);
    padding: 32px;
    margin: 16px 0 28px 0;
    box-shadow: 0 20px 50px -15px rgba(255, 99, 125, 0.35);
    animation: fadeInUp 0.5s ease both;
    position: relative;
}

.anomaly-alert-card::before {
    content: "";
    position: absolute;
    top: 0;
    left: 0;
    width: 4px;
    height: 100%;
    background: var(--accent-red);
    box-shadow: 0 0 15px var(--accent-red);
}

.alert-kicker {
    color: #ff8497;
    font: 600 12px 'DM Mono', monospace;
    letter-spacing: 2.5px;
    text-transform: uppercase;
}

.alert-title {
    font-size: 26px;
    font-weight: 800;
    margin: 12px 0 14px 0;
    color: #fff5f7;
    letter-spacing: -0.5px;
}

.alert-body {
    color: #dcd0dc;
    font-size: 14.5px;
    line-height: 1.85;
}

/* --- STEP TRACE CARDS --- */
.trace-step-card {
    background: linear-gradient(145deg, #141b2b, #0f1420);
    border: 1px solid #28334d;
    border-radius: var(--radius-md);
    padding: 22px 18px;
    min-height: 195px;
    margin-bottom: 16px;
    transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
    animation: fadeInUp 0.5s ease both;
    position: relative;
}

.trace-step-card:hover {
    transform: translateY(-5px);
    border-color: var(--accent-purple);
    box-shadow: 0 18px 35px -12px rgba(0, 0, 0, 0.8), var(--shadow-glow);
}

.trace-step-card.failed-step {
    border-color: var(--accent-red);
    background: linear-gradient(145deg, #361520, #161017);
}

.trace-step-card.failed-step:hover {
    box-shadow: 0 18px 35px -12px rgba(255, 99, 125, 0.4);
}

.trace-step-card.success-step {
    border-color: #215240;
}

.step-badge-top {
    font: 600 11px 'DM Mono', monospace;
    color: var(--text-muted);
    margin-bottom: 10px;
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.step-title-main {
    font-size: 15px;
    font-weight: 700;
    margin-bottom: 6px;
    color: var(--text-main);
    word-break: break-all;
}

.step-timing-pill {
    font: 500 11px 'DM Mono', monospace;
    color: var(--accent-cyan);
    margin-bottom: 14px;
}

.step-metric-rows {
    font-size: 26px;
    font-weight: 800;
    color: #ffffff;
    letter-spacing: -0.5px;
}

/* --- INPUTS & BUTTONS STYLING --- */
div[data-testid="stTextArea"] textarea {
    background: #080b13 !important;
    color: #d4e0ff !important;
    border: 1px solid #28334d !important;
    border-radius: var(--radius-md) !important;
    padding: 18px !important;
    font-family: 'DM Mono', monospace !important;
    font-size: 13.5px !important;
    line-height: 1.75 !important;
    transition: all 0.3s ease !important;
}

div[data-testid="stTextArea"] textarea:focus {
    border-color: var(--accent-purple) !important;
    box-shadow: 0 0 0 4px rgba(155, 140, 255, 0.2), var(--shadow-glow) !important;
}

div[data-testid="stTextInput"] input {
    background: #080b13 !important;
    color: #d4e0ff !important;
    border: 1px solid #28334d !important;
    border-radius: var(--radius-sm) !important;
    font-family: 'DM Mono', monospace !important;
    font-size: 13px !important;
    padding: 12px 14px !important;
}

div[data-testid="stTextInput"] input:focus {
    border-color: var(--accent-purple) !important;
    box-shadow: 0 0 0 3px rgba(155, 140, 255, 0.2) !important;
}

/* Custom Buttons with Glow */
.stButton > button {
    border-radius: var(--radius-sm) !important;
    min-height: 50px !important;
    font-weight: 700 !important;
    letter-spacing: 0.5px !important;
    transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1) !important;
}

.stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #8c7ae6 0%, #4b7bec 100%) !important;
    border: 1px solid #a29bfe !important;
    color: #ffffff !important;
    box-shadow: 0 10px 25px -8px rgba(140, 122, 230, 0.8) !important;
}

.stButton > button[kind="primary"]:hover {
    transform: translateY(-2px) !important;
    filter: brightness(1.15) !important;
    box-shadow: 0 15px 35px -8px rgba(140, 122, 230, 1) !important;
}

.stButton > button[kind="secondary"] {
    background: rgba(255, 255, 255, 0.025) !important;
    border: 1px solid #28334d !important;
    color: #d0d7de !important;
}

.stButton > button[kind="secondary"]:hover {
    border-color: #485b85 !important;
    background: rgba(255, 255, 255, 0.06) !important;
    color: #ffffff !important;
}

/* --- METRIC CONTAINERS --- */
div[data-testid="stMetric"] {
    background: linear-gradient(145deg, #131a2b, #0e1320);
    border: 1px solid #28334d;
    padding: 22px 24px;
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-subtle);
    transition: all 0.3s ease;
}

div[data-testid="stMetric"]:hover {
    border-color: #3b4c73;
    transform: translateY(-3px);
    box-shadow: 0 15px 30px -10px rgba(0, 0, 0, 0.7);
}

div[data-testid="stMetricLabel"] {
    color: var(--text-muted) !important;
    font-weight: 600 !important;
}

div[data-testid="stMetricValue"] {
    color: #ffffff !important;
    font-weight: 800 !important;
}

hr {
    border-color: var(--border-color);
    margin: 2rem 0;
}

::-webkit-scrollbar {
    width: 8px;
    height: 8px;
}

::-webkit-scrollbar-thumb {
    background: #25314d;
    border-radius: 6px;
}

::-webkit-scrollbar-thumb:hover {
    background: #394c78;
}

footer {
    visibility: hidden;
}
</style>
""", unsafe_allow_html=True)


# --- SIDEBAR NAVIGATION ---
with st.sidebar:
    st.markdown(
        "<div class='brand-container'><span class='brand-mark'>◈</span> ECHO</div>",
        unsafe_allow_html=True,
    )
    st.markdown("<div class='sidebar-caption'>Enterprise SQL Intelligence Engine</div>", unsafe_allow_html=True)
    st.divider()

    st.markdown("#### 🔗 API CONNECTION")
    api_url = st.text_input(
        "API URL endpoint",
        "http://127.0.0.1:8000",
    ).strip().rstrip("/")

    st.markdown("#### ⚡ SYSTEM MODULES")
    st.markdown("""
    <div class="nav-box"><span class="nav-dot-active"></span>AST PARSER & SECURITY GUARD</div>
    <div class="nav-box"><span class="nav-dot-active"></span>DUCKDB STEP TRACE RUNNER</div>
    <div class="nav-box"><span class="nav-dot-active"></span>FAN-OUT & ANOMALY DETECTORS</div>
    <div class="nav-box"><span class="nav-dot-active"></span>SILENT GUARD CONTROLLER</div>
    """, unsafe_allow_html=True)

    st.divider()
    st.markdown("#### 🏆 CAPSTONE INFO")
    st.caption("Advanced SQL Diagnostics & Observability platform built for enterprise data pipelines.")
    st.caption("DEPI Capstone Project • 2026 Edition")


# --- HERO HEADER SECTION ---
header_col_1, header_col_2 = st.columns([4, 1.2], gap="large")

with header_col_1:
    st.markdown("""
    <div class="eyebrow-tag">ENTERPRISE OBSERVABILITY & DIAGNOSTICS</div>
    <div class="hero-main-title">Trace the bug.<br>
    <span>Prove the fix.</span></div>
    <div class="hero-description">
    Advanced end-to-end SQL query analysis, performance profiling in milliseconds, and automated anomaly detection backed by rigorous DuckDB execution steps.
    </div>
    """, unsafe_allow_html=True)

with header_col_2:
    st.write("")
    st.markdown(
        '<div style="text-align:right; padding-top: 15px;">'
        '<div class="engine-status-badge">ENGINE ONLINE</div>'
        '</div>',
        unsafe_allow_html=True,
    )

st.write("")
st.divider()


# --- WORKSPACE & SQL EDITOR ---
st.markdown(
    '<div style="font-size: 20px; font-weight: 700; color: #f0f3ff; margin-bottom: 6px;">01 / Enterprise SQL Workspace</div>',
    unsafe_allow_html=True,
)
st.markdown(
    '<div style="font-size: 13px; color: var(--text-muted); margin-bottom: 18px;">Input or paste your transformation query below for deep inspection.</div>',
    unsafe_allow_html=True,
)

editor_col, meta_col = st.columns([3, 1.1], gap="large")

with editor_col:
    sql = st.text_area(
        "SQL input query",
        value=DEFAULT_SQL,
        height=290,
        label_visibility="collapsed",
        key="enterprise_sql_input",
    )

with meta_col:
    st.markdown("""
    <div class="enterprise-panel" style="margin-top: 0; padding: 22px;">
      <div class="panel-header-label">Security & Compliance</div>
      <h3 style="font-size: 16px; margin: 6px 0 12px 0; color: #fff;">Read-Only Guard</h3>
      <p style="font-size: 12.5px; color: var(--text-muted); line-height: 1.6; margin-bottom: 10px;">
        • Strict AST protection<br>
        • Blocks DROP / DELETE / UPDATE<br>
        • Zero false-positive silence<br>
        • DuckDB isolated execution
      </p>
      <hr style="margin: 12px 0;">
      <div class="panel-header-label">Performance</div>
      <p style="font-size: 12.5px; color: var(--text-muted); margin: 4px 0 0 0;">
        <b>Profiling:</b> Active (ms)<br>
        <b>Metrics:</b> Multi-column sums
      </p>
    </div>
    """, unsafe_allow_html=True)

st.write("")
btn_col_1, btn_col_2, spacer_col = st.columns([1.5, 1, 4], gap="medium")

with btn_col_1:
    investigate_btn = st.button(
        "🚀 Run Enterprise Investigation",
        type="primary",
        use_container_width=True,
    )

with btn_col_2:
    if st.button("Clear Cache", use_container_width=True):
        st.session_state.pop("investigation_result", None)
        st.session_state.pop("api_conn_error", None)
        st.rerun()


# --- API REQUEST HANDLING ---
if investigate_btn:
    if not sql.strip():
        st.warning("Please provide a valid SQL query before executing.")
        st.session_state.pop("investigation_result", None)
    else:
        try:
            with st.spinner("⚡ Echo Engine is executing step-by-step tracing and analyzing AST patterns..."):
                resp = requests.post(
                    f"{api_url}/investigate",
                    json={"sql": sql},
                    timeout=60,
                )
                resp.raise_for_status()
                data_payload = resp.json()

            if not isinstance(data_payload, dict):
                raise ValueError("API response must be a JSON object dictionary.")

            st.session_state["investigation_result"] = data_payload
            st.session_state.pop("api_conn_error", None)

        except requests.RequestException as err:
            st.session_state.pop("investigation_result", None)
            st.session_state["api_conn_error"] = f"Failed to connect to FastAPI endpoint: {err}"

        except (ValueError, TypeError) as err:
            st.session_state.pop("investigation_result", None)
            st.session_state["api_conn_error"] = f"Parsing error in API response: {err}"

if st.session_state.get("api_conn_error"):
    st.error(st.session_state["api_conn_error"])


# --- RENDER RESULTS & METRICS ---
result_data = st.session_state.get("investigation_result")

if result_data:
    step_traces = result_data.get("step_trace") or []
    detected_bugs_list = result_data.get("detected_bugs") or []

    st.write("")
    st.divider()
    st.markdown(
        '<div style="font-size: 20px; font-weight: 700; color: #f0f3ff; margin-bottom: 6px;">02 / Comprehensive Investigation Report</div>',
        unsafe_allow_html=True,
    )
    st.write("")

    success_count = sum(1 for s in step_traces if s.get("status") == "success")
    failed_count = sum(1 for s in step_traces if s.get("status") == "failed")

    m1, m2, m3 = st.columns(3, gap="medium")
    m1.metric("Total Execution Steps", len(step_traces), delta="Optimized")
    m2.metric(
        "Detected Anomalies",
        len(detected_bugs_list),
        delta="Clean Query" if not detected_bugs_list else "Action Required",
        delta_color="normal" if not detected_bugs_list else "inverse",
    )
    has_anomalies = len(detected_bugs_list) > 0
    if failed_count > 0:
        health_value = "Failed"
        health_delta = "Error"
        health_delta_color = "inverse"
    elif has_anomalies:
        health_value = "Warning"
        health_delta = f"{len(detected_bugs_list)} {'Anomaly' if len(detected_bugs_list) == 1 else 'Anomalies'}"
        health_delta_color = "inverse"
    else:
        health_value = "Optimal"
        health_delta = "100% Success"
        health_delta_color = "normal"

    m3.metric(
        "Pipeline Health Status",
        health_value,
        delta=health_delta,
        delta_color=health_delta_color,
    )

    # BUGS / ANOMALIES DISPLAY
    if detected_bugs_list:
        for bug_item in detected_bugs_list:
            st.markdown(f"""
            <div class="anomaly-alert-card">
              <div class="alert-kicker">🚨 CRITICAL ANOMALY / BEHAVIORAL BUG</div>
              <div class="alert-title">{safe_text(bug_item.get("bug_type"))}</div>
              <div class="alert-body">
                <b>Severity Level:</b> {safe_text(bug_item.get("severity"))}<br>
                <b>Technical Description:</b> {safe_text(bug_item.get("description"))}
              </div>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.markdown("")
        st.success("✨ Query execution and verification completed successfully with zero bugs or anomalies detected! (Silent Guard active — no false positives).")

    # STEP TRACE PIPELINE GRID
    st.markdown(
        '<div style="font-size: 20px; font-weight: 700; color: #f0f3ff; margin: 36px 0 6px 0;">03 / Step Trace Pipeline & Performance Grid</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div style="font-size: 13px; color: var(--text-muted); margin-bottom: 18px;">Inspect row volume, execution duration (ms), and column metrics across each discrete query phase.</div>',
        unsafe_allow_html=True,
    )

    if step_traces:
        for row_idx in range(0, len(step_traces), STEPS_PER_ROW):
            grid_cols = st.columns(STEPS_PER_ROW, gap="medium")
            current_chunk = step_traces[row_idx:row_idx + STEPS_PER_ROW]

            for offset_idx, step_obj in enumerate(current_chunk):
                with grid_cols[offset_idx]:
                    st_status = step_obj.get("status", "success")
                    is_err = st_status == "failed"
                    card_css = "failed-step" if is_err else "success-step"
                    badge_label = "FAILED" if is_err else safe_text(step_obj.get("step_type"), "STEP")

                    rows_val = format_number(step_obj.get("row_count"))
                    step_name_id = safe_text(step_obj.get("step_id"))
                    duration_ms = step_obj.get("execution_time_ms")
                    time_display = f"{duration_ms}ms" if duration_ms is not None else "—"
                    err_detail = safe_text(step_obj.get("error"))

                    st.markdown(f"""
                    <div class="trace-step-card {card_css}">
                      <div class="step-badge-top">
                        <span>{badge_label}</span>
                        <span style="color: var(--accent-cyan);">⚡ {time_display}</span>
                      </div>
                      <div class="step-title-main">{step_name_id}</div>
                      <div style="margin: 18px 0 6px 0;">
                        <span class="step-metric-rows">{rows_val}</span>
                      </div>
                      <div style="font: 600 10px 'DM Mono', monospace; color: var(--text-muted); letter-spacing: 1px;">ROW COUNT</div>
                      {"<div style='color: var(--accent-red); font-size: 11.5px; margin-top: 10px;'><b>Error:</b> " + err_detail + "</div>" if is_err else ""}
                    </div>
                    """, unsafe_allow_html=True)

        st.write("")

        # ALTAIR ROW COUNT VISUALIZATION BAR CHART
        valid_chart_steps = [
            s for s in step_traces
            if isinstance(s.get("row_count"), (int, float))
            and s.get("status") == "success"
        ]

        if valid_chart_steps:
            df_chart = pd.DataFrame(valid_chart_steps)
            chart_viz = (
                alt.Chart(df_chart)
                .mark_bar(cornerRadiusTopLeft=8, cornerRadiusTopRight=8, size=48)
                .encode(
                    x=alt.X("step_id:N", sort=None, title=None, axis=alt.Axis(labelAngle=-15, labelColor="#a0abc2", labelPadding=10)),
                    y=alt.Y("row_count:Q", title="Row Count Volume", axis=alt.Axis(labelColor="#a0abc2", titleColor="#8b95ad")),
                    color=alt.value("#8c7ae6"),
                    tooltip=[
                        alt.Tooltip("step_id:N", title="Step ID"),
                        alt.Tooltip("step_type:N", title="Type"),
                        alt.Tooltip("row_count:Q", title="Row Count", format=","),
                        alt.Tooltip("execution_time_ms:Q", title="Duration (ms)"),
                    ],
                )
                .properties(height=340, padding={"left": 12, "right": 20, "top": 16, "bottom": 12})
                .configure(background="transparent", view=alt.ViewConfig(stroke=None))
                .configure_axis(gridColor="#1d263b", domainColor="#303f60", tickColor="#303f60")
            )
            st.altair_chart(chart_viz, use_container_width=True)

    else:
        st.info("No execution step traces returned from the engine runner.")

    st.write("")
    with st.expander("🔍 Inspect Raw Engine JSON Payload"):
        st.json(result_data)

else:
    # EMPTY INITIAL STATE
    st.markdown("""
    <div class="enterprise-panel" style="text-align: center; padding: 64px 24px; margin-top: 32px;">
      <div style="font-size: 48px; color: var(--accent-purple); margin-bottom: 12px;">◈</div>
      <div style="font-size: 24px; font-weight: 800; color: #ffffff; margin-bottom: 10px;">
        Ready for Enterprise Query Investigation
      </div>
      <div style="color: var(--text-muted); font-size: 14px; line-height: 1.8; max-width: 560px; margin: 0 auto;">
        Submit your SQL transformation above to trigger AST parsing, execute step tracing via DuckDB, and evaluate performance anomalies with zero false positives.
      </div>
    </div>
    """, unsafe_allow_html=True)


# --- FOOTER ---
st.markdown("""
<div style="border-top: 1px solid var(--border-color); padding-top: 24px; margin-top: 60px; color: #5a6582; font-size: 11.5px; font-family: 'DM Mono', monospace; letter-spacing: 1.2px;">
ECHO · ENTERPRISE SQL INTELLIGENCE & OBSERVABILITY PLATFORM
<span style="float: right;">TRACE THE BUG. PROVE THE FIX.</span>
</div>
""", unsafe_allow_html=True)