import os
import time
import ssl
import smtplib
import logging
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.header import Header
from html import escape

import requests

# ==================== 配置 ====================
LOW = 25
HIGH = 75
BEIJING = timezone(timedelta(hours=8))
FORCE = os.getenv("FORCE_SEND", "false").lower() == "true"

CNN_PAGE = "https://edition.cnn.com/markets/fear-and-greed"
CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": CNN_PAGE,
}

COMPONENTS = {
    "market_momentum_sp500": "标普500市场动量",
    "stock_price_strength": "股价强度",
    "stock_price_breadth": "股价广度",
    "put_call_options": "看跌/看涨期权",
    "market_volatility_vix": "市场波动率",
    "safe_haven_demand": "避险需求",
    "junk_bond_demand": "垃圾债需求",
}

SYMBOLS = {
    "SPY": "标普500 ETF",
    "VOO": "先锋标普500 ETF",
    "IVV": "iShares标普500 ETF",
    "RSP": "标普500等权重ETF",
    "^VIX": "VIX波动率指数",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ==================== 通用函数 ====================
def valid_score(value):
    try:
        value = float(value)
        if 0 <= value <= 100 and value == value:
            return value
    except (TypeError, ValueError):
        pass
    return None


def number(value, digits=2):
    try:
        value = float(value)
        if value != value or abs(value) == float("inf"):
            return "—"
        return f"{value:,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def pct(current, previous):
    try:
        current, previous = float(current), float(previous)
        if previous == 0:
            return None
        return (current / previous - 1) * 100
    except (TypeError, ValueError):
        return None


def pct_text(value):
    return "—" if value is None else f"{value:+.2f}%"


def pct_color(value):
    if value is None or value == 0:
        return "#667085"
    return "#16804a" if value > 0 else "#c62828"


def zone(score):
    if score < 25:
        return "极度恐惧", "#c62828"
    if score < 45:
        return "恐惧", "#e67e22"
    if score <= 55:
        return "中性", "#667085"
    if score <= 75:
        return "贪婪", "#39845a"
    return "极度贪婪", "#16733c"


def parse_time(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result
    except (TypeError, ValueError):
        return None


def get_json(url, params=None, timeout=25):
    response = SESSION.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


# ==================== CNN官方数据 ====================
def get_cnn_data():
    last_error = None

    for attempt in range(3):
        try:
            data = get_json(CNN_URL)
            if not isinstance(data, dict):
                raise ValueError("CNN响应不是有效JSON对象")

            fg = data.get("fear_and_greed")
            if not isinstance(fg, dict):
                raise ValueError("CNN响应缺少fear_and_greed")

            score = valid_score(fg.get("score"))
            if score is None:
                raise ValueError("CNN指数缺失或数值无效")

            timestamp = parse_time(fg.get("timestamp"))
            if timestamp is None:
                raise ValueError("CNN指数缺少有效更新时间")

            now = datetime.now(timezone.utc)

            if timestamp > now + timedelta(hours=2):
                raise ValueError("CNN数据时间异常：时间戳位于未来")

            if timestamp < now - timedelta(days=10):
                raise ValueError("CNN数据时间异常：数据可能已过期")

            components = {}
            for key in COMPONENTS:
                item = data.get(key)
                value = (
                    valid_score(item.get("score"))
                    if isinstance(item, dict)
                    else None
                )
                components[key] = value

            logging.info(
                "CNN指数=%.2f，更新时间=%s，子指标=%s/7",
                score,
                timestamp.isoformat(),
                sum(v is not None for v in components.values()),
            )

            return data, score, components

        except Exception as exc:
            last_error = exc
            logging.warning(
                "CNN请求第%d次失败：%s", attempt + 1, exc
            )
            if attempt < 2:
                time.sleep(2 ** attempt)

    raise RuntimeError(
        "无法获取并验证CNN指数，停止本次报告。"
    ) from last_error


# ==================== Yahoo行情 ====================
def get_yahoo_data(symbol):
    payload = get_json(
        f"{YAHOO_URL}{symbol}",
        params={
            "range": "3mo",
            "interval": "1d",
            "events": "div,splits",
        },
        timeout=20,
    )

    chart = payload.get("chart", {})
    if chart.get("error"):
        raise ValueError(f"Yahoo返回错误：{chart['error']}")

    results = chart.get("result")
    if not results:
        raise ValueError(f"{symbol}没有行情数据")

    item = results[0]
    timestamps = item.get("timestamp") or []
    quotes = item.get("indicators", {}).get("quote") or []

    if not timestamps or not quotes:
        raise ValueError(f"{symbol}行情结构不完整")

    closes = quotes[0].get("close") or []
    points = []

    for i, ts in enumerate(timestamps):
        if i >= len(closes) or closes[i] is None:
            continue

        try:
            close = float(closes[i])
            if close <= 0:
                continue

            points.append({
                "timestamp": int(ts),
                "close": close,
            })
        except (ValueError, TypeError):
            continue

    points.sort(key=lambda x: x["timestamp"])

    if len(points) < 22:
        raise ValueError(f"{symbol}有效日线不足22条")

    latest = points[-1]
    previous = points[-2]

    def trailing_return(intervals):
        return pct(
            points[-1]["close"],
            points[-(intervals + 1)]["close"],
        )

    # Yahoo日线时间戳通常对应交易日开始时间；
    # 使用交易所时区确定交易日期，避免北京时间跨日误标。
    meta = item.get("meta") or {}
    exchange_tz = meta.get(
        "exchangeTimezoneName", "America/New_York"
    )

    try:
        from zoneinfo import ZoneInfo
        latest_dt = datetime.fromtimestamp(
            latest["timestamp"], timezone.utc
        ).astimezone(ZoneInfo(exchange_tz))
    except Exception:
        latest_dt = datetime.fromtimestamp(
            latest["timestamp"], timezone.utc
        )

    return {
        "symbol": symbol,
        "price": latest["close"],
        "daily": pct(
            latest["close"], previous["close"]
        ),
        "week": trailing_return(5),
        "month": trailing_return(21),
        "date": latest_dt.strftime("%Y-%m-%d"),
        "points": points,
    }


def get_market_data():
    market, errors = {}, {}

    for symbol in SYMBOLS:
        try:
            market[symbol] = get_yahoo_data(symbol)
            logging.info(
                "%s: %s，日期=%s",
                symbol,
                market[symbol]["price"],
                market[symbol]["date"],
            )
        except Exception as exc:
            errors[symbol] = str(exc)
            logging.warning("%s行情失败：%s", symbol, exc)

    return market, errors


# ==================== 市场分析 ====================
def analyze_market(score, market):
    spy = market.get("SPY")
    rsp = market.get("RSP")
    vix = market.get("^VIX")

    notes = []

    if score < 25:
        notes.append(
            f"CNN指数为{score:.1f}，处于极度恐惧区间。"
            "情绪极端不代表市场已经见底。"
        )
    elif score > 75:
        notes.append(
            f"CNN指数为{score:.1f}，处于极度贪婪区间。"
            "情绪极端不代表市场必然马上反转。"
        )
    else:
        notes.append(
            f"CNN指数为{score:.1f}，未处于极端区间。"
            "需结合价格、波动率及市场广度判断。"
        )

    spy_day = spy.get("daily") if spy else None
    spy_week = spy.get("week") if spy else None
    vix_day = vix.get("daily") if vix else None

    # 比较收益率，而不是比较ETF单份价格
    if (
        spy and rsp
        and spy.get("week") is not None
        and rsp.get("week") is not None
    ):
        gap = rsp["week"] - spy["week"]

        if gap > 1:
            notes.append(
                f"RSP近一周收益领先SPY {gap:.2f}个百分点，"
                "等权重组合相对占优，但不等于资金净流入。"
            )
        elif gap < -1:
            notes.append(
                f"SPY近一周收益领先RSP {abs(gap):.2f}个百分点，"
                "市值加权组合相对占优，需留意权重集中度。"
            )
        else:
            notes.append(
                "SPY与RSP近一周表现接近，"
                "暂未发现明显的相对强弱分化。"
            )

    if vix_day is None:
        notes.append("VIX行情不可用，无法判断当日波动率变化。")
    elif vix_day >= 5:
        notes.append(
            f"VIX单日上涨{vix_day:.2f}%，风险定价升温。"
            "即使股价上涨，也不能据此认定市场风险下降。"
        )
    elif vix_day <= -5:
        notes.append(
            f"VIX单日下跌{vix_day:.2f}%，波动率有所回落，"
            "仍需观察后续变化。"
        )
    else:
        notes.append(
            "VIX当日变化未达到正负5%的提示阈值，"
            "需结合绝对水平及持续趋势判断。"
        )

    # 价格上涨不再自动等同于短期强势
    if spy_day is None or spy_week is None:
        title, color = "数据不足，暂不判断", "#667085"
    elif spy_day > 0 and spy_week > 0:
        if vix_day is not None and vix_day >= 5:
            title = "价格上涨，但风险定价升温"
            color = "#b26a00"
        elif score < 25:
            title = "价格偏强，情绪仍极度恐惧"
            color = "#b26a00"
        else:
            title = "价格偏强，仍待确认"
            color = "#16804a"
    elif spy_day < 0 and spy_week < 0:
        title, color = "价格偏弱", "#c62828"
    else:
        title, color = "价格趋势分化", "#b26a00"

    notes.append(
        "价格变化、市场情绪和资金流是不同概念。"
        "本报告不把ETF涨跌或成交量直接解释为净申购赎回。"
    )

    return title, color, notes


# ==================== 邮件HTML组件 ====================
def table_row(cells, header=False):
    bg = "#f2f4f7" if header else "#ffffff"
    weight = "700" if header else "400"

    return (
        f'<tr style="background:{bg};font-weight:{weight}">'
        + "".join(
            '<td style="padding:9px 7px;border-bottom:1px solid '
            '#eaecf0;font-size:12px;vertical-align:top">'
            f'{cell}</td>'
            for cell in cells
        )
        + "</tr>"
    )


def section(title, subtitle=""):
    return (
        '<div style="padding:22px 0 10px">'
        f'<div style="font-size:17px;font-weight:700">'
        f'{escape(title)}</div>'
        f'<div style="font-size:12px;color:#667085">'
        f'{escape(subtitle)}</div></div>'
    )


def metric_card(title, value, subtitle, color):
    return f"""
    <td style="width:50%;padding:6px;vertical-align:top">
      <div style="border:1px solid #e5e7eb;border-radius:10px;padding:14px">
        <div style="font-size:12px;color:#667085">{escape(title)}</div>
        <div style="font-size:22px;font-weight:bold;color:{color};margin-top:6px">
          {escape(str(value))}
        </div>
        <div style="font-size:12px;color:#667085;margin-top:5px">
          {escape(str(subtitle))}
        </div>
      </div>
    </td>
    """


def progress_bar(score):
    _, color = zone(score)
    return (
        '<div style="height:8px;background:#edf0f4;'
        'border-radius:8px;overflow:hidden;min-width:65px">'
        f'<div style="height:8px;width:{score:.1f}%;'
        f'background:{color}"></div></div>'
    )


# ==================== 生成日报 ====================
def build_email(data, score, components, market, errors):
    now = datetime.now(BEIJING)
    fg = data["fear_and_greed"]
    label, score_color = zone(score)
    title, title_color, notes = analyze_market(score, market)

    history = []
    for name, key in [
        ("昨日收盘", "previous_close"),
        ("一周前", "previous_1_week"),
        ("一个月前", "previous_1_month"),
        ("一年前", "previous_1_year"),
    ]:
        value = valid_score(fg.get(key))
        if value is None:
            history.append(
                table_row([name, "—", "暂无数据"])
            )
        else:
            rating, color = zone(value)
            history.append(table_row([
                name,
                f'<span style="color:{color};font-weight:bold">'
                f'{value:.1f}</span>',
                f'<span style="color:{color}">{rating}</span>',
            ]))

    component_rows = []
    count = 0

    for key, name in COMPONENTS.items():
        value = components.get(key)

        if value is None:
            component_rows.append(
                table_row([escape(name), "—", "—", "本次无有效数据"])
            )
            continue

        count += 1
        rating, color = zone(value)

        component_rows.append(table_row([
            escape(name),
            progress_bar(value),
            f'<span style="color:{color};font-weight:bold">'
            f'{value:.1f}</span>',
            f'<span style="color:{color}">{rating}</span>',
        ]))

    market_rows = []
    market_dates = []

    for symbol, name in SYMBOLS.items():
        item = market.get(symbol)

        if not item:
            market_rows.append(
                table_row([
                    escape(name), "—", "—", "—", "—", "行情不可用"
                ])
            )
            continue

        market_dates.append(f"{symbol}: {item['date']}")

        market_rows.append(table_row([
            escape(name),
            number(item["price"]),
            f'<span style="color:{pct_color(item["daily"])}">'
            f'{pct_text(item["daily"])}</span>',
            f'<span style="color:{pct_color(item["week"])}">'
            f'{pct_text(item["week"])}</span>',
            f'<span style="color:{pct_color(item["month"])}">'
            f'{pct_text(item["month"])}</span>',
            escape(item["date"]),
        ]))

    notes_html = "".join(
        f"<li style='margin-bottom:8px;line-height:1.7'>"
        f"{escape(note)}</li>"
        for note in notes
    )

    timestamp = escape(
        str(fg.get("timestamp") or "CNN未提供更新时间")
    )
    dates_text = "；".join(market_dates) or "行情不可用"
    error_text = (
        "；".join(f"{k}: {v}" for k, v in errors.items())
        if errors else "本次请求的行情均已返回"
    )

    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;background:#f2f4f7;padding:16px;
font-family:Arial,'Microsoft YaHei',sans-serif;color:#202832">
<div style="max-width:760px;margin:auto;background:#fff;
border:1px solid #e4e7ec;border-radius:12px;overflow:hidden">

<div style="background:#202832;color:#fff;padding:24px">
<div style="font-size:11px;letter-spacing:2px">DAILY MARKET BRIEF</div>
<div style="font-size:23px;font-weight:bold;margin-top:10px">美股市场风险监测</div>
<div style="font-size:12px;margin-top:8px">
邮件生成时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）</div>
</div>

<div style="padding:20px">
<table style="width:100%;border-collapse:collapse"><tr>
{metric_card("CNN恐惧与贪婪指数", f"{score:.1f}/100", label, score_color)}
{metric_card("综合监测结论", title, "规则型观察，不是买卖指令", title_color)}
</tr></table>

<div style="margin-top:16px;padding:15px;background:#f8fafc;border:1px solid #eaecf0;border-radius:9px">
<div style="font-size:12px;color:#667085">指数位置</div>
<div style="height:10px;background:#edf0f4;border-radius:8px;margin-top:12px;overflow:hidden">
<div style="height:10px;width:{score:.1f}%;background:{score_color}"></div>
</div>
<div style="display:flex;justify-content:space-between;font-size:11px;color:#667085;margin-top:6px">
<span>0 极度恐惧</span><span>50 中性</span><span>100 极度贪婪</span>
</div></div>

{section("一、历史情绪对比", "CNN接口返回的历史参考值")}
<table style="width:100%;border-collapse:collapse">
{table_row(["时间", "指数", "情绪"], True)}
{''.join(history)}
</table>

{section("二、标普500与波动率", "Yahoo日线数据；各标的交易日期分别列示")}
<div style="overflow-wrap:anywhere"><table style="width:100%;border-collapse:collapse">
{table_row(["标的", "最新收盘", "日涨跌", "约一周", "约一月", "日期"], True)}
{''.join(market_rows)}
</table></div>
<div style="font-size:11px;color:#667085;margin-top:7px;line-height:1.6">
各标的日期：{escape(dates_text)}。约一周按5个交易日间隔计算，约一月按21个交易日间隔计算。
ETF单份价格不能直接比较强弱，应比较相同时间区间的收益率。VIX不是可直接买入的股票资产。
</div>

{section("三、CNN七项子指标", f"本次取得{count}/7项有效分数；缺失值不作推测")}
<table style="width:100%;border-collapse:collapse">
{table_row(["指标", "区间", "分数", "状态"], True)}
{''.join(component_rows)}
</table>

{section("四、市场分析", "分别观察价格、情绪与波动率")}
<div style="padding:16px;background:#f8fafc;border:1px solid #eaecf0;border-radius:9px">
<div style="font-size:18px;font-weight:bold;color:{title_color}">{escape(title)}</div>
<ul style="padding-left:20px">{notes_html}</ul>
</div>

{section("五、资金流向", "不把价格变化冒充资金流")}
<div style="font-size:13px;line-height:1.8">
<p>本报告比较SPY、VOO、IVV和RSP的价格表现。</p>
<p><b>注意：</b>ETF价格涨跌和成交量不等于净申购赎回。本版本没有接入经过核验的每日ETF份额变化或基金净流量数据，因此不报告净流入/流出金额。</p>
</div>

{section("六、数据来源与完整性")}
<div style="font-size:11px;line-height:1.8;color:#667085">
<div>CNN官方页面：<a href="{CNN_PAGE}">{CNN_PAGE}</a></div>
<div>CNN数据接口：{CNN_URL}</div>
<div>CNN数据时间：{timestamp}</div>
<div>行情来源：Yahoo Finance日线接口。</div>
<div>行情状态：{escape(error_text)}</div>
<div>提醒阈值：严格低于{LOW}或严格高于{HIGH}时发送。</div>
<div>注意：CNN底层接口不是正式承诺长期稳定的公共API。</div>
<div>本报告仅供研究参考，不构成投资建议。</div>
</div>
</div></div></body></html>"""

    plain = (
        "美股市场风险监测\n"
        f"生成时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）\n\n"
        f"CNN恐惧与贪婪指数：{score:.1f}/100（{label}）\n"
        f"综合监测结论：{title}\n\n"
        "市场观察：\n"
        + "\n".join(f"- {note}" for note in notes)
        + f"\n\nCNN数据时间：{timestamp}\n"
        f"行情日期：{dates_text}\n"
        f"行情状态：{error_text}\n\n"
        "资金流说明：ETF价格涨跌和成交量不等于净申购赎回。\n"
        "本报告仅供研究参考，不构成投资建议。\n"
    )

    return plain, html, title


# ==================== 发送邮件 ====================
def send_email(subject, plain, html):
    username = os.environ.get("MAIL_USER")
    password = os.environ.get("MAIL_PASS")
    recipient = os.environ.get("MAIL_TO")

    if not username or not password or not recipient:
        raise RuntimeError(
            "邮件配置不完整，请检查MAIL_USER、MAIL_PASS、MAIL_TO"
        )

    message = EmailMessage()
    message["Subject"] = str(Header(subject, "utf-8"))
    message["From"] = username
    message["To"] = recipient
    message.set_content(plain)
    message.add_alternative(html, subtype="html")

    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))

    with smtplib.SMTP_SSL(
        host,
        port,
        timeout=30,
        context=ssl.create_default_context(),
    ) as server:
        server.login(username, password)
        server.send_message(message)

    logging.info("邮件发送成功：%s", recipient)


# ==================== 主程序 ====================
def main():
    logging.info("北京时间：%s", datetime.now(BEIJING).isoformat())

    if FORCE:
        send_email(
            "[测试] 美股市场监测邮件",
            "邮件系统测试成功。本邮件没有获取实时行情。",
            "<html><body><h2>美股市场监测测试</h2>"
            "<p>邮件发送配置可用。本邮件没有获取实时行情。</p>"
            "</body></html>",
        )
        return

    data, score, components = get_cnn_data()
    market, errors = get_market_data()

    # 只在CNN指数进入极端区间时发送
    if not (score < LOW or score > HIGH):
        logging.info(
            "CNN指数%.2f，未触发提醒阈值。", score
        )
        return

    plain, html, title = build_email(
        data, score, components, market, errors
    )

    if score < LOW:
        subject = f"[极度恐惧提醒] CNN指数{score:.1f}/100｜{title}"
    else:
        subject = f"[极度贪婪提醒] CNN指数{score:.1f}/100｜{title}"

    send_email(subject, plain, html)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("程序运行失败")
        raise
