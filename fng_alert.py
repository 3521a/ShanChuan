
import os
import json
import math
import html
import smtplib
import requests
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate
from urllib.parse import quote

# ==================== 基础配置 ====================

CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
SP500_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
FLOW_URL = "https://tickerinside.com/api/v1/etf-flows.json"

MAIL_USER = os.getenv("MAIL_USER", "").strip()
MAIL_PASS = os.getenv("MAIL_PASS", "").strip()
MAIL_TO = os.getenv("MAIL_TO", "").strip()

FORCE_SEND = os.getenv("FORCE_SEND", "false").lower() in (
    "1", "true", "yes", "on"
)

TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}


# ==================== 通用函数 ====================

def get_json(url):
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def safe_float(value):
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except (TypeError, ValueError):
        pass
    return None


def clamp(value, low=0, high=100):
    return max(low, min(high, value))


def fmt_number(value, digits=2):
    if value is None:
        return "暂无数据"
    return f"{value:,.{digits}f}"


def fmt_pct(value):
    if value is None:
        return "暂无数据"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def fmt_money(value):
    if value is None:
        return "暂无数据"

    sign = "+" if value > 0 else ("-" if value < 0 else "")
    amount = abs(value)

    if amount >= 1_000_000_000:
        return f"{sign}${amount / 1_000_000_000:.2f}B"
    if amount >= 1_000_000:
        return f"{sign}${amount / 1_000_000:.1f}M"
    return f"{sign}${amount:,.0f}"


def sentiment_label(score):
    if score < 25:
        return "极度恐惧"
    if score < 45:
        return "恐惧"
    if score <= 55:
        return "中性"
    if score <= 75:
        return "贪婪"
    return "极度贪婪"


def score_color(score):
    if score < 25:
        return "#b91c1c"
    if score < 45:
        return "#ea580c"
    if score <= 55:
        return "#64748b"
    if score <= 75:
        return "#16a34a"
    return "#047857"


def change_color(value):
    if value is None or value == 0:
        return "#64748b"
    return "#15803d" if value > 0 else "#b91c1c"


def html_escape(value):
    return html.escape(str(value))


# ==================== CNN 恐贪指数 ====================

def fetch_cnn():
    data = get_json(CNN_URL)

    current = data.get("fear_and_greed", {})
    score = safe_float(current.get("score"))

    if score is None:
        raise ValueError("CNN 返回数据中没有有效的恐贪指数")

    rating = current.get("rating", sentiment_label(score))
    timestamp = current.get("timestamp")

    if timestamp:
        try:
            date_text = datetime.fromtimestamp(
                float(timestamp), timezone.utc
            ).strftime("%Y-%m-%d")
        except (ValueError, TypeError, OSError):
            date_text = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    else:
        date_text = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    historical = data.get("fear_and_greed_historical", {})
    history_data = historical.get("data", [])

    points = []
    for item in history_data:
        if not isinstance(item, dict):
            continue

        value = safe_float(item.get("y"))
        stamp = safe_float(item.get("x"))

        if value is None or stamp is None:
            continue

        # CNN 历史时间戳通常为毫秒
        if stamp > 10_000_000_000:
            stamp /= 1000

        try:
            day = datetime.fromtimestamp(
                stamp, timezone.utc
            ).strftime("%m-%d")
        except (ValueError, OSError):
            continue

        points.append({"date": day, "score": value})

    points.sort(key=lambda item: item["date"])

    # 邮件中只展示最近 30 个有效历史点
    points = points[-30:]

    if not points:
        points = [{"date": date_text[5:], "score": score}]

    return {
        "score": score,
        "rating": str(rating),
        "date": date_text,
        "history": points,
    }


# ==================== Yahoo Finance 行情 ====================

def fetch_yahoo_chart(symbol, range_text="3mo"):
    url = YAHOO_URL.format(quote(symbol, safe=""))
    data = get_json(
        url + f"?range={range_text}&interval=1d&events=history"
    )

    chart = data.get("chart", {})
    if chart.get("error"):
        raise ValueError(f"{symbol} 行情接口返回错误")

    results = chart.get("result") or []
    if not results:
        raise ValueError(f"{symbol} 没有行情数据")

    result = results[0]
    timestamps = result.get("timestamp") or []
    quote_data = (result.get("indicators", {}).get("quote") or [{}])[0]
    closes = quote_data.get("close") or []

    rows = []
    for stamp, close in zip(timestamps, closes):
        close = safe_float(close)
        if close is None:
            continue

        try:
            date_text = datetime.fromtimestamp(
                stamp, timezone.utc
            ).strftime("%Y-%m-%d")
        except (ValueError, OSError):
            continue

        rows.append({"date": date_text, "close": close})

    if not rows:
        raise ValueError(f"{symbol} 没有有效收盘价")

    return rows


def pct_change(old, new):
    if old is None or new is None or old == 0:
        return None
    return (new / old - 1) * 100


def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def calc_rsi(values, period=14):
    if len(values) < period + 1:
        return 50.0

    changes = [
        values[i] - values[i - 1]
        for i in range(len(values) - period, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def fetch_sp500():
    rows = fetch_yahoo_chart("^GSPC", "3mo")
    closes = [row["close"] for row in rows]

    latest = closes[-1]
    day_return = pct_change(closes[-2], latest) if len(closes) >= 2 else None
    week_return = pct_change(closes[-6], latest) if len(closes) >= 6 else None
    month_return = pct_change(closes[-22], latest) if len(closes) >= 22 else None

    return {
        "date": rows[-1]["date"],
        "price": latest,
        "day_return": day_return,
        "week_return": week_return,
        "month_return": month_return,
        "history": rows[-30:],
    }


# ==================== 七姐妹自定义恐贪指数 ====================

MAG7 = [
    ("AAPL", "苹果"),
    ("MSFT", "微软"),
    ("NVDA", "英伟达"),
    ("AMZN", "亚马逊"),
    ("GOOGL", "谷歌"),
    ("META", "Meta"),
    ("TSLA", "特斯拉"),
]


def fetch_mag7_one(symbol, name):
    rows = fetch_yahoo_chart(symbol, "3mo")
    closes = [row["close"] for row in rows]

    latest = closes[-1]
    rsi = calc_rsi(closes, 14)

    ret5 = pct_change(closes[-6], latest) if len(closes) >= 6 else 0
    ret20 = pct_change(closes[-21], latest) if len(closes) >= 21 else 0

    ma20 = sma(closes, 20)
    ma50 = sma(closes, 50)

    # 将技术指标映射为 0–100 的自定义情绪分：
    # RSI 35%；20 日动量 25%；20 日均线趋势 25%；5 日动量 15%。
    rsi_component = rsi

    momentum20_component = clamp(
        50 + (ret20 or 0) * (50 / 15)
    )

    trend_component = 50
    if ma50:
        distance50 = (latest / ma50 - 1) * 100
        trend_component = clamp(50 + distance50 * 5)

    momentum5_component = clamp(
        50 + (ret5 or 0) * (50 / 8)
    )

    score = round(
        0.35 * rsi_component
        + 0.25 * momentum20_component
        + 0.25 * trend_component
        + 0.15 * momentum5_component
    )

    return {
        "symbol": symbol,
        "name": name,
        "price": latest,
        "score": int(clamp(score)),
        "label": sentiment_label(score),
        "day_return": (
            pct_change(closes[-2], latest) if len(closes) >= 2 else None
        ),
        "week_return": ret5,
        "month_return": ret20,
        "rsi": rsi,
        "date": rows[-1]["date"],
    }


def fetch_mag7():
    results = []
    for symbol, name in MAG7:
        try:
            results.append(fetch_mag7_one(symbol, name))
        except Exception as exc:
            print(f"[WARN] {symbol} 获取失败：{exc}")

    return results


# ==================== ETF 资金流数据 ====================

def recursive_find(obj, wanted_keys):
    """在 JSON 的嵌套结构中寻找指定键。"""
    if isinstance(obj, dict):
        for key in wanted_keys:
            if key in obj:
                return obj[key]

        for value in obj.values():
            found = recursive_find(value, wanted_keys)
            if found is not None:
                return found

    elif isinstance(obj, list):
        for value in obj:
            found = recursive_find(value, wanted_keys)
            if found is not None:
                return found

    return None


def extract_flow_rows(data):
    """
    兼容不同 JSON 结构：
    优先读取明确标注净流入/净流出的 ETF 列表。
    不把价格涨跌或成交量当作资金流。
    """
    positive_keys = [
        "largest_inflows", "top_inflows", "inflows",
        "biggest_inflows", "added_by_value"
    ]
    negative_keys = [
        "largest_outflows", "top_outflows", "outflows",
        "biggest_outflows", "reduced_by_value"
    ]

    positive = recursive_find(data, positive_keys)
    negative = recursive_find(data, negative_keys)

    rows = []

    def add_items(items, direction):
        if not isinstance(items, list):
            return

        for item in items[:10]:
            if not isinstance(item, dict):
                continue

            ticker = (
                item.get("ticker")
                or item.get("symbol")
                or item.get("fund")
                or item.get("name")
            )

            # 资金流 API 的字段可能为净流量美元值。
            amount = None
            for key in (
                "net_flow_usd", "net_flow", "flow_usd",
                "net_value_usd", "net_assets_flow_usd",
                "flow", "amount_usd"
            ):
                if key in item:
                    amount = safe_float(item.get(key))
                    if amount is not None:
                        break

            # 如果是明确的 inflow/outflow 分类，方向可由列表决定。
            if amount is not None and direction == "out":
                amount = -abs(amount)
            elif amount is not None and direction == "in":
                amount = abs(amount)

            if ticker and amount is not None:
                rows.append({
                    "ticker": str(ticker),
                    "amount": amount,
                    "direction": direction,
                })

    add_items(positive, "in")
    add_items(negative, "out")

    # 某些版本直接提供带正负号的 ETF 净流量数组。
    if not rows:
        direct = recursive_find(
            data, ["etfs", "funds", "flows", "daily_flows"]
        )

        if isinstance(direct, list):
            for item in direct:
                if not isinstance(item, dict):
                    continue

                ticker = item.get("ticker") or item.get("symbol")
                amount = None

                for key in (
                    "net_flow_usd", "net_flow", "flow_usd",
                    "net_value_usd", "flow"
                ):
                    if key in item:
                        amount = safe_float(item.get(key))
                        if amount is not None:
                            break

                if ticker and amount is not None:
                    rows.append({
                        "ticker": str(ticker),
                        "amount": amount,
                        "direction": "in" if amount >= 0 else "out",
                    })

    # 去重，保留每只 ETF 的首次记录
    seen = set()
    unique = []
    for row in rows:
        key = row["ticker"].upper()
        if key not in seen:
            seen.add(key)
            unique.append(row)

    return unique[:16]


def fetch_flows():
    try:
        data = get_json(FLOW_URL)
        date_text = (
            data.get("date")
            or data.get("as_of")
            or data.get("latest_date")
            or "以数据源公布日期为准"
        )

        rows = extract_flow_rows(data)

        # 尝试寻找公开 API 中的总净流量字段
        total = None
        for key in (
            "net_flow_usd", "total_net_flow_usd",
            "net_flow", "total_net_flow"
        ):
            if key in data:
                total = safe_float(data.get(key))
                if total is not None:
                    break

        # 如果没有可验证的明细或总值，不展示虚构图表
        if not rows and total is None:
            return {
                "ok": False,
                "date": date_text,
                "rows": [],
                "total": None,
                "message": "接口结构中没有识别到可验证的资金流字段。",
            }

        return {
            "ok": True,
            "date": date_text,
            "rows": rows,
            "total": total,
            "message": "",
        }

    except Exception as exc:
        print(f"[WARN] ETF 资金流获取失败：{exc}")
        return {
            "ok": False,
            "date": "暂无",
            "rows": [],
            "total": None,
            "message": "资金流数据源暂时不可用。",
        }


# ==================== HTML 图表 ====================

def make_bar(value, max_value=100, color=None, height=12):
    value = clamp(value, 0, max_value)
    width = value / max_value * 100

    if color is None:
        color = "#2563eb"

    return f"""
    <div style="background:#e5e7eb;border-radius:8px;height:{height}px;
                overflow:hidden;width:100%;">
      <div style="height:{height}px;width:{width:.1f}%;
                  background:{color};border-radius:8px;"></div>
    </div>
    """


def cnn_history_chart(points):
    if not points:
        return "<p>暂无历史数据。</p>"

    bars = []
    for point in points:
        score = clamp(point["score"])
        color = score_color(score)
        bars.append(f"""
        <td style="vertical-align:bottom;text-align:center;padding:2px 2px 0;
                   width:{100 / len(points):.2f}%;">
          <div title="{point['date']}：{score:.1f}"
               style="height:{max(4, score * 1.35):.1f}px;
                      background:{color};border-radius:3px 3px 0 0;
                      margin:0 auto;width:70%;"></div>
        </td>
        """)

    labels = []
    for i, point in enumerate(points):
        if i == 0 or i == len(points) - 1 or i % 5 == 0:
            label = point["date"]
        else:
            label = ""

        labels.append(
            f'<td style="font-size:10px;color:#64748b;text-align:center;'
            f'padding-top:4px;">{html_escape(label)}</td>'
        )

    return f"""
    <div style="margin-top:12px;">
      <div style="display:flex;justify-content:space-between;
                  font-size:12px;color:#64748b;margin-bottom:6px;">
        <span>极度恐惧 0</span>
        <span>中性 50</span>
        <span>极度贪婪 100</span>
      </div>
      <table role="presentation" cellpadding="0" cellspacing="0"
             style="width:100%;border-collapse:collapse;">
        <tr style="height:140px;vertical-align:bottom;">
          {''.join(bars)}
        </tr>
        <tr>{''.join(labels)}</tr>
      </table>
    </div>
    """


def sp500_chart(history):
    if not history:
        return "<p>暂无标普 500 历史行情。</p>"

    closes = [row["close"] for row in history]
    low = min(closes)
    high = max(closes)
    span = high - low or 1

    bars = []
    for row in history:
        height = 18 + (row["close"] - low) / span * 70
        bars.append(f"""
        <td title="{row['date']}：{row['close']:.2f}"
            style="vertical-align:bottom;width:{100 / len(history):.2f}%;
                   padding:1px;">
          <div style="height:{height:.1f}px;background:#2563eb;
                      border-radius:2px 2px 0 0;"></div>
        </td>
        """)

    return f"""
    <div style="margin-top:12px;">
      <div style="font-size:12px;color:#64748b;margin-bottom:6px;">
        标普 500 最近 {len(history)} 个交易日收盘走势（柱高表示相对价格）
      </div>
      <table role="presentation" cellpadding="0" cellspacing="0"
             style="width:100%;border-collapse:collapse;">
        <tr style="height:100px;vertical-align:bottom;">
          {''.join(bars)}
        </tr>
      </table>
      <div style="display:flex;justify-content:space-between;
                  font-size:11px;color:#64748b;margin-top:4px;">
        <span>{html_escape(history[0]['date'])}</span>
        <span>{html_escape(history[-1]['date'])}</span>
      </div>
    </div>
    """


def mag7_html_table(stocks):
    if not stocks:
        return "<p>七姐妹行情暂时不可用。</p>"

    rows = []
    for stock in stocks:
        score = stock["score"]
        color = score_color(score)

        rows.append(f"""
        <tr>
          <td style="padding:10px 6px;border-bottom:1px solid #e5e7eb;">
            <strong>{html_escape(stock['name'])}</strong>
            <div style="font-size:11px;color:#64748b;">
              {html_escape(stock['symbol'])}
            </div>
          </td>
          <td style="padding:10px 6px;width:42%;">
            <div style="font-weight:bold;color:{color};margin-bottom:5px;">
              {score} · {html_escape(stock['label'])}
            </div>
            {make_bar(score, color=color)}
          </td>
          <td style="padding:10px 6px;text-align:right;
                     border-bottom:1px solid #e5e7eb;">
            {fmt_pct(stock['day_return'])}
          </td>
          <td style="padding:10px 6px;text-align:right;
                     border-bottom:1px solid #e5e7eb;">
            {fmt_pct(stock['month_return'])}
          </td>
        </tr>
        """)

    return f"""
    <table role="presentation" cellpadding="0" cellspacing="0"
           style="width:100%;border-collapse:collapse;font-size:13px;">
      <tr style="color:#64748b;text-align:left;">
        <th style="padding:8px 6px;">公司</th>
        <th style="padding:8px 6px;">自定义情绪分</th>
        <th style="padding:8px 6px;text-align:right;">当日</th>
        <th style="padding:8px 6px;text-align:right;">约 1 个月</th>
      </tr>
      {''.join(rows)}
    </table>
    """


def flows_html(flow_data):
    if not flow_data["ok"]:
        return f"""
        <div style="background:#f8fafc;border-radius:8px;padding:14px;
                    color:#64748b;">
          <strong>资金流图暂不可用</strong>
          <p style="margin:6px 0 0;">
            {html_escape(flow_data['message'])}
          </p>
          <p style="font-size:12px;margin-bottom:0;">
            数据源：
            <a href="https://tickerinside.com/etf-flows/">
              TickerInside ETF Flows
            </a>
          </p>
        </div>
        """

    rows = flow_data["rows"]
    total = flow_data["total"]

    total_html = ""
    if total is not None:
        total_color = change_color(total)
        total_html = f"""
        <div style="margin:8px 0 14px;padding:12px;background:#f8fafc;
                    border-radius:8px;">
          <div style="font-size:12px;color:#64748b;">数据源公布的净流量</div>
          <div style="font-size:22px;font-weight:bold;color:{total_color};">
            {fmt_money(total)}
          </div>
        </div>
        """

    if not rows:
        details = """
        <p style="color:#64748b;">
          已获取接口数据，但没有识别到可绘制的 ETF 明细。
          请查看数据源页面获取完整表格。
        </p>
        """
    else:
        max_abs = max(abs(row["amount"]) for row in rows) or 1
        pieces = []

        for row in rows:
            amount = row["amount"]
            width = abs(amount) / max_abs * 100
            is_in = amount >= 0
            color = "#16a34a" if is_in else "#dc2626"
            label = "净流入" if is_in else "净流出"

            pieces.append(f"""
            <div style="margin:10px 0;">
              <div style="display:flex;justify-content:space-between;
                          gap:12px;font-size:13px;margin-bottom:4px;">
                <span><strong>{html_escape(row['ticker'])}</strong>
                  <span style="color:{color};"> · {label}</span>
                </span>
                <strong style="color:{color};">{fmt_money(amount)}</strong>
              </div>
              <div style="height:10px;background:#e5e7eb;border-radius:8px;
                          overflow:hidden;">
                <div style="height:10px;width:{width:.1f}%;
                            background:{color};border-radius:8px;"></div>
              </div>
            </div>
            """)

        details = "".join(pieces)

    return f"""
    <div style="font-size:12px;color:#64748b;margin-bottom:8px;">
      数据日期：{html_escape(flow_data['date'])}
    </div>
    {total_html}
    <div style="display:flex;gap:14px;font-size:12px;margin-bottom:10px;">
      <span style="color:#15803d;">■ 净流入</span>
      <span style="color:#b91c1c;">■ 净流出</span>
    </div>
    {details}
    <p style="font-size:11px;line-height:1.6;color:#64748b;margin-top:12px;">
      来源：
      <a href="https://tickerinside.com/etf-flows/">
        TickerInside ETF Flows
      </a>。
      数据仅覆盖该来源所读取的部分 ETF，非全市场完整资金统计。
    </p>
    """


# ==================== 邮件内容 ====================

def build_email(cnn, sp500, stocks, flow_data):
    score = cnn["score"]
    label = sentiment_label(score)
    color = score_color(score)

    mag7_average = None
    if stocks:
        mag7_average = sum(s["score"] for s in stocks) / len(stocks)

    if mag7_average is None:
        mag7_summary = "七姐妹数据不足"
    else:
        mag7_summary = (
            f"{mag7_average:.1f}/100（{sentiment_label(mag7_average)}）"
        )

    html_body = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width,initial-scale=1">
    </head>
    <body style="margin:0;background:#f3f4f6;
                 font-family:Arial,'Microsoft YaHei',sans-serif;color:#111827;">
      <div style="max-width:760px;margin:0 auto;padding:18px 12px;">
        <div style="background:#111827;color:#ffffff;padding:22px;
                    border-radius:12px 12px 0 0;">
          <div style="font-size:12px;opacity:.8;">MARKET SENTIMENT REPORT</div>
          <h1 style="font-size:23px;margin:8px 0;">美股恐贪指数日报</h1>
          <div style="font-size:13px;opacity:.8;">
            CNN 数据日期：{html_escape(cnn['date'])}
          </div>
        </div>

        <div style="background:#ffffff;padding:20px;
                    border:1px solid #e5e7eb;border-top:0;">
          <h2 style="font-size:18px;margin:0 0 14px;">一、CNN 恐贪指数</h2>

          <div style="background:#f8fafc;padding:18px;border-radius:10px;">
            <div style="font-size:13px;color:#64748b;">当前指数</div>
            <div style="font-size:42px;font-weight:bold;color:{color};
                        margin:4px 0;">
              {score:.1f}
              <span style="font-size:16px;">/100</span>
            </div>
            <div style="font-size:16px;font-weight:bold;color:{color};">
              {html_escape(label)}
            </div>
            {make_bar(score, color=color, height=14)}
            <div style="display:flex;justify-content:space-between;
                        font-size:11px;color:#64748b;margin-top:6px;">
              <span>0 极度恐惧</span><span>50 中性</span>
              <span>100 极度贪婪</span>
            </div>
          </div>

          <h3 style="font-size:15px;margin:20px 0 8px;">近期情绪趋势</h3>
          {cnn_history_chart(cnn['history'])}
          <p style="font-size:12px;color:#64748b;line-height:1.6;">
            图表为 CNN 历史恐贪指数；历史数据可能存在更新或缺失。
          </p>

          <hr style="border:0;border-top:1px solid #e5e7eb;margin:24px 0;">

          <h2 style="font-size:18px;margin:0 0 12px;">二、标普 500</h2>
          <div style="background:#f8fafc;padding:16px;border-radius:10px;">
            <div style="font-size:12px;color:#64748b;">
              S&P 500 指数 · {html_escape(sp500['date'])}
            </div>
            <div style="font-size:28px;font-weight:bold;margin:5px 0;">
              {fmt_number(sp500['price'])}
            </div>
            <table role="presentation" style="width:100%;font-size:13px;">
              <tr>
                <td style="padding:6px 0;color:#64748b;">当日涨跌</td>
                <td style="text-align:right;color:{change_color(sp500['day_return'])};">
                  {fmt_pct(sp500['day_return'])}
                </td>
              </tr>
              <tr>
                <td style="padding:6px 0;color:#64748b;">约 1 周涨跌</td>
                <td style="text-align:right;color:{change_color(sp500['week_return'])};">
                  {fmt_pct(sp500['week_return'])}
                </td>
              </tr>
              <tr>
                <td style="padding:6px 0;color:#64748b;">约 1 个月涨跌</td>
                <td style="text-align:right;color:{change_color(sp500['month_return'])};">
                  {fmt_pct(sp500['month_return'])}
                </td>
              </tr>
            </table>
          </div>
          {sp500_chart(sp500['history'])}

          <hr style="border:0;border-top:1px solid #e5e7eb;margin:24px 0;">

          <h2 style="font-size:18px;margin:0 0 8px;">三、市场资金流入 / 流出</h2>
          <p style="font-size:12px;color:#64748b;line-height:1.6;">
            绿色表示净流入，红色表示净流出。仅展示数据源能够验证的数值。
          </p>
          {flows_html(flow_data)}

          <hr style="border:0;border-top:1px solid #e5e7eb;margin:24px 0;">

          <h2 style="font-size:18px;margin:0 0 8px;">四、七姐妹自定义恐贪指数</h2>
          <div style="background:#f8fafc;padding:12px;border-radius:8px;
                      font-size:13px;line-height:1.7;margin-bottom:12px;">
            七姐妹平均情绪分：<strong>{html_escape(mag7_summary)}</strong><br>
            计算参考 RSI、5 日和 20 日动量及均线趋势。
            这是本脚本的技术指标综合分，不是 CNN 官方指数，也不是买卖信号。
          </div>
          {mag7_html_table(stocks)}

          <hr style="border:0;border-top:1px solid #e5e7eb;margin:24px 0;">

          <h2 style="font-size:18px;margin:0 0 8px;">五、简要观察</h2>
          <ul style="padding-left:20px;font-size:14px;line-height:1.8;">
            <li>CNN 当前情绪：{score:.1f}/100，{html_escape(label)}。</li>
            <li>标普 500 当日涨跌：{fmt_pct(sp500['day_return'])}。</li>
            <li>七姐妹平均自定义情绪分：{html_escape(mag7_summary)}。</li>
          </ul>
          <p style="font-size:12px;color:#64748b;line-height:1.7;">
            注意：恐贪指数和技术指标只反映部分市场特征，不能单独预测市场方向。
            ETF 净流量数据覆盖有限，不能代表全部投资者或整个市场的全部资金流。
            本报告仅用于信息参考，不构成投资建议。
          </p>
        </div>

        <div style="text-align:center;color:#94a3b8;font-size:11px;
                    padding:14px;">
          自动生成 · 无图片附件 · HTML 图表
        </div>
      </div>
    </body>
    </html>
    """

    plain = f"""
美股恐贪指数日报
CNN 日期：{cnn['date']}

CNN 恐贪指数：{score:.1f}/100（{label}）
标普500：{fmt_number(sp500['price'])}
标普500当日涨跌：{fmt_pct(sp500['day_return'])}
标普500约1周涨跌：{fmt_pct(sp500['week_return'])}
标普500约1个月涨跌：{fmt_pct(sp500['month_return'])}

七姐妹自定义恐贪指数：
{chr(10).join(
    f"{s['name']} ({s['symbol']}): {s['score']}/100，"
    f"{s['label']}，当日 {fmt_pct(s['day_return'])}"
    for s in stocks
) if stocks else '暂无数据'}

ETF 资金流数据日期：{flow_data['date']}
来源：https://tickerinside.com/etf-flows/

注意：七姐妹分数为自定义技术指标综合分，不是官方 CNN 指数。
资金流数据覆盖有限，不代表整个市场全部资金流。
本报告仅供信息参考，不构成投资建议。
"""

    return plain, html_body


# ==================== 发送邮件 ====================

def send_email(subject, plain_body, html_body=None):
    if not MAIL_USER or not MAIL_PASS or not MAIL_TO:
        raise RuntimeError(
            "缺少邮件环境变量，请检查 MAIL_USER、MAIL_PASS、MAIL_TO。"
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_USER
    msg["To"] = MAIL_TO
    msg["Date"] = formatdate(localtime=True)

    msg.set_content(plain_body)

    if html_body:
        msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(MAIL_USER, MAIL_PASS)
        server.send_message(msg)


# ==================== 主流程 ====================

def main():
    print("[INFO] 开始获取 CNN 恐贪指数……")

    try:
        cnn = fetch_cnn()
    except Exception as exc:
        print(f"[ERROR] CNN 数据获取失败：{exc}")

        if FORCE_SEND:
            send_email(
                "【测试】美股恐贪指数邮件提醒",
                "这是一次 GitHub Actions 邮件测试。\n"
                "CNN 数据暂时获取失败，因此本次没有生成完整行情报告。\n"
                f"错误信息：{exc}",
            )
            print("[INFO] 已发送测试邮件。")
            return

        raise

    score = cnn["score"]
    should_send = FORCE_SEND or score < 25 or score > 75

    print(f"[INFO] CNN 恐贪指数：{score:.1f}（{sentiment_label(score)}）")

    if not should_send:
        print("[INFO] 指数处于提醒区间之外，本次不发送邮件。")
        return

    print("[INFO] 获取标普 500 行情……")
    try:
        sp500 = fetch_sp500()
    except Exception as exc:
        print(f"[WARN] 标普 500 行情获取失败：{exc}")
        sp500 = {
            "date": "暂无",
            "price": None,
            "day_return": None,
            "week_return": None,
            "month_return": None,
            "history": [],
        }

    print("[INFO] 获取七姐妹行情并计算自定义情绪分……")
    stocks = fetch_mag7()

    print("[INFO] 获取 ETF 资金流数据……")
    flow_data = fetch_flows()

    plain, html_body = build_email(cnn, sp500, stocks, flow_data)

    if FORCE_SEND:
        subject = f"【测试报告】CNN 恐贪指数 {score:.1f} · {cnn['date']}"
    elif score < 25:
        subject = f"【恐惧提醒】CNN 恐贪指数 {score:.1f} · {cnn['date']}"
    else:
        subject = f"【贪婪提醒】CNN 恐贪指数 {score:.1f} · {cnn['date']}"

    send_email(subject, plain, html_body)
    print("[INFO] 邮件发送成功。")


if __name__ == "__main__":
    main()
