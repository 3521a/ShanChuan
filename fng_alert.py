import os
import json
import time
import ssl
import smtplib
import logging
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.header import Header
from html import escape

import requests


# =========================================================
# 配置
# =========================================================

LOW = 25
HIGH = 75
BEIJING = timezone(timedelta(hours=8))
FORCE = os.getenv("FORCE_SEND", "false").lower() == "true"

CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"

# 关键修正：使用 Macintosh UA，避免 CNN 对 Windows UA 返回 418
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://edition.cnn.com/",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


# =========================================================
# 情绪分区
# =========================================================

ZONES = [
    (25,  "极度恐惧", "#dc2626", "#fef2f2"),
    (45,  "恐惧",     "#f97316", "#fff7ed"),
    (55,  "中性",     "#6b7280", "#f3f4f6"),
    (75,  "贪婪",     "#10b981", "#ecfdf5"),
    (101, "极度贪婪", "#059669", "#ecfdf5"),
]


def zone(score):
    for limit, label, color, bg in ZONES:
        if score < limit:
            return label, color, bg
    return "极度贪婪", "#059669", "#ecfdf5"


def valid_score(value):
    try:
        value = float(value)
        if 0 <= value <= 100:
            return value
    except (TypeError, ValueError):
        pass
    return None


def rating_cn(rating):
    mapping = {
        "extreme fear":  "极度恐惧",
        "fear":          "恐惧",
        "neutral":       "中性",
        "greed":         "贪婪",
        "extreme greed": "极度贪婪",
    }
    return mapping.get(str(rating or "").strip().lower(), "")


def number(value, digits=2):
    try:
        x = float(value)
        if x != x or abs(x) == float("inf"):
            return "—"
        return f"{x:,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def pct_change(current, previous):
    try:
        current = float(current)
        previous = float(previous)
        if previous == 0:
            return None
        return (current / previous - 1) * 100
    except (TypeError, ValueError):
        return None


def pct_text(value):
    if value is None:
        return "—"
    return f"{value:+.2f}%"


def pct_color(value):
    if value is None:
        return "#94a3b8"
    if value > 0:
        return "#059669"
    if value < 0:
        return "#dc2626"
    return "#64748b"


# =========================================================
# CNN 恐惧与贪婪指数
# =========================================================

def get_cnn_data():
    last_error = None

    for attempt in range(3):
        try:
            resp = requests.get(CNN_URL, headers=HEADERS, timeout=25)
            resp.raise_for_status()
            data = resp.json()

            score = valid_score((data.get("fear_and_greed") or {}).get("score"))
            if score is None:
                raise ValueError("CNN 响应中没有有效指数")

            logging.info("CNN 指数：%.2f", score)
            return data, score

        except Exception as exc:
            last_error = exc
            logging.warning("CNN 请求第 %s/3 次失败：%s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(2 ** attempt)

    raise RuntimeError("无法获取 CNN 指数") from last_error


# 子指标：键名可能随 CNN 版本变化，用别名列表动态探测
INDICATOR_DEFS = [
    {"display": "标普 500 市场动量", "keys": ["market_momentum_sp500", "market_momentum_sp125"]},
    {"display": "股价强度",          "keys": ["stock_price_strength"]},
    {"display": "股价广度",          "keys": ["stock_price_breadth"]},
    {"display": "看跌 / 看涨期权",   "keys": ["put_call_options", "put_call_ratio"]},
    {"display": "市场波动率",        "keys": ["market_volatility_vix", "market_volatility_vix_50"]},
    {"display": "避险需求",          "keys": ["safe_haven_demand"]},
    {"display": "垃圾债需求",        "keys": ["junk_bond_demand"]},
]


def extract_indicator(data, keys):
    for key in keys:
        item = data.get(key)
        if not isinstance(item, dict):
            continue
        score = valid_score(item.get("score"))
        if score is None:
            continue
        return {
            "score": score,
            "rating": item.get("rating") or "",
            "previous_close": valid_score(item.get("previous_close")),
        }
    return None


# =========================================================
# Yahoo 行情
# =========================================================

def get_yahoo_chart(symbol, range_value="3mo"):
    url = f"{YAHOO_CHART_URL}{symbol}"
    resp = requests.get(
        url,
        params={"range": range_value, "interval": "1d"},
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    result = (resp.json().get("chart", {}) or {}).get("result")
    if not result:
        raise ValueError(f"{symbol} 没有可用行情")

    item = result[0]
    timestamps = item.get("timestamp") or []
    quote = ((item.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    points = []
    for i, ts in enumerate(timestamps):
        if i >= len(closes):
            break
        close = closes[i]
        if close is None:
            continue
        try:
            points.append({
                "timestamp": int(ts),
                "close": float(close),
                "volume": float(volumes[i]) if i < len(volumes) and volumes[i] is not None else None,
            })
        except (TypeError, ValueError):
            continue

    if not points:
        raise ValueError(f"{symbol} 没有有效收盘价")

    latest = points[-1]
    previous = points[-2] if len(points) >= 2 else None

    def trailing(days):
        if len(points) <= days:
            return None
        return pct_change(latest["close"], points[-(days + 1)]["close"])

    latest_date = datetime.fromtimestamp(latest["timestamp"], tz=timezone.utc).astimezone(BEIJING)

    return {
        "symbol": symbol,
        "price": latest["close"],
        "daily_return": pct_change(latest["close"], previous["close"]) if previous else None,
        "week_return": trailing(5),
        "month_return": trailing(21),
        "date": latest_date.strftime("%Y-%m-%d"),
    }


def get_market_data():
    symbols = {
        "SPY":  "标普 500 ETF",
        "VOO":  "先锋标普 500",
        "IVV":  "iShares 标普 500",
        "RSP":  "等权重标普 500",
        "^VIX": "VIX 波动率",
    }
    result, errors = {}, {}
    for symbol, name in symbols.items():
        try:
            result[symbol] = get_yahoo_chart(symbol)
            result[symbol]["name"] = name
            logging.info("%s 行情成功：%s", symbol, result[symbol]["date"])
        except Exception as exc:
            errors[symbol] = str(exc)
            logging.warning("%s 行情失败：%s", symbol, exc)
    return result, errors


# =========================================================
# HTML 组件
# =========================================================

def render_progress(score, color, height=6):
    pct = max(0, min(100, float(score)))
    return (
        f'<div style="background:#e5e7eb;border-radius:99px;height:{height}px;'
        f'overflow:hidden;width:100%">'
        f'<div style="background:{color};height:{height}px;width:{pct:.1f}%;'
        f'border-radius:99px"></div>'
        f'</div>'
    )


def hero_card(score, label, color):
    pct = f"{score:.1f}"
    return f"""
    <div style="background-color:#0f172a;
      background-image:linear-gradient(135deg,#1e293b 0%,#0f172a 100%);
      border-radius:20px;padding:32px 28px;color:#ffffff">
      <div style="font-size:11px;letter-spacing:3px;color:#94a3b8;
        text-transform:uppercase;font-weight:600">
        CNN FEAR &amp; GREED INDEX
      </div>
      <div style="margin-top:18px;display:inline-block">
        <span style="font-size:64px;font-weight:800;line-height:1;
          color:#ffffff;letter-spacing:-2px">{pct}</span>
        <span style="font-size:20px;color:#94a3b8;font-weight:500"> / 100</span>
      </div>
      <div style="margin-top:16px">
        <span style="display:inline-block;padding:6px 14px;
          background-color:{color};color:#ffffff;border-radius:99px;
          font-size:13px;font-weight:600;letter-spacing:0.5px">{label}</span>
      </div>
      <div style="margin-top:22px">{render_progress(score, color, 10)}</div>
      <div style="margin-top:10px;font-size:11px;color:#64748b;
        display:flex;justify-content:space-between">
        <span>0 极度恐惧</span><span>50 中性</span><span>100 极度贪婪</span>
      </div>
    </div>
    """


def history_cell(title, value):
    if value is None:
        return f"""
        <td width="50%" style="padding:6px;vertical-align:top">
          <div style="background:#f8fafc;border-radius:12px;padding:14px">
            <div style="font-size:11px;color:#94a3b8">{title}</div>
            <div style="font-size:22px;font-weight:700;color:#94a3b8;
              margin-top:6px">—</div>
            <div style="font-size:11px;color:#94a3b8;margin-top:4px">暂无数据</div>
          </div>
        </td>
        """
    label, color, bg = zone(value)
    return f"""
    <td width="50%" style="padding:6px;vertical-align:top">
      <div style="background:{bg};border-radius:12px;padding:14px">
        <div style="font-size:11px;color:#64748b">{title}</div>
        <div style="font-size:22px;font-weight:700;color:{color};
          margin-top:6px">{value:.1f}</div>
        <div style="font-size:11px;color:{color};font-weight:600;
          margin-top:4px">{label}</div>
      </div>
    </td>
    """


def history_card(fg):
    cells = [
        ("昨日收盘", valid_score(fg.get("previous_close"))),
        ("一周前",   valid_score(fg.get("previous_1_week"))),
        ("一个月前", valid_score(fg.get("previous_1_month"))),
        ("一年前",   valid_score(fg.get("previous_1_year"))),
    ]
    rows = ""
    for i in range(0, 4, 2):
        rows += (
            "<tr>"
            + history_cell(*cells[i])
            + history_cell(*cells[i + 1])
            + "</tr>"
        )
    return f"""
    <div style="background:#ffffff;border-radius:20px;padding:24px;
      margin-top:16px;box-shadow:0 1px 3px rgba(15,23,42,0.04)">
      <div style="font-size:14px;font-weight:700;color:#0f172a">
        历史情绪对比
      </div>
      <div style="font-size:11px;color:#94a3b8;margin-top:4px;margin-bottom:14px">
        CNN 公布的历史参考值，用于观察情绪变化方向
      </div>
      <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
        {rows}
      </table>
    </div>
    """


def indicator_row(title, info):
    if info is None:
        return f"""
        <tr>
          <td style="padding:12px 0;border-bottom:1px solid #f1f5f9;
            font-size:13px;color:#94a3b8">{title}</td>
          <td style="padding:12px 12px;border-bottom:1px solid #f1f5f9;
            font-size:11px;color:#94a3b8;text-align:right">—</td>
          <td style="padding:12px 0;border-bottom:1px solid #f1f5f9;
            width:32px;text-align:right;font-size:13px;color:#cbd5e1">—</td>
        </tr>
        """
    score = info["score"]
    label, color, _ = zone(score)
    rating = rating_cn(info["rating"]) or label
    return f"""
    <tr>
      <td style="padding:14px 0;border-bottom:1px solid #f1f5f9;
        font-size:13px;color:#0f172a;font-weight:600">
        {escape(title)}
        <div style="font-size:11px;color:{color};font-weight:500;
          margin-top:2px">{rating}</div>
      </td>
      <td style="padding:14px 12px;border-bottom:1px solid #f1f5f9;
        width:45%">{render_progress(score, color, 6)}</td>
      <td style="padding:14px 0;border-bottom:1px solid #f1f5f9;
        width:44px;text-align:right;font-size:15px;font-weight:700;
        color:{color}">{score:.0f}</td>
    </tr>
    """


def indicators_card(data):
    rows = ""
    available = 0
    for item in INDICATOR_DEFS:
        info = extract_indicator(data, item["keys"])
        if info:
            available += 1
        rows += indicator_row(item["display"], info)

    return f"""
    <div style="background:#ffffff;border-radius:20px;padding:24px;
      margin-top:16px;box-shadow:0 1px 3px rgba(15,23,42,0.04)">
      <div style="font-size:14px;font-weight:700;color:#0f172a">
        CNN 七项子指标
      </div>
      <div style="font-size:11px;color:#94a3b8;margin-top:4px;margin-bottom:6px">
        综合指数由七项子指标等权合成；本次成功解析 {available}/7 项
      </div>
      <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
        {rows}
      </table>
    </div>
    """


def market_row(name, info):
    if not info:
        return f"""
        <tr>
          <td style="padding:12px 0;border-bottom:1px solid #f1f5f9;
            font-size:13px;color:#0f172a;font-weight:600">{escape(name)}</td>
          <td colspan="4" style="padding:12px 0;border-bottom:1px solid #f1f5f9;
            font-size:11px;color:#94a3b8;text-align:right">行情暂不可用</td>
        </tr>
        """
    daily = info.get("daily_return")
    week = info.get("week_return")
    month = info.get("month_return")

    def cell(value):
        return (
            f'<td style="padding:12px 4px;border-bottom:1px solid #f1f5f9;'
            f'text-align:right;font-size:12px;font-weight:600;color:{pct_color(value)}">'
            f'{pct_text(value)}</td>'
        )

    return f"""
    <tr>
      <td style="padding:12px 0;border-bottom:1px solid #f1f5f9;
        font-size:13px;color:#0f172a;font-weight:600">{escape(name)}</td>
      <td style="padding:12px 4px;border-bottom:1px solid #f1f5f9;
        text-align:right;font-size:13px;color:#0f172a;font-weight:600">
        {number(info.get("price"))}</td>
      {cell(daily)}{cell(week)}{cell(month)}
    </tr>
    """


def market_card(market, errors):
    rows = ""
    for symbol, display in [
        ("SPY",  "标普 500 ETF（SPY）"),
        ("VOO",  "先锋标普 500（VOO）"),
        ("IVV",  "iShares 标普 500（IVV）"),
        ("RSP",  "等权重标普 500（RSP）"),
        ("^VIX", "VIX 波动率指数"),
    ]:
        rows += market_row(display, market.get(symbol))

    dates = sorted({v.get("date") for v in market.values() if v.get("date")})
    date_text = "、".join(dates) if dates else "行情暂不可用"
    err_text = (
        "行情缺失：" + "；".join(f"{k}: {v}" for k, v in errors.items())
        if errors else ""
    )

    return f"""
    <div style="background:#ffffff;border-radius:20px;padding:24px;
      margin-top:16px;box-shadow:0 1px 3px rgba(15,23,42,0.04)">
      <div style="font-size:14px;font-weight:700;color:#0f172a">
        市场行情快照
      </div>
      <div style="font-size:11px;color:#94a3b8;margin-top:4px;margin-bottom:14px">
        价格表现基于 Yahoo Finance 日线数据 · {escape(date_text)}
      </div>
      <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
        <tr>
          <td style="padding:8px 0;font-size:11px;color:#64748b;
            border-bottom:1px solid #e2e8f0;font-weight:600">标的</td>
          <td style="padding:8px 4px;font-size:11px;color:#64748b;
            border-bottom:1px solid #e2e8f0;font-weight:600;text-align:right">最新</td>
          <td style="padding:8px 4px;font-size:11px;color:#64748b;
            border-bottom:1px solid #e2e8f0;font-weight:600;text-align:right">日</td>
          <td style="padding:8px 4px;font-size:11px;color:#64748b;
            border-bottom:1px solid #e2e8f0;font-weight:600;text-align:right">约一周</td>
          <td style="padding:8px 4px;font-size:11px;color:#64748b;
            border-bottom:1px solid #e2e8f0;font-weight:600;text-align:right">约一月</td>
        </tr>
        {rows}
      </table>
      <div style="font-size:11px;color:#94a3b8;margin-top:10px;line-height:1.6">
        ETF 价格涨跌与成交量不等于净申购赎回金额；VIX 为波动率指数，不可直接买入。
        {escape(err_text)}
      </div>
    </div>
    """


def analysis_card(score, market):
    notes = []
    if score < LOW:
        notes.append("CNN 指数位于极度恐惧区间，市场情绪偏谨慎。这本身不能证明市场已经见底。")
    elif score > HIGH:
        notes.append("CNN 指数位于极度贪婪区间，需留意拥挤交易与回撤风险。这本身不能证明市场即将见顶。")
    else:
        notes.append("CNN 指数未处于极端区间，仍需结合价格趋势、市场宽度与波动率判断风险。")

    spy = market.get("SPY")
    rsp = market.get("RSP")
    if spy and rsp and spy.get("week_return") is not None and rsp.get("week_return") is not None:
        gap = spy["week_return"] - rsp["week_return"]
        if gap > 1.0:
            notes.append("SPY 近似一周表现明显强于 RSP，市值加权指数相对占优；需留意上涨是否集中于大型权重股。")
        elif gap < -1.0:
            notes.append("RSP 近似一周表现强于 SPY，等权重指数相对占优，可能反映上涨参与度有所扩散。")
        else:
            notes.append("SPY 与 RSP 近似一周表现接近，暂未见到明显的相对强弱分化。")

    vix = market.get("^VIX")
    if vix and vix.get("daily_return") is not None:
        d = vix["daily_return"]
        if d > 5:
            notes.append("VIX 单日涨幅较大，市场对风险的定价有所上升。")
        elif d < -5:
            notes.append("VIX 单日跌幅较大，短期市场压力有所缓和；仍需观察是否持续。")

    notes.append("以上为自动规则生成的观察线索，不作为买卖指令，也不代表 CNN 指数的驱动机制。")

    list_html = "".join(
        f'<li style="margin:0 0 10px;line-height:1.7;font-size:13px;color:#334155">'
        f'{escape(n)}</li>'
        for n in notes
    )

    return f"""
    <div style="background:#ffffff;border-radius:20px;padding:24px;
      margin-top:16px;box-shadow:0 1px 3px rgba(15,23,42,0.04)">
      <div style="font-size:14px;font-weight:700;color:#0f172a">
        市场观察
      </div>
      <div style="font-size:11px;color:#94a3b8;margin-top:4px;margin-bottom:14px">
        自动分析规则输出，仅供参考
      </div>
      <ul style="padding-left:18px;margin:0">{list_html}</ul>
    </div>
    """


def footer_card(error_text, timestamp_text):
    return f"""
    <div style="padding:22px 8px;text-align:center">
      <div style="font-size:11px;color:#94a3b8;line-height:1.8">
        CNN 数据时间字段：{escape(timestamp_text)}<br>
        行情来源：Yahoo Finance 日线接口<br>
        触发阈值：严格低于 {LOW} 或严格高于 {HIGH} 时发送提醒<br>
        {escape(error_text)}<br>
        本报告仅供研究参考，不构成投资建议。
      </div>
    </div>
    """


# =========================================================
# 邮件生成
# =========================================================

def build_email(data, score, market, market_errors):
    now = datetime.now(BEIJING)
    fg = data.get("fear_and_greed") or {}
    label, color, _ = zone(score)

    timestamp_text = fg.get("timestamp") or "CNN 未提供更新时间"
    error_text = (
        "部分行情源未返回：" + "；".join(f"{k}: {v}" for k, v in market_errors.items())
        if market_errors else "本次所请求的行情均已返回。"
    )

    body = (
        hero_card(score, label, color)
        + history_card(fg)
        + indicators_card(data)
        + market_card(market, market_errors)
        + analysis_card(score, market)
    )

    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body style="margin:0;padding:24px 12px;background:#f1f5f9;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',
  'Microsoft YaHei',Arial,sans-serif;color:#0f172a">
<div style="max-width:640px;margin:0 auto">
  <div style="padding:0 8px 14px;display:flex;justify-content:space-between;
    align-items:center">
    <div>
      <div style="font-size:11px;letter-spacing:3px;color:#94a3b8;
        text-transform:uppercase;font-weight:600">DAILY BRIEF</div>
      <div style="font-size:18px;font-weight:700;color:#0f172a;
        margin-top:2px">美股风险监测</div>
    </div>
    <div style="font-size:11px;color:#94a3b8;text-align:right">
      {now:%Y-%m-%d %H:%M}<br>北京时间
    </div>
  </div>

  {body}
  {footer_card(error_text, timestamp_text)}
</div>
</body>
</html>"""

    plain = f"""美股风险监测
生成时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）

CNN 恐惧与贪婪指数：{score:.1f}/100（{label}）
昨日：{fg.get('previous_close') or '—'}   一周前：{fg.get('previous_1_week') or '—'}
一月前：{fg.get('previous_1_month') or '—'}   一年前：{fg.get('previous_1_year') or '—'}

市场观察：
""" + "\n".join(
        f"- {n}" for n in [
            "CNN 指数由七项子指标等权合成，请结合价格趋势、市场宽度与波动率判断。",
            "ETF 价格涨跌和成交量不等于净申购赎回金额。",
            "本报告仅供参考，不构成投资建议。",
        ]
    )

    return plain, html


# =========================================================
# 发送邮件
# =========================================================

def send_email(subject, plain, html):
    username = os.environ.get("MAIL_USER")
    password = os.environ.get("MAIL_PASS")
    recipient = os.environ.get("MAIL_TO")

    if not username or not password or not recipient:
        raise RuntimeError("邮件配置不完整：MAIL_USER / MAIL_PASS / MAIL_TO")

    message = EmailMessage()
    message["Subject"] = str(Header(subject, "utf-8"))
    message["From"] = username
    message["To"] = recipient
    message.set_content(plain)
    message.add_alternative(html, subtype="html")

    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))

    with smtplib.SMTP_SSL(host, port, timeout=30,
                          context=ssl.create_default_context()) as server:
        server.login(username, password)
        server.send_message(message)

    logging.info("邮件发送成功：%s", recipient)


# =========================================================
# 主程序
# =========================================================

def main():
    now = datetime.now(BEIJING)
    logging.info("北京时间：%s", now.isoformat())

    if FORCE:
        plain = "邮件系统测试成功。\n此邮件不代表当前市场触发阈值。\n"
        html = """<html><body style="font-family:-apple-system,Arial,sans-serif;
        padding:24px;color:#0f172a">
        <h2>美股风险监测 · 测试</h2>
        <p>邮件配置可用，此邮件不包含实时数据。</p>
        </body></html>"""
        send_email("[测试] 美股风险监测", plain, html)
        return

    data, score = get_cnn_data()

    if not (score < LOW or score > HIGH):
        logging.info("当前指数 %.2f，未严格低于 %s 或高于 %s，不发送提醒。",
                     score, LOW, HIGH)
        return

    market, errors = get_market_data()
    plain, html = build_email(data, score, market, errors)

    if score < LOW:
        subject = f"[极度恐惧提醒] CNN 指数 {score:.1f}/100"
    else:
        subject = f"[极度贪婪提醒] CNN 指数 {score:.1f}/100"

    send_email(subject, plain, html)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("程序运行失败")
        raise
