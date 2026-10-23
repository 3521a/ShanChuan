import os
import json
import time
import ssl
import base64
import smtplib
import logging
from io import BytesIO
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.header import Header
from html import escape

import requests
import matplotlib
matplotlib.use("Agg")  # 无头模式，适合服务器运行
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# =========================================================
# 配置
# =========================================================
LOW = 25
HIGH = 75
BEIJING = timezone(timedelta(hours=8))
FORCE = os.getenv("FORCE_SEND", "false").lower() == "true"

CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

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
        current, previous = float(current), float(previous)
        if previous == 0:
            return None
        return (current / previous - 1) * 100
    except (TypeError, ValueError):
        return None

def pct_text(value):
    return f"{value:+.2f}%" if value is not None else "—"

def pct_color(value):
    if value is None: return "#94a3b8"
    return "#059669" if value > 0 else "#dc2626" if value < 0 else "#64748b"

# =========================================================
# 获取 CNN 数据（含历史走势）
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

            # 解析近一年历史数据
            history = []
            hist_data = (data.get("fear_and_greed_historical") or {}).get("data", [])
            for point in hist_data:
                try:
                    ts = int(point.get("x")) / 1000
                    val = float(point.get("y"))
                    dt = datetime.fromtimestamp(ts, tz=BEIJING)
                    history.append((dt, val))
                except (TypeError, ValueError, KeyError):
                    continue

            logging.info("CNN 指数：%.2f，历史数据点：%d", score, len(history))
            return data, score, history

        except Exception as exc:
            last_error = exc
            logging.warning("CNN 请求第 %s/3 次失败：%s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(2 ** attempt)

    raise RuntimeError("无法获取 CNN 指数") from last_error

# =========================================================
# 生成走势图（base64 内联）
# =========================================================
def generate_trend_chart(history):
    if not history:
        return None

    dates = [h[0] for h in history]
    scores = [h[1] for h in history]

    fig, ax = plt.subplots(figsize=(8, 2.6), dpi=150)
    fig.patch.set_facecolor("#0f172a")
    ax.set_facecolor("#0f172a")

    # 阈值线
    ax.axhline(y=LOW, color="#ef4444", linewidth=0.8, linestyle="--", alpha=0.6)
    ax.axhline(y=HIGH, color="#10b981", linewidth=0.8, linestyle="--", alpha=0.6)

    # 走势线
    ax.plot(dates, scores, color="#38bdf8", linewidth=1.5, alpha=0.9)

    # 填充
    ax.fill_between(dates, scores, LOW, where=[s >= LOW for s in scores],
                    color="#38bdf8", alpha=0.1, interpolate=True)
    ax.fill_between(dates, scores, LOW, where=[s < LOW for s in scores],
                    color="#ef4444", alpha=0.15, interpolate=True)

    # 格式
    ax.set_ylim(0, 100)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.tick_params(colors="#64748b", labelsize=7)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax.grid(axis="y", color="#1e293b", linewidth=0.6)
    for spine in ax.spines.values():
        spine.set_visible(False)

    plt.tight_layout(pad=1.2)
    buf = BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode("utf-8")
    return f"data:image/png;base64,{b64}"

# =========================================================
# 获取 Yahoo 行情（精简：去掉 VOO / IVV）
# =========================================================
def get_yahoo_chart(symbol, range_value="3mo"):
    resp = requests.get(f"{YAHOO_CHART_URL}{symbol}",
                        params={"range": range_value, "interval": "1d"},
                        headers=HEADERS, timeout=20)
    resp.raise_for_status()
    result = (resp.json().get("chart", {}) or {}).get("result")
    if not result:
        raise ValueError(f"{symbol} 没有可用行情")

    item = result[0]
    timestamps = item.get("timestamp") or []
    quote = ((item.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []

    points = []
    for i, ts in enumerate(timestamps):
        if i >= len(closes) or closes[i] is None:
            continue
        try:
            points.append({"timestamp": int(ts), "close": float(closes[i])})
        except (TypeError, ValueError):
            continue

    if not points:
        raise ValueError(f"{symbol} 没有有效收盘价")

    latest = points[-1]
    prev = points[-2] if len(points) >= 2 else None

    def trailing(days):
        return pct_change(latest["close"], points[-(days + 1)]["close"]) if len(points) > days else None

    return {
        "symbol": symbol,
        "price": latest["close"],
        "daily_return": pct_change(latest["close"], prev["close"]) if prev else None,
        "week_return": trailing(5),
        "month_return": trailing(21),
        "date": datetime.fromtimestamp(latest["timestamp"], tz=BEIJING).strftime("%Y-%m-%d"),
    }

def get_market_data():
    symbols = {"SPY": "标普 500 ETF", "RSP": "等权重标普 500", "^VIX": "VIX 波动率"}
    result, errors = {}, {}
    for symbol, name in symbols.items():
        try:
            result[symbol] = get_yahoo_chart(symbol)
            result[symbol]["name"] = name
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
        f'<div style="background:#e5e7eb;border-radius:99px;height:{height}px;overflow:hidden;width:100%">'
        f'<div style="background:{color};height:{height}px;width:{pct:.1f}%;border-radius:99px"></div></div>'
    )

def hero_card(score, label, color, chart_b64):
    chart_html = f'<div style="margin-top:24px"><img src="{chart_b64}" alt="近一年走势" style="width:100%;border-radius:8px;display:block"></div>' if chart_b64 else ""
    return f"""
    <div style="background-color:#0f172a;background-image:linear-gradient(135deg,#1e293b 0%,#0f172a 100%);border-radius:20px;padding:32px 28px;color:#ffffff">
      <div style="font-size:11px;letter-spacing:3px;color:#94a3b8;text-transform:uppercase;font-weight:600">CNN FEAR &amp; GREED INDEX</div>
      <div style="margin-top:18px">
        <span style="font-size:64px;font-weight:800;line-height:1;color:#ffffff;letter-spacing:-2px">{score:.1f}</span>
        <span style="font-size:20px;color:#94a3b8;font-weight:500"> / 100</span>
      </div>
      <div style="margin-top:16px">
        <span style="display:inline-block;padding:6px 14px;background-color:{color};color:#ffffff;border-radius:99px;font-size:13px;font-weight:600">{label}</span>
      </div>
      <div style="margin-top:22px">{render_progress(score, color, 10)}</div>
      {chart_html}
    </div>
    """

# 精简后的子指标（去掉重叠项）
INDICATOR_DEFS = [
    {"display": "市场动量", "keys": ["market_momentum_sp500", "market_momentum_sp125"]},
    {"display": "市场广度", "keys": ["stock_price_breadth"]},
    {"display": "市场波动率", "keys": ["market_volatility_vix"]},
    {"display": "垃圾债需求", "keys": ["junk_bond_demand"]},
]

def extract_indicator(data, keys):
    for key in keys:
        item = data.get(key)
        if isinstance(item, dict):
            score = valid_score(item.get("score"))
            if score is not None:
                return {"score": score, "rating": item.get("rating") or ""}
    return None

def indicators_card(data):
    rows = ""
    for item in INDICATOR_DEFS:
        info = extract_indicator(data, item["keys"])
        if info:
            label, color, _ = zone(info["score"])
            rows += f"""
            <tr>
              <td style="padding:12px 0;border-bottom:1px solid #f1f5f9;font-size:13px;color:#0f172a;font-weight:600">{escape(item['display'])}</td>
              <td style="padding:12px 8px;border-bottom:1px solid #f1f5f9;width:40%">{render_progress(info['score'], color, 6)}</td>
              <td style="padding:12px 0;border-bottom:1px solid #f1f5f9;width:44px;text-align:right;font-size:15px;font-weight:700;color:{color}">{info['score']:.0f}</td>
            </tr>
            """
    return f"""
    <div style="background:#ffffff;border-radius:20px;padding:24px;margin-top:16px;box-shadow:0 1px 3px rgba(15,23,42,0.04)">
      <div style="font-size:14px;font-weight:700;color:#0f172a">核心子指标</div>
      <div style="font-size:11px;color:#94a3b8;margin-top:4px;margin-bottom:6px">精简展示 CNN 驱动指数的关键维度</div>
      <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">{rows}</table>
    </div>
    """

def market_card(market):
    rows = ""
    for symbol in ["SPY", "RSP", "^VIX"]:
        info = market.get(symbol)
        if not info:
            rows += f'<tr><td colspan="4" style="padding:10px 0;font-size:12px;color:#94a3b8">行情暂不可用</td></tr>'
            continue
        daily, week, month = info.get("daily_return"), info.get("week_return"), info.get("month_return")
        rows += f"""
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;color:#0f172a;font-weight:600">{escape(info['name'])}</td>
          <td style="padding:10px 4px;border-bottom:1px solid #f1f5f9;text-align:right;font-size:13px;font-weight:600">{number(info.get('price'))}</td>
          <td style="padding:10px 4px;border-bottom:1px solid #f1f5f9;text-align:right;font-size:12px;color:{pct_color(daily)}">{pct_text(daily)}</td>
          <td style="padding:10px 4px;border-bottom:1px solid #f1f5f9;text-align:right;font-size:12px;color:{pct_color(week)}">{pct_text(week)}</td>
          <td style="padding:10px 4px;border-bottom:1px solid #f1f5f9;text-align:right;font-size:12px;color:{pct_color(month)}">{pct_text(month)}</td>
        </tr>
        """
    return f"""
    <div style="background:#ffffff;border-radius:20px;padding:24px;margin-top:16px;box-shadow:0 1px 3px rgba(15,23,42,0.04)">
      <div style="font-size:14px;font-weight:700;color:#0f172a">关键行情对比</div>
      <div style="font-size:11px;color:#94a3b8;margin-top:4px;margin-bottom:14px">SPY（市值加权） vs RSP（等权重）</div>
      <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
        <tr>
          <td style="padding:8px 0;font-size:11px;color:#64748b;border-bottom:1px solid #e2e8f0;font-weight:600">标的</td>
          <td style="padding:8px 4px;font-size:11px;color:#64748b;border-bottom:1px solid #e2e8f0;font-weight:600;text-align:right">最新</td>
          <td style="padding:8px 4px;font-size:11px;color:#64748b;border-bottom:1px solid #e2e8f0;font-weight:600;text-align:right">日</td>
          <td style="padding:8px 4px;font-size:11px;color:#64748b;border-bottom:1px solid #e2e8f0;font-weight:600;text-align:right">周</td>
          <td style="padding:8px 4px;font-size:11px;color:#64748b;border-bottom:1px solid #e2e8f0;font-weight:600;text-align:right">月</td>
        </tr>
        {rows}
      </table>
    </div>
    """

def analysis_card(score, market):
    notes = []
    if score < LOW:
        notes.append("指数位于极度恐惧区间，情绪极端不代表市场已经见底。")
    elif score > HIGH:
        notes.append("指数位于极度贪婪区间，需留意拥挤交易与回撤风险。")
    else:
        notes.append("指数未处于极端区间，仍需结合趋势与波动率判断。")

    spy, rsp = market.get("SPY"), market.get("RSP")
    if spy and rsp and spy.get("week_return") is not None and rsp.get("week_return") is not None:
        gap = spy["week_return"] - rsp["week_return"]
        if gap > 1.0:
            notes.append("SPY 周表现明显强于 RSP，上涨可能集中于大型权重股。")
        elif gap < -1.0:
            notes.append("RSP 周表现强于 SPY，等权重占优，上涨参与度可能更广。")

    vix = market.get("^VIX")
    if vix and vix.get("daily_return") is not None:
        if vix["daily_return"] > 5:
            notes.append("VIX 单日涨幅较大，风险定价升温。")
        elif vix["daily_return"] < -5:
            notes.append("VIX 单日跌幅较大，短期压力有所缓和。")

    notes.append("以上为自动规则生成的观察线索，不构成买卖指令。")

    items = "".join(f'<li style="margin:0 0 8px;line-height:1.7;font-size:13px;color:#334155">{escape(n)}</li>' for n in notes)
    return f"""
    <div style="background:#ffffff;border-radius:20px;padding:24px;margin-top:16px;box-shadow:0 1px 3px rgba(15,23,42,0.04)">
      <div style="font-size:14px;font-weight:700;color:#0f172a">市场观察</div>
      <div style="font-size:11px;color:#94a3b8;margin-top:4px;margin-bottom:14px">规则型分析，仅供参考</div>
      <ul style="padding-left:18px;margin:0">{items}</ul>
    </div>
    """

# =========================================================
# 邮件生成
# =========================================================
def build_email(data, score, market):
    now = datetime.now(BEIJING)
    label, color, _ = zone(score)
    chart_b64 = generate_trend_chart(data.get("_history", []))

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px 12px;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;color:#0f172a">
<div style="max-width:640px;margin:0 auto">
  <div style="padding:0 8px 14px;display:flex;justify-content:space-between;align-items:center">
    <div>
      <div style="font-size:11px;letter-spacing:3px;color:#94a3b8;text-transform:uppercase;font-weight:600">DAILY BRIEF</div>
      <div style="font-size:18px;font-weight:700;color:#0f172a;margin-top:2px">美股风险监测</div>
    </div>
    <div style="font-size:11px;color:#94a3b8;text-align:right">{now:%Y-%m-%d %H:%M}<br>北京时间</div>
  </div>
  {hero_card(score, label, color, chart_b64)}
  {indicators_card(data)}
  {market_card(market)}
  {analysis_card(score, market)}
  <div style="padding:22px 8px;text-align:center;font-size:11px;color:#94a3b8;line-height:1.8">
    CNN 数据时间：{escape(str((data.get('fear_and_greed') or {}).get('timestamp') or '—'))}<br>
    触发阈值：严格低于 {LOW} 或严格高于 {HIGH}<br>
    本报告仅供研究参考，不构成投资建议。
  </div>
</div>
</body></html>"""

    plain = f"""美股风险监测
生成时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）
CNN 恐惧与贪婪指数：{score:.1f}/100（{label}）

市场观察：
- 指数位于{label}区间。
- SPY 与 RSP 的周表现对比见邮件正文。
- 本报告仅供参考，不构成投资建议。
"""
    return plain, html

# =========================================================
# 发送邮件
# =========================================================
def send_email(subject, plain, html):
    username, password, recipient = os.environ.get("MAIL_USER"), os.environ.get("MAIL_PASS"), os.environ.get("MAIL_TO")
    if not all([username, password, recipient]):
        raise RuntimeError("邮件配置不完整")

    msg = EmailMessage()
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"], msg["To"] = username, recipient
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")

    host, port = os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context()) as s:
        s.login(username, password)
        s.send_message(msg)

    logging.info("邮件发送成功：%s", recipient)

# =========================================================
# 主程序
# =========================================================
def main():
    if FORCE:
        plain = "邮件系统测试成功。\n"
        html = "<html><body><h2>测试邮件</h2><p>配置可用。</p></body></html>"
        send_email("[测试] 美股风险监测", plain, html)
        return

    data, score, history = get_cnn_data()

    if not (score < LOW or score > HIGH):
        logging.info("当前指数 %.2f，未触发阈值，不发送提醒。", score)
        return

    data["_history"] = history
    market, _ = get_market_data()
    plain, html = build_email(data, score, market)
    subject = f"[{'极度恐惧' if score < LOW else '极度贪婪'}提醒] CNN 指数 {score:.1f}/100"
    send_email(subject, plain, html)

if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("程序运行失败")
        raise
