#!/usr/bin/env python3
"""Refresh the ETF comparison snapshot from public association/government data."""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "etf-data.json"
ETF_PAGE = "https://www.sitca.org.tw/ROC/SITCA_ETF/etf_statement2.aspx?txtR1=0&txtYM={ym}"
TWSE_ETF_REGISTRY = "https://mopsfin.twse.com.tw/opendata/t187ap47_L.csv"
TWSE_POLICY_OVERRIDES = {
    # The exchange's official new-ETF page states this fund's income is not distributed.
    "00412A": "收益不分配",
}
DIVIDENDS_CSV = (
    "https://www.sitca.org.tw/MemberK0000/F/03/"
    "160547%E6%8A%95%E4%BF%A1%E6%8A%95%E9%A1%A7%E5%85%AC%E6%9C%83%E5%A2%83%E5%85%A7%E5%9F%BA%E9%87%91%E9%85%8D%E6%81%AF%E8%B3%87%E6%96%99.csv"
)
UA = {"User-Agent": "Mozilla/5.0 (compatible; TaiwanETFDashboard/1.0)"}


def clean(value: object) -> str:
    if pd.isna(value):
        return ""
    return str(value).replace("\xa0", " ").strip()


def number(value: object) -> float | None:
    s = clean(value).replace(",", "").replace("元", "").replace("NT$", "")
    if not s or s in {"-", "--", "N/A", "nan"}:
        return None
    s = s.replace("%", "")
    try:
        return float(s)
    except ValueError:
        match = re.search(r"-?\d+(?:\.\d+)?", s)
        return float(match.group()) if match else None


def colmap(df: pd.DataFrame) -> dict[str, object]:
    out = {}
    for col in df.columns:
        name = "".join(clean(x) for x in col) if isinstance(col, tuple) else clean(col)
        out[name.replace(" ", "").replace("\n", "")] = col
    return out


def pick(cols: dict[str, object], *keys: str) -> object | None:
    for key in keys:
        for name, original in cols.items():
            if key in name:
                return original
    return None


def code(value: object) -> str:
    s = clean(value).upper()
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    if s.isdigit() and len(s) < 4:
        s = s.zfill(4)
    return s


def parsed_date(value: object) -> pd.Timestamp:
    s = clean(value)
    # SITCA/legacy CSVs may use ROC years (for example 115/10/15).
    m = re.fullmatch(r"(\d{2,3})[./-](\d{1,2})[./-](\d{1,2})", s)
    if m and int(m.group(1)) < 1911:
        s = f"{int(m.group(1)) + 1911:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return pd.to_datetime(s, errors="coerce")


def registry_date(value: object) -> str | None:
    s = clean(value)
    # TWSE open data uses ROC dates without separators, e.g. 1151013.
    if re.fullmatch(r"\d{7}", s):
        year = int(s[:3]) + 1911
        return f"{year:04d}-{int(s[3:5]):02d}-{int(s[5:]):02d}"
    parsed = parsed_date(s)
    return parsed.date().isoformat() if not pd.isna(parsed) else None


def get_twse_registry(session: requests.Session) -> pd.DataFrame:
    res = session.get(TWSE_ETF_REGISTRY, timeout=60)
    res.raise_for_status()
    res.encoding = "utf-8-sig"
    df = pd.read_csv(StringIO(res.content.decode("utf-8-sig")), dtype=str)
    cols = colmap(df)
    c_code = pick(cols, "基金代號")
    c_short = pick(cols, "基金簡稱")
    c_name = pick(cols, "基金中文名稱")
    c_type = pick(cols, "基金類型")
    c_id = pick(cols, "基金統一編號")
    c_listing = pick(cols, "上市日期")
    if not all((c_code, c_short, c_name, c_type, c_id, c_listing)):
        raise RuntimeError(f"TWSE ETF 基本資料欄位有變動：{list(cols)}")
    out = pd.DataFrame({
        "code": df[c_code].map(code),
        "short_name": df[c_short].map(clean),
        "full_name": df[c_name].map(clean),
        "type_text": df[c_type].map(clean),
        "fund_id": df[c_id].map(clean).str.replace(r"\.0$", "", regex=True).str.zfill(8),
        "listing_date": df[c_listing].map(registry_date),
    })
    out = out[out.code.str.match(r"^\d{4,6}[A-Z]{0,2}$", na=False)]
    return out.drop_duplicates("code", keep="last")


def get_report(session: requests.Session) -> tuple[pd.DataFrame, str]:
    today = date.today()
    # SITCA publishes the monthly ETF statement after month end. Search backward
    # in case the newest month has not been published yet.
    for back in range(1, 7):
        month_index = today.year * 12 + today.month - 1 - back
        year, month0 = divmod(month_index, 12)
        ym = f"{year}{month0 + 1:02d}"
        res = session.get(ETF_PAGE.format(ym=ym), timeout=40)
        res.raise_for_status()
        res.encoding = res.apparent_encoding or "utf-8"
        try:
            tables = pd.read_html(StringIO(res.text), displayed_only=False)
        except ValueError:
            continue
        for table in tables:
            cols = colmap(table)
            c_code = pick(cols, "證券代號", "股票代號")
            c_name = pick(cols, "證券簡稱", "證券名稱")
            c_aum = pick(cols, "基金規模(台幣)", "基金規模（台幣）")
            c_nav = pick(cols, "單位淨值(台幣)", "單位淨值（台幣）")
            c_benef = pick(cols, "總受益人數", "受益人數")
            c_fundid = pick(cols, "基金統編")
            c_type = pick(cols, "類型代號", "基金類型")
            c_rule = cols.get("類型") or pick(cols, "分配", "級別")
            if any(x is None for x in (c_code, c_name, c_aum, c_nav)):
                continue
            result = pd.DataFrame({
                "code": table[c_code].map(code),
                "name": table[c_name].map(clean),
                "aum": table[c_aum].map(number),
                "nav": table[c_nav].map(number),
                "beneficiaries": table[c_benef].map(number) if c_benef is not None else pd.Series([None] * len(table), index=table.index),
                "fund_id": table[c_fundid].map(clean).str.replace(r"\.0$", "", regex=True).str.zfill(8) if c_fundid is not None else "",
                "type_code": table[c_type].map(clean) if c_type is not None else "",
                "distribution_policy": table[c_rule].map(clean) if c_rule is not None else "",
            })
            result = result[result.code.str.match(r"^\d{4,6}[A-Z]{0,2}$", na=False)]
            if not result.empty:
                return result.drop_duplicates("code", keep="last"), ym
    raise RuntimeError("SITCA 找不到最近 6 個月份的 ETF 明細；保留上次資料，不發布空清單。")


def get_dividends(session: requests.Session) -> pd.DataFrame:
    res = session.get(DIVIDENDS_CSV, timeout=60)
    res.raise_for_status()
    # Handle the association's UTF-8/Big5 CSV variations.
    raw = None
    for enc in ("utf-8-sig", "cp950", "big5"):
        try:
            raw = res.content.decode(enc)
            break
        except UnicodeDecodeError:
            pass
    if raw is None:
        raw = res.content.decode("utf-8", errors="replace")
    df = pd.read_csv(StringIO(raw), dtype=str)
    cols = colmap(df)
    fund_id = pick(cols, "基金統編")
    ex_date = pick(cols, "除息日", "除權息日")
    amount = pick(cols, "每單位分派金額", "每單位配息金額")
    if not all((fund_id, ex_date, amount)):
        raise RuntimeError(f"配息 CSV 欄位有變動：{list(cols)}")
    return pd.DataFrame({
        "fund_id": df[fund_id].map(clean).str.replace(r"\.0$", "", regex=True).str.zfill(8),
        "date": df[ex_date].map(clean),
        "amount": df[amount].map(number),
    })


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(UA)
    funds, data_month = get_report(session)
    registry = pd.DataFrame()
    try:
        registry = get_twse_registry(session)
    except Exception as exc:
        print(f"TWSE ETF 基本資料暫時無法取得，先使用投信投顧公會月報：{exc}", file=sys.stderr)
    if not registry.empty:
        registry_by_code = registry.set_index("code").to_dict("index")
        funds = funds.copy()
        funds["listing_date"] = funds["code"].map(lambda c: registry_by_code.get(c, {}).get("listing_date"))
        existing = set(funds["code"])
        supplements = []
        for item in registry.to_dict("records"):
            if item["code"] in existing:
                continue
            # For brand-new listings, the registry supplies identity and date;
            # monthly AUM/NAV/beneficiary data will fill in from SITCA later.
            rule = TWSE_POLICY_OVERRIDES.get(item["code"], "")
            active = "主動式" in item["type_text"]
            supplements.append({
                "code": item["code"], "name": item["short_name"] or item["full_name"],
                "aum": None, "nav": None, "beneficiaries": None,
                "fund_id": item["fund_id"], "type_code": "AL-OPEN" if active else "AH-OPEN",
                "distribution_policy": rule, "listing_date": item["listing_date"],
            })
        if supplements:
            funds = pd.concat([funds, pd.DataFrame(supplements)], ignore_index=True)
            print(f"TWSE open-data supplement: added {len(supplements)} ETF listings missing from the SITCA monthly report.")
    dividends = get_dividends(session)
    cutoff = pd.Timestamp.today().normalize() - pd.Timedelta(days=365)
    rows = []
    for fund in funds.to_dict("records"):
        subset = dividends[dividends.fund_id == fund["fund_id"]].copy()
        subset["parsed_date"] = subset.date.map(parsed_date)
        subset["amount"] = pd.to_numeric(subset.amount, errors="coerce")
        subset = subset.dropna(subset=["parsed_date", "amount"])
        trailing = subset[subset.parsed_date >= cutoff]
        last = subset.sort_values("parsed_date").tail(1)
        nav = fund["nav"]
        twelve_month_sum = float(trailing.amount.sum()) if not trailing.empty else 0.0
        last_amount = float(last.amount.iloc[0]) if not last.empty else None
        policy = clean(fund["distribution_policy"])
        # The official policy field explicitly distinguishes distribution and
        # non-distribution classes. A blank/unknown policy is not evidence of
        # non-distribution; scheduled distribution also does not guarantee a
        # cash payment in every period.
        if re.search(r"不配息|不分配", policy):
            policy_status = "no_distribution"
        elif "分配" in policy:
            policy_status = "distribution"
        else:
            policy_status = "unspecified"
        # SITCA type codes distinguish active (ALxx) from index/passive (AHxx)
        # funds more reliably than the listing-code suffix.
        management = "主動式" if clean(fund["type_code"]).upper().startswith("AL") else "被動式／其他"
        rows.append({
            "code": fund["code"], "name": fund["name"],
            "management": management, "fund_type_code": fund["type_code"], "distribution_policy": policy,
            "distribution_policy_source": "TWSE ETF information" if fund["code"] in TWSE_POLICY_OVERRIDES else "SITCA monthly report",
            "listing_date": fund.get("listing_date"),
            "distribution_policy_status": policy_status,
            "aum_twd": fund["aum"], "nav_twd": nav,
            "beneficiaries": fund["beneficiaries"],
            "beneficiaries_date": data_month,
            "last_ex_date": last.parsed_date.iloc[0].date().isoformat() if not last.empty else None,
            "last_distribution_per_unit": last_amount,
            "last_distribution_yield_pct": round(last_amount / nav * 100, 4) if last_amount is not None and nav else None,
            "trailing_12m_distribution_per_unit": round(twelve_month_sum, 6) if not trailing.empty else None,
            "trailing_12m_yield_pct": round(twelve_month_sum / nav * 100, 4) if nav and not trailing.empty else None,
            "has_paid_in_12m": bool(not trailing.empty),
        })
    rows.sort(key=lambda x: x["code"])
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data_month": f"{data_month[:4]}-{data_month[4:]}",
        "sources": {
            "aum_and_beneficiaries": "SITCA ETF monthly detail",
            "listing_registry": "TWSE ETF Fund Basic Information Summary (Open Data dataset 157399)",
            "listing_registry_url": "https://data.gov.tw/dataset/157399",
            "dividends": "SITCA / Government Open Data dataset 160547 (irregular updates)",
            "aum_url": ETF_PAGE.format(ym=data_month),
            "dividend_url": "https://data.gov.tw/dataset/160547",
            "holders_url": "https://data.gov.tw/dataset/11452",
        },
        "count": len(rows), "etfs": rows,
    }
    temp = OUT.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(OUT)
    print(f"Wrote {len(rows)} ETFs; source month {payload['data_month']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ETF data refresh failed: {exc}", file=sys.stderr)
        sys.exit(1)
