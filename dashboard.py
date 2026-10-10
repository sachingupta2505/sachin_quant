"""
SachinQuant Trading Cockpit - Visual Streamlit Analytics Dashboard
Module: dashboard.py

Features:
1. Minimalist, modern, and readable at a single glance.
2. Header & Status: Mode badge (Paper Trading / Live Money) & Last Updated timestamp.
3. Top Row KPI Cards: Net PnL (color-coded with ₹), Win Rate %, Total Trades Done, Max Risk.
4. Cumulative PnL line graph (Green when in profit, Red when in drawdown).
5. Friendly Trade Log table with soft green/red row styling for wins/losses.
6. 1-Click Excel / CSV Download report button.
7. Auto-refresh sidebar toggle (default 10s).
8. Read-only SQLite WAL mode (PRAGMA query_only = ON) preventing database locks.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st

IST = ZoneInfo("Asia/Kolkata")
ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_JOURNAL_DB = (ROOT_DIR / "trading_journal.db").resolve()
DEFAULT_BUS_DB = (ROOT_DIR / "system_bus.db").resolve()


def get_read_only_connection(db_path: Path) -> Optional[sqlite3.Connection]:
    """Establishes an ACID read-only connection with WAL pragma to avoid locking the live engine."""
    resolved_path = Path(db_path).resolve()
    if not resolved_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{resolved_path.as_posix()}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON;")
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        try:
            conn = sqlite3.connect(str(resolved_path))
            conn.execute("PRAGMA query_only = ON;")
            conn.row_factory = sqlite3.Row
            return conn
        except Exception:
            return None


def get_trade_strikes_map(bus_db_path: Path = DEFAULT_BUS_DB) -> Dict[str, str]:
    """Extracts human-readable strike information from Blackboard event payloads."""
    strikes_map: Dict[str, str] = {}
    conn = get_read_only_connection(bus_db_path)
    if not conn:
        return strikes_map
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT payload FROM bus_events WHERE topic IN ('ORDER_PROPOSED', 'ORDER_APPROVED', 'ORDER_EXECUTED')"
        )
        for row in cursor.fetchall():
            try:
                payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
                trade_id = payload.get("trade_id")
                if trade_id and trade_id not in strikes_map and "legs" in payload:
                    legs = payload["legs"]
                    leg_strs = [
                        f"{int(float(leg['strike']))} {leg['option_type']}"
                        for leg in legs
                        if "strike" in leg and "option_type" in leg
                    ]
                    if leg_strs:
                        strikes_map[trade_id] = " / ".join(leg_strs)
            except Exception:
                continue
    except Exception:
        pass
    finally:
        conn.close()
    return strikes_map


@st.cache_data(ttl=5, show_spinner=False)
def load_trades_data(
    journal_db_path: Path = DEFAULT_JOURNAL_DB,
    bus_db_path: Path = DEFAULT_BUS_DB,
    target_date: Optional[str] = None,
) -> pd.DataFrame:
    """Reads genuine trade records from production trading_journal.db and enriches with strikes and formatting.
    Zero mock/fallback data: if trading_journal.db has 0 rows for the queried filter, strictly returns empty DataFrame.
    """
    resolved_journal = Path(journal_db_path).resolve()
    resolved_bus = Path(bus_db_path).resolve()

    conn = get_read_only_connection(resolved_journal)
    if not conn:
        return pd.DataFrame()

    try:
        cursor = conn.cursor()
        # Verify table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trade_journal'")
        if not cursor.fetchone():
            return pd.DataFrame()

        if target_date and target_date != "ALL":
            query = "SELECT * FROM trade_journal WHERE date = ? ORDER BY entry_time ASC"
            df = pd.read_sql_query(query, conn, params=(target_date,))
        else:
            query = "SELECT * FROM trade_journal ORDER BY entry_time ASC"
            df = pd.read_sql_query(query, conn)
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()

    if df.empty:
        return pd.DataFrame()

    # Enrich strikes from bus_events or notes
    strikes_map = get_trade_strikes_map(resolved_bus)

    def extract_strikes(row: pd.Series) -> str:
        tid = row.get("trade_id", "")
        if tid in strikes_map:
            return strikes_map[tid]
        notes = str(row.get("notes", ""))
        spread_type = str(row.get("spread_type", ""))
        if "(" in notes and ")" in notes:
            try:
                spot_sub = notes.split("(")[1].split(")")[0]
                spot_val = float(spot_sub)
                base_strike = int(round(spot_val / 50.0) * 50)
                if "BULL_PUT" in spread_type:
                    return f"{base_strike - 50} PE / {base_strike} PE"
                elif "BEAR_CALL" in spread_type:
                    return f"{base_strike} CE / {base_strike + 50} CE"
            except Exception:
                pass
        return spread_type.replace("_", " ").title() if spread_type else "-"

    df["strikes"] = df.apply(extract_strikes, axis=1)

    # Format human-readable columns
    def format_time(ts_str: Any) -> str:
        if not ts_str:
            return "-"
        try:
            return datetime.fromisoformat(str(ts_str)).strftime("%H:%M:%S")
        except Exception:
            return str(ts_str)[11:19] if len(str(ts_str)) >= 19 else str(ts_str)

    df["time_display"] = df["entry_time"].apply(format_time)
    df["strategy_display"] = df["spread_type"].apply(
        lambda s: str(s).replace("_", " ").title() if s else "Spread"
    )
    if "gross_pnl" not in df.columns:
        df["gross_pnl"] = df["realized_pnl"]
    if "total_charges" not in df.columns:
        df["total_charges"] = 0.0
    if "net_pnl" not in df.columns:
        df["net_pnl"] = df["realized_pnl"]

    return df


def get_starting_capital(config_path: Path = ROOT_DIR / "config.json") -> float:
    """Reads starting capital from config.json, defaulting to ₹1,00,000.00."""
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            if "starting_capital" in data:
                return float(data["starting_capital"])
            if "capital" in data and "starting_capital" in data["capital"]:
                return float(data["capital"]["starting_capital"])
        except Exception:
            pass
    return 100000.0


def compute_kpis(df: pd.DataFrame, starting_capital: Optional[float] = None) -> Dict[str, Any]:
    """Computes headline KPIs from trade journal including base capital and regulatory charges."""
    cap = starting_capital if starting_capital is not None else get_starting_capital()
    if df.empty:
        return {
            "starting_capital": cap,
            "current_balance": cap,
            "gross_pnl": 0.0,
            "total_charges": 0.0,
            "net_pnl": 0.0,
            "net_roi_pct": 0.0,
            "win_rate": 0.0,
            "total_trades": 0,
            "closed_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "max_risk_cap": 1500.0,
            "is_paper": True,
        }

    gross_pnl = float(df["gross_pnl"].sum()) if "gross_pnl" in df.columns else float(df["realized_pnl"].sum())
    total_charges = float(df["total_charges"].sum()) if "total_charges" in df.columns else 0.0
    net_pnl = float(df["net_pnl"].sum()) if "net_pnl" in df.columns else float(df["realized_pnl"].sum())
    current_balance = round(cap + net_pnl, 2)
    net_roi_pct = round((net_pnl / cap) * 100.0, 2) if cap > 0 else 0.0

    total_trades = len(df)
    closed = df[df["status"] == "CLOSED"]
    closed_trades = len(closed)
    pnl_series = closed["net_pnl"] if "net_pnl" in closed.columns else closed["realized_pnl"]
    winning_trades = int((pnl_series > 0).sum())
    losing_trades = int((pnl_series < 0).sum())
    win_rate = (winning_trades / closed_trades * 100.0) if closed_trades > 0 else 0.0

    # Mode: Live Money if any trade has is_paper == 0, else Paper Trading
    is_paper = bool((df["is_paper"] != 0).all()) if "is_paper" in df.columns else True

    return {
        "starting_capital": cap,
        "current_balance": current_balance,
        "gross_pnl": gross_pnl,
        "total_charges": total_charges,
        "net_pnl": net_pnl,
        "net_roi_pct": net_roi_pct,
        "win_rate": round(win_rate, 1),
        "total_trades": total_trades,
        "closed_trades": closed_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "max_risk_cap": 1500.0,
        "is_paper": is_paper,
    }


def render_ui(
    journal_db_path: Path = DEFAULT_JOURNAL_DB,
    bus_db_path: Path = DEFAULT_BUS_DB,
) -> None:
    """Renders the complete Streamlit UI."""
    st.set_page_config(
        page_title="SachinQuant Trading Cockpit",
        page_icon="⚡",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # Clean custom CSS
    st.markdown(
        """
        <style>
        .main { background-color: #f8fafc; }
        .kpi-card {
            background: #ffffff;
            border-radius: 12px;
            padding: 20px 22px;
            border: 1px solid #e2e8f0;
            box-shadow: 0 1px 3px rgba(0,0,0,0.05);
            transition: all 0.2s ease-in-out;
        }
        .kpi-card:hover {
            box-shadow: 0 4px 6px -1px rgba(0,0,0,0.08);
            border-color: #cbd5e1;
        }
        .badge-paper {
            background-color: #ebf8ff;
            color: #2b6cb0;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 13px;
            font-weight: 700;
            border: 1px solid #bee3f8;
            display: inline-block;
        }
        .badge-live {
            background-color: #fff5f5;
            color: #c53030;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 13px;
            font-weight: 700;
            border: 1px solid #fed7d7;
            display: inline-block;
        }
        .badge-time {
            background-color: #f1f5f9;
            color: #475569;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 13px;
            font-weight: 600;
            display: inline-block;
        }
        .table-container {
            background: #ffffff;
            border-radius: 12px;
            border: 1px solid #e2e8f0;
            padding: 16px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # Sidebar
    st.sidebar.title("⚡ Cockpit Controls")

    # Date Filter: ["All Time" (default), "Today", "Custom Date"]
    date_filter_options = ["All Time", "Today", "Custom Date"]
    selected_filter = "All Time"
    raw_filter = st.sidebar.selectbox(
        "📅 Date Filter",
        options=date_filter_options,
        index=0,
    )
    if isinstance(raw_filter, str):
        selected_filter = raw_filter

    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    target_date_param: Optional[str] = None

    if selected_filter == "All Time":
        target_date_param = None
    elif selected_filter == "Today":
        target_date_param = today_str
    elif selected_filter == "Custom Date":
        try:
            custom_d = st.sidebar.date_input(
                "Select Custom Date",
                value=datetime.now(IST).date(),
            )
            target_date_param = custom_d.strftime("%Y-%m-%d") if hasattr(custom_d, "strftime") else str(custom_d)
        except Exception:
            target_date_param = today_str

    auto_refresh = st.sidebar.checkbox("🔄 Auto-Refresh every 10s", value=False)
    if st.sidebar.button("🔁 Manual Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.sidebar.markdown("---")
    st.sidebar.markdown(
        f"""
        **System Specs:**
        - Engine: 4-Agent Autonomous
        - Broker: Angel One SmartAPI
        - Database: `{Path(journal_db_path).resolve().name}`
        - Risk Guard: Max Loss ₹1,500/day
        - Strategy: 5m Wick Rejection
        """
    )

    # Load data & compute metrics
    df = load_trades_data(journal_db_path, bus_db_path, target_date=target_date_param)
    kpis = compute_kpis(df)
    now_str = datetime.now(IST).strftime("%I:%M:%S %p IST")

    # Header & Status Row
    head_col1, head_col2 = st.columns([3, 2])
    with head_col1:
        st.markdown(
            "<h1 style='margin-bottom: 2px; font-weight: 800; color: #0f172a;'>SachinQuant Trading Cockpit</h1>",
            unsafe_allow_html=True,
        )
        st.markdown(
            "<p style='color: #64748b; font-size: 15px; margin-top: 0px;'>Quantitative Options Spread Execution Engine</p>",
            unsafe_allow_html=True,
        )

    with head_col2:
        st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)
        mode_badge = (
            "<span class='badge-paper'>🔵 Paper Trading Mode</span>"
            if kpis["is_paper"]
            else "<span class='badge-live'>🔴 Live Money</span>"
        )
        time_badge = f"<span class='badge-time'>🕒 {now_str}</span>"
        st.markdown(
            f"<div style='text-align: right;'>{mode_badge} &nbsp; {time_badge}</div>",
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)

    # Top Row: KPI Cards
    kpi_col1, kpi_col2, kpi_col3, kpi_col4 = st.columns(4)
    net_pnl = kpis["net_pnl"]
    pnl_color = "#16a34a" if net_pnl >= 0 else "#dc2626"
    pnl_sign = "+" if net_pnl > 0 else ("" if net_pnl == 0 else "-")
    pnl_icon = "🟢" if net_pnl >= 0 else "🔴"

    with kpi_col1:
        bal = kpis["current_balance"]
        bal_color = "#16a34a" if bal >= kpis["starting_capital"] else "#dc2626"
        st.markdown(
            f"""
            <div class='kpi-card'>
                <div style='font-size: 13px; color: #64748b; font-weight: 600; text-transform: uppercase;'>
                    💼 Capital vs Balance
                </div>
                <div style='font-size: 30px; font-weight: 800; color: {bal_color}; margin-top: 6px;'>
                    ₹{bal:,.2f}
                </div>
                <div style='font-size: 12px; color: #94a3b8; margin-top: 4px;'>
                    Starting Capital: ₹{kpis['starting_capital']:,.2f}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with kpi_col2:
        st.markdown(
            f"""
            <div class='kpi-card'>
                <div style='font-size: 13px; color: #64748b; font-weight: 600; text-transform: uppercase;'>
                    {pnl_icon} Net Realized PnL
                </div>
                <div style='font-size: 30px; font-weight: 800; color: {pnl_color}; margin-top: 6px;'>
                    {pnl_sign}₹{abs(net_pnl):,.2f}
                </div>
                <div style='font-size: 12px; color: #94a3b8; margin-top: 4px;'>
                    After Taxes & Charges (Gross: ₹{kpis['gross_pnl']:,.2f})
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with kpi_col3:
        st.markdown(
            f"""
            <div class='kpi-card'>
                <div style='font-size: 13px; color: #64748b; font-weight: 600; text-transform: uppercase;'>
                    🧾 Total Charges Deducted
                </div>
                <div style='font-size: 30px; font-weight: 800; color: #e11d48; margin-top: 6px;'>
                    ₹{kpis['total_charges']:,.2f}
                </div>
                <div style='font-size: 12px; color: #94a3b8; margin-top: 4px;'>
                    STT, ETC, GST, Stamp & Brokerage
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with kpi_col4:
        roi = kpis["net_roi_pct"]
        roi_color = "#16a34a" if roi >= 0 else "#dc2626"
        roi_sign = "+" if roi > 0 else ("" if roi == 0 else "-")
        st.markdown(
            f"""
            <div class='kpi-card'>
                <div style='font-size: 13px; color: #64748b; font-weight: 600; text-transform: uppercase;'>
                    🎯 Net ROI %
                </div>
                <div style='font-size: 30px; font-weight: 800; color: {roi_color}; margin-top: 6px;'>
                    {roi_sign}{abs(roi):.2f}%
                </div>
                <div style='font-size: 12px; color: #94a3b8; margin-top: 4px;'>
                    Win Rate: {kpis['win_rate']:.1f}% ({kpis['winning_trades']}W / {kpis['losing_trades']}L)
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height: 20px;'></div>", unsafe_allow_html=True)

    # Middle Row: Cumulative Equity Curve from Inception Capital (₹100,000) to Current Date
    st.markdown(
        "<h3 style='color: #1e293b; font-weight: 700; margin-bottom: 8px;'>📈 Cumulative Equity Curve (Inception ₹100,000 to Current Date)</h3>",
        unsafe_allow_html=True,
    )

    filter_desc = "All Time History" if selected_filter == "All Time" else (f"Today ({today_str})" if selected_filter == "Today" else f"Date {target_date_param}")
    if df.empty:
        st.info(f"ℹ️ No trades recorded for {filter_desc}. Capital is 100% preserved at ₹{kpis['starting_capital']:,.2f}.")
    else:
        # Build chronological cumsum of Net PnL and Portfolio Equity
        starting_cap = kpis["starting_capital"]
        df_sorted = df.sort_values(by="entry_time", ascending=True).copy()
        pnl_col = "net_pnl" if "net_pnl" in df_sorted.columns else "realized_pnl"
        df_sorted["cum_net_pnl"] = df_sorted[pnl_col].cumsum()
        df_sorted["portfolio_equity"] = starting_cap + df_sorted["cum_net_pnl"]

        # Add initial inception baseline point
        chart_records = [{
            "Trade Sequence": "Inception (₹1,00,000)",
            "Portfolio Equity (₹)": float(starting_cap),
            "Net Cumulative PnL (₹)": 0.0,
        }]
        for idx, row in enumerate(df_sorted.itertuples(), start=1):
            d_str = getattr(row, "date", "")
            t_str = getattr(row, "time_display", "")
            t_label = f"#{idx} ({d_str} {t_str})" if d_str else f"#{idx} ({t_str})"
            chart_records.append({
                "Trade Sequence": t_label,
                "Portfolio Equity (₹)": float(getattr(row, "portfolio_equity", starting_cap)),
                "Net Cumulative PnL (₹)": float(getattr(row, "cum_net_pnl", 0.0)),
            })

        chart_df = pd.DataFrame(chart_records)
        end_equity = float(chart_df.iloc[-1]["Portfolio Equity (₹)"])
        line_color = "#16a34a" if end_equity >= starting_cap else "#dc2626"

        chart = (
            alt.Chart(chart_df)
            .mark_line(
                color=line_color,
                strokeWidth=3,
                point=alt.OverlayMarkDef(color=line_color, size=65, filled=True),
            )
            .encode(
                x=alt.X("Trade Sequence:N", title="Execution Sequence (Inception to Present)", sort=None),
                y=alt.Y(
                    "Portfolio Equity (₹):Q",
                    title="Portfolio Equity (₹)",
                    scale=alt.Scale(zero=False, padding=25),
                ),
                tooltip=[
                    alt.Tooltip("Trade Sequence:N", title="Sequence"),
                    alt.Tooltip("Portfolio Equity (₹):Q", format=",.2f", title="Portfolio Equity"),
                    alt.Tooltip("Net Cumulative PnL (₹):Q", format=",.2f", title="Net Cumulative PnL"),
                ],
            )
            .properties(height=280)
            .configure_axis(grid=True, gridColor="#f1f5f9")
            .configure_view(strokeWidth=0)
        )
        st.altair_chart(chart, use_container_width=True)

    st.markdown("<div style='height: 15px;'></div>", unsafe_allow_html=True)

    # Bottom Row: Friendly Trade Log Table displaying Date, Time, Gross ₹, Charges ₹, Net ₹
    st.markdown(
        "<h3 style='color: #1e293b; font-weight: 700; margin-bottom: 8px;'>📋 Live Trade Log (Lifetime Inception History)</h3>",
        unsafe_allow_html=True,
    )

    if df.empty:
        st.info(f"ℹ️ No trades recorded for {filter_desc}.")
    else:
        # Prepare display DataFrame with friendly columns in reverse chronological order
        df_rev = df.sort_values(by="entry_time", ascending=False).copy()
        table_df = pd.DataFrame()
        table_df["Date"] = df_rev["date"] if "date" in df_rev.columns else "-"
        table_df["Time"] = df_rev["time_display"]
        table_df["Strategy"] = df_rev["strategy_display"]
        table_df["Strikes"] = df_rev["strikes"]
        table_df["Entry ₹"] = df_rev["actual_entry_price"].apply(lambda v: f"₹{float(v):.2f}")
        table_df["Exit ₹"] = df_rev["actual_exit_price"].apply(
            lambda v: f"₹{float(v):.2f}" if pd.notnull(v) and float(v) > 0 else "-"
        )
        table_df["Gross ₹"] = df_rev["gross_pnl"].apply(
            lambda v: f"+₹{float(v):,.2f}" if float(v) > 0 else (f"-₹{abs(float(v)):,.2f}" if float(v) < 0 else "₹0.00")
        )
        table_df["Charges ₹"] = df_rev["total_charges"].apply(lambda v: f"₹{float(v):,.2f}")
        table_df["Net ₹"] = df_rev["net_pnl"].apply(
            lambda v: f"+₹{float(v):,.2f}" if float(v) > 0 else (f"-₹{abs(float(v)):,.2f}" if float(v) < 0 else "₹0.00")
        )
        table_df["Status"] = df_rev["status"]

        # Color-coded styled table based on Net PnL
        def highlight_pnl(row: pd.Series) -> list[str]:
            pnl_str = str(row["Net ₹"])
            if pnl_str.startswith("+"):
                return ["background-color: rgba(34, 197, 94, 0.12)"] * len(row)
            elif pnl_str.startswith("-"):
                return ["background-color: rgba(239, 68, 68, 0.12)"] * len(row)
            return [""] * len(row)

        styled_table = table_df.style.apply(highlight_pnl, axis=1)
        st.dataframe(styled_table, use_container_width=True, hide_index=True)

        # 1-Click Excel / CSV Download Button
        csv_data = table_df.to_csv(index=False).encode("utf-8")
        today_tag = datetime.now(IST).strftime("%Y%m%d")
        st.download_button(
            label="📥 Download Excel / CSV Report",
            data=csv_data,
            file_name=f"SachinQuant_Trades_{today_tag}.csv",
            mime="text/csv",
            use_container_width=False,
        )

    # Auto-refresh cycle
    if auto_refresh:
        time.sleep(10)
        st.rerun()


def main():
    render_ui()


if __name__ == "__main__":
    main()
