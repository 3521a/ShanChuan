import os
import time
import ssl
import smtplib
import logging
import base64
from io import BytesIO
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.header import Header
from html import escape

import requests

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ==================== 配置 ====================
LOW = 25
HIGH = 75
BEIJING = timezone(timedelta(hours=8))

CNN_PAGE = "https://edition.cnn.com/markets/fear-and-greed"
CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
FORCE_SEND = os.getenv("FORCE_SEND", "false").lower() == "true"

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

# 动态 keys 容错：不同版本可能使用不同的动量指标键名
COMPONENT_KEYS = [
    ("market_momentum_sp500", "market_momentum_sp125", "标普500市场动量"),
    ("stock_price_breadth", "股价广度"),
    ("market_volatility_vix", "市场波动率"),
    ("junk_bond_demand", "垃圾债需求"),
]

# 仅保留 SPY、RSP、VIX
SYMBOLS = {
    "SPY": "标普500市值加权ETF",
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
        number = float(value)
        if 0 <= number <= 100 and number == number:
            if abs(number) != float("inf"):
                return number
    except (TypeError, ValueError):
        pass
    return None


def fmt_number(value, digits=2):
    try:
        number = float(value)
        if number != number or abs(number) == float("inf"):
            return "—"
        return f"{number:,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def pct(current, previous):
    try:
        current = float(current)
        previous = float(previous)
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
    if score is None:
        return "数据缺失", "#667085"
    if score < 25:
        return "极度恐惧", "#f04438"
    if score < 45:
        return "恐惧", "#f79009"
    if score <= 55:
        return "中性", "#98a2b3"
    if score <= 75:
        return "贪婪", "#32d583"
    return "极度贪婪", "#12b76a"


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


# ==================== CNN 指数与历史数据 ====================
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
                raise ValueError("CNN当前指数缺失或无效")

            timestamp = parse_time(fg.get("timestamp"))
            if timestamp is None:
                raise ValueError("CNN指数缺少有效更新时间")

            now = datetime.now(timezone.utc)

            if timestamp > now + timedelta(hours=2):
                raise ValueError("CNN时间戳位于未来")

            if timestamp < now - timedelta(days=10):
                raise ValueError("CNN当前指数可能已过期")

            components = get_components(data)
            history = get_history(data)

            logging.info(
                "CNN score=%.2f, history_points=%d, components=%d/4",
                score,
                len(history),
                sum(c["score"] is not None for c in components),
            )

            return data, score, components, history, timestamp

        except Exception as exc:
            last_error = exc
            logging.warning(
                "CNN请求第%d次失败：%s",
                attempt + 1,
                exc,
            )

            if attempt < 2:
                time.sleep(2 ** attempt)

    raise RuntimeError(
        "无法获取并验证CNN当前指数"
    ) from last_error


def get_components(data):
    """动态解析4项指标，缺失数据以—显示。"""
    components = []

    keys_obj = data.get("keys", [])
    if not isinstance(keys_obj, list):
        keys_obj = []

    for spec in COMPONENT_KEYS:
        *candidate_keys, label = spec
        selected_key = None

        # 优先使用 keys 数组中声明且实际存在的候选键
        for candidate in candidate_keys:
            if (
                candidate in keys_obj
                and isinstance(data.get(candidate), dict)
            ):
                selected_key = candidate
                break

        # keys 数组缺失或不完整时，逐一检查候选键
        if selected_key is None:
            for candidate in candidate_keys:
                if isinstance(data.get(candidate), dict):
                    selected_key = candidate
                    break

        raw = data.get(selected_key) if selected_key else None

        value = (
            valid_score(raw.get("score"))
            if isinstance(raw, dict)
            else None
        )

        rating = (
            raw.get("rating")
            if isinstance(raw, dict)
            else None
        )

        components.append({
            "key": selected_key or candidate_keys[0],
            "label": label,
            "score": value,
            "rating": str(rating) if rating else None,
        })

    return components


def get_history(data):
    """
    解析 fear_and_greed_historical.data 中的 x/y。
    兼容对象列表，以及 x/y 分别为数组的结构。
    """
    raw = data.get("fear_and_greed_historical")

    if not isinstance(raw, dict):
        raw = data.get("fear_and_greed_historical_data")

    rows = raw.get("data", []) if isinstance(raw, dict) else []
    result = []

    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue

            x = row.get("x")
            y = valid_score(row.get("y"))

            if x is None or y is None:
                continue

            try:
                stamp = float(x)

                # 兼容毫秒级与秒级 Unix 时间戳
                if stamp > 10_000_000_000:
                    stamp /= 1000.0

                dt = datetime.fromtimestamp(
                    stamp,
                    tz=timezone.utc,
                )

                result.append({
                    "date": dt,
                    "score": y,
                })

            except (TypeError, ValueError, OSError, OverflowError):
                continue

    # 兼容 {"x": [...], "y": [...]} 结构
    if not result and isinstance(rows, dict):
        xs = rows.get("x", [])
        ys = rows.get("y", [])

        if isinstance(xs, list) and isinstance(ys, list):
            for x, y_raw in zip(xs, ys):
                y = valid_score(y_raw)

                if y is None:
                    continue

                try:
                    stamp = float(x)

                    if stamp > 10_000_000_000:
                        stamp /= 1000.0

                    dt = datetime.fromtimestamp(
                        stamp,
                        tz=timezone.utc,
                    )

                    result.append({
                        "date": dt,
                        "score": y,
                    })

                except (TypeError, ValueError, OSError, OverflowError):
                    continue

    # 只保留近一年数据，并按时间排序、去重
    cutoff = datetime.now(timezone.utc) - timedelta(days=366)
    unique = {}

    for point in result:
        if point["date"] >= cutoff:
            unique[point["date"]] = point["score"]

    return [
        {"date": dt, "score": value}
        for dt, value in sorted(unique.items())
    ]


# ==================== 近一年走势图 ====================
def make_chart_base64(history):
    """
    返回 data:image/png;base64,... 格式的图表。
    历史数据不足或绘图失败时返回 None。
    """
    if not history or len(history) < 2:
        logging.warning("CNN历史数据不足，走势图不可用")
        return None

    fig = None

    try:
        dates = [point["date"] for point in history]
        scores = [point["score"] for point in history]

        fig, ax = plt.subplots(
            figsize=(10, 4.2),
            dpi=150,
        )

        fig.patch.set_facecolor("#0b1220")
        ax.set_facecolor("#0b1220")

        # 分数曲线及淡色填充
        ax.fill_between(
            dates,
            scores,
            50,
            color="#38bdf8",
            alpha=0.10,
        )

        ax.plot(
            dates,
            scores,
            color="#38bdf8",
            linewidth=2.0,
            solid_capstyle="round",
        )

        # 极度恐惧、极度贪婪阈值线
        ax.axhline(
            25,
            color="#fb7185",
            linestyle="--",
            linewidth=1.2,
            alpha=0.95,
        )

        ax.axhline(
            75,
            color="#fbbf24",
            linestyle="--",
            linewidth=1.2,
            alpha=0.95,
        )

        ax.axhline(
            50,
            color="#64748b",
            linestyle=":",
            linewidth=0.9,
            alpha=0.75,
        )

        ax.set_ylim(0, 100)
        ax.set_xlim(dates[0], dates[-1])
        ax.set_yticks([0, 25, 50, 75, 100])

        ax.tick_params(
            axis="x",
            colors="#cbd5e1",
            labelsize=8,
        )

        ax.tick_params(
            axis="y",
            colors="#cbd5e1",
            labelsize=8,
        )

        ax.xaxis.set_major_locator(
            mdates.MonthLocator(interval=2)
        )

        ax.xaxis.set_major_formatter(
            mdates.DateFormatter("%b %Y")
        )

        ax.grid(
            axis="y",
            color="#334155",
            linewidth=0.6,
            alpha=0.55,
        )

        for spine in ax.spines.values():
            spine.set_color("#334155")

        ax.set_title(
            "CNN Fear & Greed Index · 1 Year",
            color="#f8fafc",
            fontsize=13,
            fontweight="bold",
            loc="left",
            pad=12,
        )

        ax.text(
            0.99,
            0.94,
            "75 · EXTREME GREED",
            transform=ax.transAxes,
            ha="right",
            va="top",
            color="#fbbf24",
            fontsize=7,
        )

        ax.text(
            0.99,
            0.23,
            "25 · EXTREME FEAR",
            transform=ax.transAxes,
            ha="right",
            va="top",
            color="#fb7185",
            fontsize=7,
        )

        fig.autofmt_xdate(rotation=0, ha="center")
        fig.tight_layout(pad=1.5)

        buffer = BytesIO()

        fig.savefig(
            buffer,
            format="png",
            facecolor=fig.get_facecolor(),
            bbox_inches="tight",
            dpi=150,
        )

        buffer.seek(0)

        encoded = base64.b64encode(
            buffer.read()
        ).decode("ascii")

        buffer.close()

        return f"data:image/png;base64,{encoded}"

    except Exception:
        logging.exception("生成CNN历史走势图失败")
        return None

    finally:
        if fig is not None:
            plt.close(fig)


# ==================== Yahoo 行情 ====================
def get_yahoo_data(symbol):
    payload = get_json(
        f"{YAHOO_URL}{symbol}",
        params={
            "range": "3mo",
            "interval": "1d",
        },
        timeout=20,
    )

    chart = payload.get("chart", {})

    if chart.get("error"):
        raise ValueError(
            f"Yahoo返回错误：{chart['error']}"
        )

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

            if close > 0 and close == close:
                points.append({
                    "timestamp": int(ts),
                    "close": close,
                })

        except (ValueError, TypeError):
            continue

    points.sort(key=lambda point: point["timestamp"])

    if len(points) < 22:
        raise ValueError(f"{symbol}有效日线不足22条")

    latest = points[-1]
    previous = points[-2]

    def trailing_return(intervals):
        if len(points) <= intervals:
            return None

        return pct(
            points[-1]["close"],
            points[-(intervals + 1)]["close"],
        )

    meta = item.get("meta") or {}

    exchange_tz = meta.get(
        "exchangeTimezoneName",
        "America/New_York",
    )

    try:
        from zoneinfo import ZoneInfo

        latest_dt = datetime.fromtimestamp(
            latest["timestamp"],
            timezone.utc,
        ).astimezone(ZoneInfo(exchange_tz))

    except Exception:
        latest_dt = datetime.fromtimestamp(
            latest["timestamp"],
            timezone.utc,
        )

    return {
        "symbol": symbol,
        "price": latest["close"],
        "daily": pct(
            latest["close"],
            previous["close"],
        ),
        "week": trailing_return(5),
        "month": trailing_return(21),
        "date": latest_dt.strftime("%Y-%m-%d"),
    }


def get_market_data():
    market = {}
    errors = {}

    for symbol in SYMBOLS:
        try:
            market[symbol] = get_yahoo_data(symbol)

            logging.info(
                "%s: price=%s date=%s",
                symbol,
                market[symbol]["price"],
                market[symbol]["date"],
            )

        except Exception as exc:
            errors[symbol] = str(exc)
            logging.warning(
                "%s行情失败：%s",
                symbol,
                exc,
            )

    return market, errors


# ==================== 市场分析 ====================
def analyze_market(score, market):
    notes = []
    label, _ = zone(score)

    if score < LOW:
        notes.append(
            f"CNN指数为{score:.1f}，处于极度恐惧区间；"
            "极端恐惧不代表市场已经见底。"
        )

    elif score > HIGH:
        notes.append(
            f"CNN指数为{score:.1f}，处于极度贪婪区间；"
            "极端贪婪不代表市场必然马上反转。"
        )

    else:
        notes.append(
            f"CNN指数为{score:.1f}，当前情绪状态为{label}。"
        )

    spy = market.get("SPY")
    rsp = market.get("RSP")
    vix = market.get("^VIX")

    # 比较 SPY 和 RSP 的收益率，而不是名义价格
    if (
        spy
        and rsp
        and spy.get("week") is not None
        and rsp.get("week") is not None
    ):
        gap = rsp["week"] - spy["week"]

        if gap > 1:
            notes.append(
                f"RSP近一周收益领先SPY {gap:.2f}个百分点，"
                "等权重股票相对占优，可作为市场广度的一个线索。"
            )

        elif gap < -1:
            notes.append(
                f"SPY近一周收益领先RSP {abs(gap):.2f}个百分点，"
                "市值加权指数相对占优；需留意权重集中度。"
            )

        else:
            notes.append(
                "SPY与RSP近一周表现接近，"
                "暂未发现明显的相对收益分化。"
            )

    else:
        notes.append(
            "SPY或RSP数据缺失，无法比较市值加权与等权重表现。"
        )

    if vix and vix.get("daily") is not None:
        vix_change = vix["daily"]

        if vix_change >= 5:
            notes.append(
                f"VIX单日上涨{vix_change:.2f}%，风险定价升温；"
                "股价上涨也不一定代表风险下降。"
            )

        elif vix_change <= -5:
            notes.append(
                f"VIX单日下跌{vix_change:.2f}%，波动率有所回落，"
                "仍需观察后续趋势。"
            )

        else:
            notes.append(
                f"VIX单日变化为{vix_change:+.2f}%，"
                "未达到正负5%的提示阈值。"
            )

    else:
        notes.append(
            "VIX行情缺失，无法分析当日波动率变化。"
        )

    if (
        not spy
        or spy.get("daily") is None
        or spy.get("week") is None
    ):
        title, color = "行情数据不足，暂不判断", "#98a2b3"

    elif spy["daily"] > 0 and spy["week"] > 0:
        if (
            vix
            and vix.get("daily") is not None
            and vix["daily"] >= 5
        ):
            title, color = (
                "价格上涨，但风险定价升温",
                "#fbbf24",
            )

        elif score < LOW:
            title, color = (
                "价格偏强，情绪仍极度恐惧",
                "#fbbf24",
            )

        else:
            title, color = "价格偏强，仍待确认", "#32d583"

    elif spy["daily"] < 0 and spy["week"] < 0:
        title, color = "价格偏弱", "#fb7185"

    else:
        title, color = "价格趋势分化", "#fbbf24"

    notes.append(
        "ETF涨跌与成交量不等于资金净流入或净流出；"
        "本报告不根据价格表现虚构资金流数据。"
    )

    return title, color, notes


# ==================== HTML UI 组件 ====================
def table_row(cells, header=False):
    background = "#f2f4f7" if header else "#ffffff"
    weight = "700" if header else "400"

    return (
        f'<tr style="background:{background};font-weight:{weight}">'
        + "".join(
            '<td style="padding:9px 7px;'
            'border-bottom:1px solid #eaecf0;'
            'font-size:12px;vertical-align:top">'
            f'{cell}</td>'
            for cell in cells
        )
        + "</tr>"
    )


def section(title, subtitle=""):
    return (
        '<div style="padding:22px 0 10px">'
        f'<div style="font-size:17px;font-weight:700;'
        f'color:#101828">{escape(title)}</div>'
        f'<div style="font-size:12px;color:#667085;'
        f'margin-top:4px">{escape(subtitle)}</div>'
        "</div>"
    )


def metric_card(title, value, subtitle, color):
    return f"""
    <td style="width:50%;padding:5px;vertical-align:top">
      <div style="border:1px solid #26364f;border-radius:10px;padding:12px">
        <div style="font-size:12px;color:#aab8cf">
          {escape(title)}
        </div>
        <div style="font-size:22px;font-weight:bold;color:{color};
        margin-top:6px;overflow-wrap:anywhere">
          {escape(str(value))}
        </div>
        <div style="font-size:12px;color:#aab8cf;margin-top:5px">
          {escape(str(subtitle))}
        </div>
      </div>
    </td>
    """


def progress_bar(score):
    _, color = zone(score)

    return (
        '<div style="height:9px;background:#26364f;'
        'border-radius:8px;overflow:hidden">'
        f'<div style="height:9px;width:{score:.1f}%;'
        f'background:{color}"></div>'
        "</div>"
    )


# ==================== 生成邮件 ====================
def build_email(
    data,
    score,
    components,
    history,
    chart_uri,
    market,
    errors,
    timestamp,
):
    now = datetime.now(BEIJING)
    label, score_color = zone(score)

    title, title_color, notes = analyze_market(
        score,
        market,
    )

    fg = data.get("fear_and_greed", {})

    # 历史情绪对比
    historical_rows = []

    for label_text, key in [
        ("昨日收盘", "previous_close"),
        ("一周前", "previous_1_week"),
        ("一个月前", "previous_1_month"),
        ("一年前", "previous_1_year"),
    ]:
        value = valid_score(fg.get(key))
        rating, color = zone(value)

        historical_rows.append(
            table_row([
                escape(label_text),
                (
                    fmt_number(value, 1)
                    if value is not None
                    else "—"
                ),
                f'<span style="color:{color}">'
                f'{escape(rating)}</span>',
            ])
        )

    # 四项 CNN 子指标
    component_rows = []

    for component in components:
        value = component["score"]
        rating, color = zone(value)

        if value is not None:
            value_html = (
                f'<span style="color:{color};font-weight:bold">'
                f'{value:.1f}</span>'
            )
            bar_html = progress_bar(value)
        else:
            value_html = "—"
            bar_html = "—"

        component_rows.append(
            table_row([
                escape(component["label"]),
                bar_html,
                value_html,
                f'<span style="color:{color}">'
                f'{escape(rating)}</span>',
            ])
        )

    # SPY、RSP、VIX 行情
    market_rows = []
    market_dates = []

    for symbol, label_text in SYMBOLS.items():
        item = market.get(symbol)

        if not item:
            market_rows.append(
                table_row([
                    escape(label_text),
                    "—",
                    "—",
                    "—",
                    "—",
                    "数据不可用",
                ])
            )
            continue

        market_dates.append(
            f"{symbol}: {item['date']}"
        )

        market_rows.append(
            table_row([
                escape(label_text),
                fmt_number(item["price"]),
                (
                    f'<span style="color:{pct_color(item["daily"])}">'
                    f'{pct_text(item["daily"])}</span>'
                ),
                (
                    f'<span style="color:{pct_color(item["week"])}">'
                    f'{pct_text(item["week"])}</span>'
                ),
                (
                    f'<span style="color:{pct_color(item["month"])}">'
                    f'{pct_text(item["month"])}</span>'
                ),
                escape(item["date"]),
            ])
        )

    notes_html = "".join(
        f"<li style='margin:0 0 8px;line-height:1.65'>"
        f"{escape(note)}</li>"
        for note in notes
    )

    # 内联 base64 图表，不添加邮件附件
    if chart_uri:
        chart_html = (
            '<img alt="CNN Fear and Greed Index one-year chart" '
            f'src="{chart_uri}" '
            'style="display:block;width:100%;max-width:100%;'
            'height:auto;border:0;border-radius:8px;margin-top:18px" />'
        )
    else:
        chart_html = (
            '<div style="margin-top:18px;padding:18px;'
            'border:1px dashed #475569;border-radius:8px;'
            'color:#cbd5e1;font-size:13px">'
            '近一年走势图暂不可用：CNN历史数据缺失或图表生成失败。'
            '</div>'
        )

    data_dates = (
        "；".join(market_dates)
        if market_dates
        else "行情不可用"
    )

    error_text = (
        "；".join(
            f"{key}: {value}"
            for key, value in errors.items()
        )
        if errors
        else "本次请求的行情均已返回"
    )

    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:12px;background:#f2f4f7;color:#101828;font-family:Arial,'Microsoft YaHei',sans-serif">
<div style="max-width:760px;margin:0 auto">

  <div style="background:#0b1220;color:#f8fafc;border-radius:14px;padding:20px 16px;margin-bottom:14px">
    <div style="font-size:11px;letter-spacing:2px;color:#94a3b8">DAILY MARKET BRIEF</div>
    <div style="font-size:23px;line-height:1.3;font-weight:700;margin-top:8px">CNN 恐惧与贪婪指数</div>
    <div style="font-size:12px;color:#94a3b8;margin-top:7px">生成时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）</div>

    <table role="presentation" style="width:100%;border-collapse:collapse;margin-top:14px"><tr>
      {metric_card("当前指数", f"{score:.1f} / 100", label, score_color)}
      {metric_card("市场监测", title, "规则型观察，不是买卖指令", title_color)}
    </tr></table>

    <div style="font-size:12px;color:#cbd5e1;margin:12px 1px 8px">指数区间</div>
    {progress_bar(score)}

    <div style="font-size:10px;color:#94a3b8;margin-top:6px">
      0 极度恐惧　　50 中性　　100 极度贪婪
    </div>

    {chart_html}

    <div style="font-size:11px;color:#94a3b8;margin-top:8px;line-height:1.5">
      近一年历史走势图；虚线为25（极度恐惧）与75（极度贪婪）。
      CNN数据时间：{escape(timestamp.isoformat())}
    </div>
  </div>

  <div style="background:#ffffff;border:1px solid #eaecf0;border-radius:12px;padding:16px;margin-bottom:14px">
    {section("一、历史情绪对比", "CNN接口提供的历史参考值")}
    <table role="presentation" style="width:100%;border-collapse:collapse">
      {table_row(["时间", "指数", "状态"], True)}
      {''.join(historical_rows)}
    </table>
  </div>

  <div style="background:#ffffff;border:1px solid #eaecf0;border-radius:12px;padding:16px;margin-bottom:14px">
    {section("二、核心子指标", "4项核心指标；缺失项显示为—")}
    <table role="presentation" style="width:100%;border-collapse:collapse">
      {table_row(["指标", "区间", "分数", "状态"], True)}
      {''.join(component_rows)}
    </table>
  </div>

  <div style="background:#ffffff;border:1px solid #eaecf0;border-radius:12px;padding:16px;margin-bottom:14px">
    {section("三、市场行情", "SPY市值加权、RSP等权重、VIX波动率")}
    <div style="overflow-wrap:anywhere">
    <table role="presentation" style="width:100%;border-collapse:collapse">
      {table_row(["标的", "最新收盘", "日涨跌", "约一周", "约一月", "日期"], True)}
      {''.join(market_rows)}
    </table>
    </div>
    <div style="font-size:11px;color:#667085;margin-top:8px;line-height:1.6">
      各标的交易日期：{escape(data_dates)}。
      约一周按5个交易日间隔计算，约一月按21个交易日间隔计算。
      SPY与RSP的收益差可提供市场广度线索，但不能替代完整的市场宽度统计。
    </div>
  </div>

  <div style="background:#ffffff;border:1px solid #eaecf0;border-radius:12px;padding:16px;margin-bottom:14px">
    {section("四、市场分析", "将价格、情绪与波动率分开观察")}
    <div style="font-size:17px;font-weight:700;color:{title_color};margin-bottom:10px">{escape(title)}</div>
    <ul style="padding-left:20px;margin:0">{notes_html}</ul>
  </div>

  <div style="background:#ffffff;border:1px solid #eaecf0;border-radius:12px;padding:16px">
    {section("五、数据与风险提示", "数据缺失时尽量降级显示")}
    <div style="font-size:12px;line-height:1.8;color:#475467">
      <div>CNN页面：<a href="{CNN_PAGE}">{CNN_PAGE}</a></div>
      <div>CNN接口：{CNN_URL}</div>
      <div>行情来源：Yahoo Finance日线数据</div>
      <div>行情状态：{escape(error_text)}</div>
      <div>提醒阈值：指数严格低于{LOW}或严格高于{HIGH}时发送</div>
      <div>说明：CNN底层接口不是正式承诺长期稳定的公共API；部分邮件客户端可能屏蔽HTML内联base64图片。</div>
      <div>本报告仅供研究参考，不构成投资建议。</div>
    </div>
  </div>

</div>
</body>
</html>"""

    plain = (
        "CNN恐惧与贪婪指数市场监测\n"
        f"生成时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）\n"
        f"当前指数：{score:.1f}/100（{label}）\n"
        f"市场监测：{title}\n"
        f"CNN时间：{timestamp.isoformat()}\n"
        f"近一年有效历史点：{len(history)}\n\n"
        "核心子指标：\n"
        + "\n".join(
            f"- {c['label']}: "
            f"{fmt_number(c['score'], 1) if c['score'] is not None else '—'}"
            for c in components
        )
        + "\n\n市场观察：\n"
        + "\n".join(f"- {note}" for note in notes)
        + f"\n\n行情日期：{data_dates}\n"
        f"行情状态：{error_text}\n"
        "ETF涨跌与成交量不等于资金净流入或净流出。\n"
        "本报告仅供研究参考，不构成投资建议。\n"
    )

    return plain, html, title


# ==================== Gmail SMTP ====================
def send_email(subject, plain, html):
    username = os.getenv("MAIL_USER")
    password = os.getenv("MAIL_PASS")
    recipient = os.getenv("MAIL_TO")

    if not username or not password or not recipient:
        raise RuntimeError(
            "邮件配置不完整，请设置 MAIL_USER、MAIL_PASS、MAIL_TO"
        )

    message = EmailMessage()
    message["Subject"] = str(Header(subject, "utf-8"))
    message["From"] = username
    message["To"] = recipient

    message.set_content(plain)
    message.add_alternative(html, subtype="html")

    # 不调用 add_attachment；图表只以内联 data URI 放在HTML中
    context = ssl.create_default_context()

    with smtplib.SMTP_SSL(
        SMTP_HOST,
        SMTP_PORT,
        timeout=30,
        context=context,
    ) as server:
        server.login(username, password)
        server.send_message(message)

    logging.info("邮件发送成功：%s", recipient)


# ==================== 主程序 ====================
def main():
    logging.info(
        "开始运行；北京时间=%s",
        datetime.now(BEIJING).isoformat(),
    )

    if FORCE_SEND:
        send_email(
            "[测试] CNN市场监测邮件",
            "邮件发送配置测试成功。本邮件不包含实时市场数据。",
            (
                "<html><body style='font-family:Arial;padding:16px'>"
                "<h2>CNN市场监测邮件测试</h2>"
                "<p>Gmail SMTP发送配置可用。本邮件不包含实时市场数据。</p>"
                "</body></html>"
            ),
        )
        return

    (
        data,
        score,
        components,
        history,
        timestamp,
    ) = get_cnn_data()

    market, errors = get_market_data()
    chart_uri = make_chart_base64(history)

    # 仅当指数进入极端区间时发送邮件
    if not (score < LOW or score > HIGH):
        logging.info(
            "CNN指数%.2f，未触发极端情绪邮件阈值。",
            score,
        )
        return

    plain, html, title = build_email(
        data=data,
        score=score,
        components=components,
        history=history,
        chart_uri=chart_uri,
        market=market,
        errors=errors,
        timestamp=timestamp,
    )

    if score < LOW:
        subject = (
            f"[极度恐惧提醒] CNN指数{score:.1f}/100｜{title}"
        )
    else:
        subject = (
            f"[极度贪婪提醒] CNN指数{score:.1f}/100｜{title}"
        )

    send_email(subject, plain, html)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("程序运行失败")
        raise
