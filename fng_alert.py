import os
import time
import ssl
import smtplib
import logging
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.header import Header
from email.utils import parseaddr
from html import escape

import requests

# ============================================================
# 配置
# ============================================================

LOW = float(os.getenv("LOW_THRESHOLD", "25"))
HIGH = float(os.getenv("HIGH_THRESHOLD", "75"))
BEIJING = timezone(timedelta(hours=8))

CNN_PAGE = "https://edition.cnn.com/markets/fear-and-greed"
CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
YAHOO_URLS = [
    "https://query1.finance.yahoo.com/v8/finance/chart/",
    "https://query2.finance.yahoo.com/v8/finance/chart/",
]

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

SYMBOLS = {
    "SPY": ("SPY", "标普500市值加权 ETF"),
    "RSP": ("RSP", "标普500等权重 ETF"),
    "^VIX": ("VIX", "CBOE 波动率指数"),
}

# 七项 CNN 完整核心子指标，保留动态 keys 解析和备用键名
COMPONENT_KEYS = [
    (
        "market_momentum_sp500",
        "market_momentum_sp125",
        "标普500市场动量",
    ),
    ("stock_price_strength", "股价强度"),
    ("stock_price_breadth", "股价广度"),
    ("put_call_options", "看跌/看涨期权比"),
    ("market_volatility_vix", "市场波动率 (VIX)"),
    ("safe_haven_demand", "避险资产需求"),
    ("junk_bond_demand", "垃圾债需求"),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ============================================================
# 通用工具
# ============================================================

def valid_score(value):
    try:
        number = float(value)

        if (
            number == number
            and abs(number) != float("inf")
            and 0 <= number <= 100
        ):
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
    if value is None:
        return "—"

    return f"{value:+.2f}%"


def pct_color(value):
    if value is None or value == 0:
        return "#64748b"

    return "#15803d" if value > 0 else "#b91c1c"


def pct_bg(value):
    if value is None or value == 0:
        return "#f1f5f9"

    return "#dcfce7" if value > 0 else "#fee2e2"


def zone(score):
    """返回 (状态名, 主色, 浅色徽章背景, 深色徽章文字)"""
    if score is None:
        return "数据缺失", "#94a3b8", "#f1f5f9", "#475569"

    if score < 25:
        return "极度恐惧", "#ef4444", "#fef2f2", "#b91c1c"

    if score < 45:
        return "恐惧", "#f97316", "#fff7ed", "#c2410c"

    if score <= 55:
        return "中性", "#64748b", "#f1f5f9", "#334155"

    if score <= 75:
        return "贪婪", "#22c55e", "#f0fdf4", "#15803d"

    return "极度贪婪", "#10b981", "#ecfdf5", "#047857"


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
    response = SESSION.get(
        url,
        params=params,
        timeout=timeout,
    )

    response.raise_for_status()
    return response.json()


# ============================================================
# CNN 指数
# ============================================================

def get_cnn_data():
    last_error = None

    for attempt in range(3):
        try:
            data = get_json(CNN_URL)

            if not isinstance(data, dict):
                raise ValueError("CNN 响应不是有效 JSON 对象")

            fg = data.get("fear_and_greed")

            if not isinstance(fg, dict):
                raise ValueError("CNN 响应缺少 fear_and_greed")

            score = valid_score(fg.get("score"))

            if score is None:
                raise ValueError("CNN 当前指数缺失或无效")

            timestamp = parse_time(fg.get("timestamp"))

            if timestamp is None:
                raise ValueError("CNN 指数缺少有效更新时间")

            now = datetime.now(timezone.utc)

            if timestamp > now + timedelta(hours=2):
                raise ValueError("CNN 时间戳位于未来")

            if timestamp < now - timedelta(days=10):
                raise ValueError("CNN 当前指数可能已过期")

            components = get_components(data)
            history = get_history(data)

            logging.info(
                "CNN指数：%.2f；历史数据点：%d",
                score,
                len(history),
            )

            return data, score, components, history, timestamp

        except Exception as exc:
            last_error = exc

            logging.warning(
                "CNN 请求第 %d 次失败：%s",
                attempt + 1,
                exc,
            )

            if attempt < 2:
                time.sleep(2 ** attempt)

    raise RuntimeError(
        "无法获取并验证 CNN 当前指数"
    ) from last_error


# ============================================================
# CNN 核心子指标
# ============================================================

def get_components(data):
    components = []

    keys_obj = data.get("keys", [])

    if not isinstance(keys_obj, list):
        keys_obj = []

    for spec in COMPONENT_KEYS:
        *candidate_keys, label = spec
        selected_key = None

        # 优先使用 keys 数组中声明的键
        for candidate in candidate_keys:
            if (
                candidate in keys_obj
                and isinstance(data.get(candidate), dict)
            ):
                selected_key = candidate
                break

        # keys 不存在或不完整时，使用备用键名
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


# ============================================================
# CNN 历史数据
# ============================================================

def get_history(data):
    raw = data.get("fear_and_greed_historical")

    if not isinstance(raw, dict):
        raw = data.get("fear_and_greed_historical_data")

    rows = raw.get("data", []) if isinstance(raw, dict) else []

    result = []

    # 常见结构：data = [{"x": timestamp, "y": score}, ...]
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

                # 同时兼容秒和毫秒时间戳
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

            except (
                TypeError,
                ValueError,
                OSError,
                OverflowError,
            ):
                continue

    # 兼容 data = {"x": [...], "y": [...]}
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

                except (
                    TypeError,
                    ValueError,
                    OSError,
                    OverflowError,
                ):
                    continue

    # 只保留近一年数据，按日期排序并去重
    cutoff = datetime.now(timezone.utc) - timedelta(days=366)
    unique = {}

    for point in result:
        if point["date"] >= cutoff:
            unique[point["date"]] = point["score"]

    return [
        {"date": dt, "score": value}
        for dt, value in sorted(unique.items())
    ]


# ============================================================
# 近一年走势图：高分辨率抗锯齿 CSS linear-gradient 渲染（彻底消除马赛克阶梯感）
# ============================================================

def make_chart_html(history):
    """
    使用 340 列超细密度 + 线性插值 + CSS linear-gradient 亚像素抗锯齿边缘绘制白底细蓝线图：
    1. 彻底消除原先 112 列抽样造成的粗大方块阶梯（马赛克感），每一列宽度接近 1.5px，折线细腻平滑
    2. 每一列仅用单个 <td> 的 background:linear-gradient(...) 完成绘制（无嵌套 div），整封邮件约 76KB，不触发 Gmail 102KB 折叠
    3. 零图片附件、零 base64，收件箱列表外面绝不显示图片药丸，点进正文直接高清展示
    """
    if not history or len(history) < 2:
        logging.warning("历史数据不足，无法生成走势图")
        return None

    try:
        # 使用 340 列高分辨率采样 + 相邻交易日线性插值，将原本宽约 6px 的方块阶梯细化为 ~1.5px 的连续平滑斜线
        n_cols = 340
        src_n = len(history)
        sampled = []

        for j in range(n_cols):
            pos = j * (src_n - 1) / (n_cols - 1)
            idx = int(pos)
            frac = pos - idx

            if idx >= src_n - 1:
                s = float(history[-1]["score"])
                dt = history[-1]["date"]
            else:
                s1 = float(history[idx]["score"])
                s2 = float(history[idx + 1]["score"])
                s = (1.0 - frac) * s1 + frac * s2
                dt = history[round(pos)]["date"]

            sampled.append({"date": dt, "score": s})

        chart_h = 152  # 图表高度（px）
        y_25 = int(round((100.0 - 75.0) / 100.0 * chart_h))  # 75分参考线距顶 (38px)
        y_50 = int(round((100.0 - 50.0) / 100.0 * chart_h))  # 50分参考线距顶 (76px)
        y_75 = int(round((100.0 - 25.0) / 100.0 * chart_h))  # 25分参考线距顶 (114px)

        all_scores = [float(p["score"]) for p in history]
        min_pt = min(history, key=lambda x: x["score"])
        max_pt = max(history, key=lambda x: x["score"])
        avg_score = sum(all_scores) / len(all_scores)

        cols = []
        for i, pt in enumerate(sampled):
            s = max(0.0, min(100.0, float(pt["score"])))
            prev_s = (
                max(0.0, min(100.0, float(sampled[i - 1]["score"])))
                if i > 0
                else s
            )

            y_curr = int(round((100.0 - s) / 100.0 * chart_h))
            y_prev = int(round((100.0 - prev_s) / 100.0 * chart_h))

            t1 = min(y_curr, y_prev)
            t2 = min(chart_h, max(t1 + 2, max(y_curr, y_prev) + 1))
            # 上下各加 1px 半透明过渡色 (#7dd3fc) 实现视觉抗锯齿，消除锯齿毛刺
            t0 = max(0, t1 - 1)
            t3 = min(chart_h, t2 + 1)

            line_color = "#0284c7" if i >= n_cols - 3 else "#1683c4"
            is_dash = ((i // 4) % 2 == 0)

            stops = [
                f"#fff {t0}px,#7dd3fc {t0}px {t1}px,"
                f"{line_color} {t1}px {t2}px,"
                f"#7dd3fc {t2}px {t3}px,#f0f7ff {t3}px"
            ]

            if is_dash:
                above = [
                    f"#fff {yg}px,#cbd5e1 {yg}px {yg+1}px,#fff {yg+1}px"
                    for yg in (y_25, y_50, y_75)
                    if yg + 1 < t0
                ]
                below = [
                    f"#f0f7ff {yg}px,#cbd5e1 {yg}px {yg+1}px,#f0f7ff {yg+1}px"
                    for yg in (y_25, y_50, y_75)
                    if yg > t3 + 1
                ]
                stops = above + stops + below

            cols.append(
                f'<td style="background:linear-gradient({",".join(stops)})"></td>'
            )

        # 底部 5 个等距月份标签（采用紧凑 YY/MM 格式，防止手机窄屏换行挤压）
        label_count = 5
        label_tds = []
        for k in range(label_count):
            idx = round(k * (n_cols - 1) / (label_count - 1))
            dt_str = sampled[idx]["date"].strftime("%y/%m")
            align = (
                "left"
                if k == 0
                else ("right" if k == label_count - 1 else "center")
            )
            label_tds.append(
                f'<td style="font-size:10px;color:#64748b;font-family:Consolas,Menlo,monospace;'
                f'text-align:{align};padding-top:6px;white-space:nowrap">{dt_str}</td>'
            )

        q_h = chart_h // 4
        r_h = chart_h - q_h * 3

        return (
            '<div style="margin-top:10px;background:#ffffff;'
            'border-radius:10px;padding:12px 12px 10px;border:1px solid #1e293b">'
            # 图表顶部迷你统计条（最高 / 均值 / 最低）
            '<table role="presentation" style="width:100%;border-collapse:collapse;margin-bottom:8px">'
            "<tr>"
            '<td style="font-size:11px;color:#475569;text-align:left">'
            f'近1年低点 <strong style="color:#dc2626">{min_pt["score"]:.1f}</strong> '
            f'<span style="color:#94a3b8">({min_pt["date"]:%y/%m/%d})</span>'
            "</td>"
            '<td style="font-size:11px;color:#475569;text-align:center">'
            f'均值 <strong style="color:#0f172a">{avg_score:.1f}</strong>'
            "</td>"
            '<td style="font-size:11px;color:#475569;text-align:right">'
            f'近1年高点 <strong style="color:#16a34a">{max_pt["score"]:.1f}</strong> '
            f'<span style="color:#94a3b8">({max_pt["date"]:%y/%m/%d})</span>'
            "</td>"
            "</tr>"
            "</table>"
            # 图表主体
            '<table role="presentation" style="width:100%;'
            'border-collapse:collapse;table-layout:fixed">'
            "<tr>"
            '<td style="width:22px;vertical-align:top;padding-right:4px">'
            f'<div style="height:{q_h}px;font-size:9px;color:#94a3b8;text-align:right;line-height:1;font-family:Consolas,monospace">100</div>'
            f'<div style="height:{q_h}px;font-size:9px;color:#16a34a;text-align:right;line-height:1;font-family:Consolas,monospace">75</div>'
            f'<div style="height:{q_h}px;font-size:9px;color:#94a3b8;text-align:right;line-height:1;font-family:Consolas,monospace">50</div>'
            f'<div style="height:{r_h}px;font-size:9px;color:#dc2626;text-align:right;line-height:1;font-family:Consolas,monospace">25</div>'
            "</td>"
            '<td style="vertical-align:top">'
            '<table class="c-grid" role="presentation" style="width:100%;'
            "border-collapse:collapse;table-layout:fixed;"
            'border-bottom:1px solid #cbd5e1;border-top:1px solid #f1f5f9">'
            f"<tr>{''.join(cols)}</tr>"
            "</table>"
            '<table role="presentation" style="width:100%;'
            'border-collapse:collapse;table-layout:fixed">'
            f"<tr>{''.join(label_tds)}</tr>"
            "</table>"
            "</td>"
            "</tr>"
            "</table>"
            "</div>"
        )

    except Exception:
        logging.exception("生成历史走势图失败")
        return None


# ============================================================
# Yahoo Finance 行情：SPY、RSP、VIX（双节点重试）
# ============================================================

def get_yahoo_data(symbol):
    last_error = None

    for attempt in range(3):
        base_url = YAHOO_URLS[attempt % len(YAHOO_URLS)]
        try:
            payload = get_json(
                f"{base_url}{symbol}",
                params={
                    "range": "3mo",
                    "interval": "1d",
                },
                timeout=20,
            )

            chart = payload.get("chart", {})

            if chart.get("error"):
                raise ValueError(
                    f"Yahoo 返回错误：{chart['error']}"
                )

            results = chart.get("result")

            if not results:
                raise ValueError(f"{symbol} 没有行情数据")

            item = results[0]

            timestamps = item.get("timestamp") or []
            quotes = item.get("indicators", {}).get("quote") or []

            if not timestamps or not quotes:
                raise ValueError(f"{symbol} 行情结构不完整")

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
                raise ValueError(f"{symbol} 有效日线不足22条")

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

        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"{symbol} 行情请求失败: {last_error}") from last_error


def get_market_data():
    market = {}
    errors = {}

    for symbol in SYMBOLS:
        try:
            market[symbol] = get_yahoo_data(symbol)

            logging.info(
                "%s：最新价格=%s，日期=%s",
                symbol,
                market[symbol]["price"],
                market[symbol]["date"],
            )

        except Exception as exc:
            errors[symbol] = str(exc)

            logging.warning(
                "%s 行情获取失败：%s",
                symbol,
                exc,
            )

    return market, errors


# ============================================================
# 市场分析
# ============================================================

def analyze_market(score, market, history=None):
    notes = []
    label, _, _, _ = zone(score)

    if score < LOW:
        notes.append(
            f"CNN指数为{score:.1f}，处于极度恐惧区间；"
            "极端恐惧并不代表市场已经见底。"
        )

    elif score > HIGH:
        notes.append(
            f"CNN指数为{score:.1f}，处于极度贪婪区间；"
            "极端贪婪并不代表市场必然马上反转。"
        )

    else:
        notes.append(
            f"CNN指数为{score:.1f}，当前情绪状态为{label}。"
        )

    # 近一年分位数与高低点统计
    if history and len(history) >= 10:
        hist_scores = [p["score"] for p in history]
        min_s = min(hist_scores)
        max_s = max(hist_scores)
        rank_pct = (
            sum(1 for s in hist_scores if s <= score)
            / len(hist_scores)
            * 100
        )
        notes.append(
            f"近一年情绪指数区间为 {min_s:.1f} – {max_s:.1f}，"
            f"当前读数处于近一年 {rank_pct:.0f}% 分位水平。"
        )

    spy = market.get("SPY")
    rsp = market.get("RSP")
    vix = market.get("^VIX")

    # 比较收益率，而不是ETF名义价格
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
                "等权重股票相对占优，可作为市场广度线索。"
            )

        elif gap < -1:
            notes.append(
                f"SPY近一周收益领先RSP {abs(gap):.2f}个百分点，"
                "市值加权指数相对占优，需要留意权重集中度。"
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
                f"VIX单日上涨{vix_change:.2f}%，风险定价升温。"
            )

        elif vix_change <= -5:
            notes.append(
                f"VIX单日下跌{abs(vix_change):.2f}%，波动率有所回落。"
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
        title, color = "行情数据不足，暂不判断", "#94a3b8"

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
            title, color = "价格偏强，仍待确认", "#34d399"

    elif spy["daily"] < 0 and spy["week"] < 0:
        title, color = "价格偏弱", "#fb7185"

    else:
        title, color = "价格趋势分化", "#fbbf24"

    notes.append(
        "ETF价格表现不等于资金净流入或净流出；"
        "本报告不根据价格变化虚构资金流数据。"
    )

    return title, color, notes


# ============================================================
# HTML 邮件组件（自适应手机竖屏与电脑宽屏）
# ============================================================

def badge_pill(text, bg_color, text_color):
    return (
        f'<span style="display:inline-block;padding:2px 8px;'
        f'border-radius:999px;background:{bg_color};color:{text_color};'
        f'font-size:11px;font-weight:600;line-height:1.4;white-space:nowrap">'
        f"{escape(text)}</span>"
    )


def table_row(cells, header=False, aligns=None):
    background = "#f8fafc" if header else "#ffffff"
    weight = "600" if header else "400"
    text_color = "#475569" if header else "#0f172a"
    border_color = "#e2e8f0" if header else "#f1f5f9"

    tds = []
    for idx, cell in enumerate(cells):
        align = (
            aligns[idx]
            if aligns and idx < len(aligns)
            else ("left" if idx == 0 else "right")
        )
        tds.append(
            f'<td class="m-cell" style="padding:10px 8px;'
            f"border-bottom:1px solid {border_color};"
            f"font-size:12px;color:{text_color};text-align:{align};"
            f'vertical-align:middle;line-height:1.45">{cell}</td>'
        )

    return (
        f'<tr style="background:{background};font-weight:{weight}">'
        + "".join(tds)
        + "</tr>"
    )


def section(title, subtitle=""):
    return (
        '<div style="padding:2px 0 12px;border-bottom:1px solid #f1f5f9;margin-bottom:8px">'
        f'<div style="font-size:16px;font-weight:700;'
        f'color:#0f172a;letter-spacing:0.2px">{escape(title)}</div>'
        f'<div style="font-size:12px;color:#64748b;'
        f'margin-top:3px">{escape(subtitle)}</div>'
        "</div>"
    )


def metric_card(title, value, subtitle, color):
    return f"""
    <td class="stack-col" style="width:50%;padding:5px;vertical-align:top">
      <div style="background:#131d31;border:1px solid #26364f;border-radius:12px;padding:14px 14px 12px">
        <div style="font-size:11px;color:#94a3b8;letter-spacing:0.5px">
          {escape(title)}
        </div>
        <div class="hero-val" style="font-size:22px;font-weight:700;color:{color};
        margin-top:6px;line-height:1.25;overflow-wrap:anywhere">
          {escape(str(value))}
        </div>
        <div style="font-size:12px;color:#cbd5e1;margin-top:6px;line-height:1.4">
          {escape(str(subtitle))}
        </div>
      </div>
    </td>
    """


def progress_bar(score, track_bg="#26364f", height=8):
    _, color, _, _ = zone(score)

    return (
        f'<div style="height:{height}px;background:{track_bg};'
        f'border-radius:999px;overflow:hidden">'
        f'<div style="height:{height}px;width:{score:.1f}%;'
        f'background:{color};border-radius:999px"></div>'
        "</div>"
    )


# ============================================================
# 邮件内容
# ============================================================

def build_email(
    data,
    score,
    components,
    history,
    chart_block,
    market,
    errors,
    timestamp,
):
    now = datetime.now(BEIJING)
    label, score_color, _, _ = zone(score)

    title, title_color, notes = analyze_market(
        score,
        market,
        history,
    )

    fg = data.get("fear_and_greed", {})

    # 1. 历史情绪对比
    historical_rows = []

    for label_text, key in [
        ("昨日收盘", "previous_close"),
        ("一周前", "previous_1_week"),
        ("一个月前", "previous_1_month"),
        ("一年前", "previous_1_year"),
    ]:
        value = valid_score(fg.get(key))
        rating, _, bg_c, txt_c = zone(value)
        diff = (score - value) if value is not None else None

        if diff is not None:
            diff_str = (
                f'<span style="font-family:Consolas,Menlo,monospace;font-weight:600;'
                f'color:{pct_color(diff)}">{diff:+.1f}</span>'
            )
        else:
            diff_str = "—"

        val_str = (
            f'<span style="font-family:Consolas,Menlo,monospace;font-weight:600;color:#0f172a">'
            f"{fmt_number(value, 1)}</span>"
            if value is not None
            else "—"
        )

        historical_rows.append(
            table_row(
                [
                    f'<span style="font-weight:500;color:#334155">{escape(label_text)}</span>',
                    val_str,
                    diff_str,
                    badge_pill(rating, bg_c, txt_c),
                ],
                aligns=["left", "right", "right", "right"],
            )
        )

    # 2. 七项核心子指标（将进度条嵌在指标名称下方，在手机窄屏与桌面端都宽敞清晰）
    component_rows = []

    for component in components:
        value = component["score"]
        rating, color, bg_c, txt_c = zone(value)

        if value is not None:
            value_html = (
                f'<span style="font-family:Consolas,Menlo,monospace;'
                f'color:{color};font-weight:700;font-size:13px">'
                f"{value:.1f}</span>"
            )
            bar_html = progress_bar(value, track_bg="#e2e8f0", height=6)
        else:
            value_html = "—"
            bar_html = (
                '<div style="height:6px;background:#f1f5f9;'
                'border-radius:999px"></div>'
            )

        name_and_bar = (
            f'<div style="font-weight:600;color:#1e293b;font-size:12px;'
            f'margin-bottom:6px">{escape(component["label"])}</div>'
            f"{bar_html}"
        )

        component_rows.append(
            table_row(
                [
                    name_and_bar,
                    value_html,
                    badge_pill(rating, bg_c, txt_c),
                ],
                aligns=["left", "right", "right"],
            )
        )

    # 3. SPY、RSP、VIX 行情（代码与中文名上下分层，避免手机端挤压换行）
    market_rows = []
    market_dates = []

    for symbol, (short_code, cn_name) in SYMBOLS.items():
        item = market.get(symbol)

        symbol_cell = (
            f'<div style="font-weight:700;color:#0f172a;font-size:13px;'
            f'font-family:Consolas,Menlo,monospace">{escape(short_code)}</div>'
            f'<div style="font-size:11px;color:#64748b;margin-top:1px">'
            f"{escape(cn_name)}</div>"
        )

        if not item:
            market_rows.append(
                table_row(
                    [
                        symbol_cell,
                        "—",
                        "—",
                        "—",
                        '<span style="color:#94a3b8;font-size:11px">不可用</span>',
                    ],
                    aligns=["left", "right", "right", "right", "right"],
                )
            )
            continue

        market_dates.append(f"{short_code}: {item['date']}")

        def fmt_pct_pill(val):
            if val is None:
                return "—"
            return (
                f'<span style="display:inline-block;padding:2px 6px;border-radius:6px;'
                f"background:{pct_bg(val)};color:{pct_color(val)};"
                f'font-family:Consolas,Menlo,monospace;font-size:11px;font-weight:600;white-space:nowrap">'
                f"{pct_text(val)}</span>"
            )

        def fmt_pct_plain(val):
            if val is None:
                return "—"
            return (
                f'<span style="color:{pct_color(val)};font-family:Consolas,Menlo,monospace;'
                f'font-size:12px;font-weight:600;white-space:nowrap">{pct_text(val)}</span>'
            )

        price_html = (
            f'<div style="font-family:Consolas,Menlo,monospace;font-weight:700;'
            f'color:#0f172a;font-size:13px">{fmt_number(item["price"])}</div>'
            f'<div style="font-size:10px;color:#94a3b8;margin-top:1px">{escape(item["date"][5:])}</div>'
        )

        market_rows.append(
            table_row(
                [
                    symbol_cell,
                    price_html,
                    fmt_pct_pill(item["daily"]),
                    fmt_pct_plain(item["week"]),
                    fmt_pct_plain(item["month"]),
                ],
                aligns=["left", "right", "right", "right", "right"],
            )
        )

    notes_html = "".join(
        f"<li style='margin:0 0 8px;line-height:1.65;color:#334155;font-size:13px'>"
        f"{escape(note)}</li>"
        for note in notes
    )

    if chart_block:
        chart_html = chart_block
    else:
        chart_html = (
            '<div style="margin-top:10px;padding:18px;'
            'border:1px dashed #475569;border-radius:8px;'
            'color:#cbd5e1;font-size:13px">'
            "近一年走势图暂不可用：历史数据缺失或图表生成失败。"
            "</div>"
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
        else "本次行情请求均已正常返回"
    )

    # 隐藏的收件箱预览摘要（确保手机与电脑收件箱列表外面看到的摘要永远干净一致）
    preheader_text = (
        f"DAILY MARKET BRIEF CNN 恐惧与贪婪指数 生成时间：{now:%Y-%m-%d %H:%M:%S}（北京时间） "
        f"当前指数 {score:.1f} / 100 {label} 市场监测 {title} 规则型观察，不是买卖指令"
    )

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<meta name="x-apple-disable-message-reformatting">
<meta name="format-detection" content="telephone=no,date=no,address=no,email=no">
<style>
/* 高分辨率抗锯齿走势图单元格 */
.c-grid td {{ height:152px; padding:0; margin:0; border:0; }}

/* 手机端自适应响应式布局 */
@media only screen and (max-width: 600px) {{
  .outer-wrap {{ padding: 8px 6px !important; }}
  .hero-box {{ padding: 16px 12px !important; border-radius: 12px !important; }}
  .card-box {{ padding: 14px 12px !important; border-radius: 10px !important; }}
  .stack-col {{ display: block !important; width: 100% !important; box-sizing: border-box !important; padding: 4px 0 !important; }}
  .hero-title {{ font-size: 20px !important; }}
  .hero-val {{ font-size: 20px !important; }}
  .m-cell {{ padding: 8px 5px !important; font-size: 11px !important; }}
  .c-grid td {{ height: 132px !important; }}
}}
</style>
</head>
<body style="margin:0;padding:0;background:#f1f5f9;color:#0f172a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%">

<!-- 隐藏预览文本：固定收件箱列表外部显示的文字摘要 -->
<div style="display:none;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;font-size:1px;line-height:1px;color:#f1f5f9">
  {escape(preheader_text)}&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;
</div>

<div class="outer-wrap" style="max-width:720px;margin:0 auto;padding:16px 12px">

  <!-- 深色 Hero 卡片 -->
  <div class="hero-box" style="background:#0b1220;color:#f8fafc;border-radius:16px;padding:22px 18px;margin-bottom:14px;box-shadow:0 4px 12px rgba(15,23,42,0.08)">

    <table role="presentation" style="width:100%;border-collapse:collapse">
      <tr>
        <td style="font-size:11px;letter-spacing:1.8px;color:#94a3b8;font-weight:600">
          DAILY MARKET BRIEF
        </td>
        <td style="font-size:11px;color:#64748b;text-align:right;font-family:Consolas,Menlo,monospace">
          {now:%Y-%m-%d %H:%M} BJT
        </td>
      </tr>
    </table>

    <div class="hero-title" style="font-size:23px;line-height:1.3;font-weight:700;margin-top:8px;color:#ffffff">
      CNN 恐惧与贪婪指数
    </div>

    <div style="font-size:12px;color:#94a3b8;margin-top:5px">
      生成时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）
    </div>

    <table role="presentation" style="width:100%;border-collapse:collapse;margin-top:14px">
      <tr>
        {metric_card("当前指数", f"{score:.1f} / 100", f"当前处于「{label}」区间", score_color)}
        {metric_card("市场监测", title, "规则型观察，不是买卖指令", title_color)}
      </tr>
    </table>

    <!-- 五档色带与当前进度 -->
    <div style="margin-top:14px;background:#131d31;border:1px solid #26364f;border-radius:10px;padding:10px 12px">
      <table role="presentation" style="width:100%;border-collapse:collapse;margin-bottom:6px">
        <tr>
          <td style="font-size:11px;color:#cbd5e1;font-weight:600">情绪区间位置</td>
          <td style="font-size:11px;color:{score_color};text-align:right;font-weight:700;font-family:Consolas,Menlo,monospace">{score:.1f}% · {escape(label)}</td>
        </tr>
      </table>

      {progress_bar(score, track_bg="#26364f", height=9)}

      <table role="presentation" style="width:100%;border-collapse:collapse;margin-top:6px;table-layout:fixed">
        <tr>
          <td style="font-size:10px;color:#ef4444;text-align:left">0 极度恐惧</td>
          <td style="font-size:10px;color:#f97316;text-align:center">25 恐惧</td>
          <td style="font-size:10px;color:#94a3b8;text-align:center">50 中性</td>
          <td style="font-size:10px;color:#22c55e;text-align:center">75 贪婪</td>
          <td style="font-size:10px;color:#10b981;text-align:right">100 极度贪婪</td>
        </tr>
      </table>
    </div>

    <table role="presentation" style="width:100%;border-collapse:collapse;margin-top:18px">
      <tr>
        <td style="font-size:14px;font-weight:700;color:#f8fafc">近一年情绪走势</td>
        <td style="font-size:11px;color:#94a3b8;text-align:right">共 {len(history)} 个交易日数据点</td>
      </tr>
    </table>

    {chart_html}

    <div style="font-size:11px;color:#94a3b8;margin-top:8px;line-height:1.5">
      蓝线为 CNN 指数历史走势；浅灰虚线为 25、50、75 参考阈值。
      CNN数据时间：{escape(timestamp.strftime("%Y-%m-%d %H:%M UTC"))}
    </div>

  </div>

  <!-- 一、历史情绪对比 -->
  <div class="card-box" style="background:#ffffff;border:1px solid #e2e8f0;border-radius:14px;padding:16px;margin-bottom:14px">
    {section("一、历史情绪对比", "CNN接口提供的历史参考值与当前读数差值")}

    <table role="presentation" style="width:100%;border-collapse:collapse">
      {table_row(["时间节点", "历史指数", "较当时变化", "当时状态"], True, ["left", "right", "right", "right"])}
      {''.join(historical_rows)}
    </table>
  </div>

  <!-- 二、核心子指标 -->
  <div class="card-box" style="background:#ffffff;border:1px solid #e2e8f0;border-radius:14px;padding:16px;margin-bottom:14px">
    {section("二、核心子指标", "CNN 7项构成指标及分项情绪水位")}

    <table role="presentation" style="width:100%;border-collapse:collapse">
      {table_row(["构成指标与水位", "分数", "状态"], True, ["left", "right", "right"])}
      {''.join(component_rows)}
    </table>
  </div>

  <!-- 三、市场行情 -->
  <div class="card-box" style="background:#ffffff;border:1px solid #e2e8f0;border-radius:14px;padding:16px;margin-bottom:14px">
    {section("三、市场行情", "SPY市值加权、RSP等权重、VIX波动率")}

    <table role="presentation" style="width:100%;border-collapse:collapse">
      {table_row(["标的", "最新收盘", "日涨跌", "约一周", "约一月"], True, ["left", "right", "right", "right", "right"])}
      {''.join(market_rows)}
    </table>

    <div style="font-size:11px;color:#64748b;margin-top:10px;line-height:1.6">
      各标的交易日期：{escape(data_dates)}。
      约一周按5个交易日间隔计算，约一月按21个交易日间隔计算。
      SPY与RSP收益差可提供市场广度线索，但不能替代完整市场宽度统计。
    </div>
  </div>

  <!-- 四、市场分析 -->
  <div class="card-box" style="background:#ffffff;border:1px solid #e2e8f0;border-radius:14px;padding:16px;margin-bottom:14px">
    {section("四、市场分析", "将价格、情绪与波动率分开观察")}

    <div style="display:inline-block;padding:4px 10px;border-radius:8px;background:#0f172a;color:{title_color};font-size:14px;font-weight:700;margin-bottom:12px">
      {escape(title)}
    </div>

    <ul style="padding-left:18px;margin:0">
      {notes_html}
    </ul>
  </div>

  <!-- 五、数据与风险提示 -->
  <div class="card-box" style="background:#ffffff;border:1px solid #e2e8f0;border-radius:14px;padding:16px">
    {section("五、数据与风险提示", "数据来源与运行参数说明")}

    <div style="font-size:12px;line-height:1.85;color:#475569;overflow-wrap:anywhere">
      <div>• <strong>CNN页面</strong>：<a href="{CNN_PAGE}" style="color:#0284c7;text-decoration:none">{CNN_PAGE}</a></div>
      <div>• <strong>行情来源</strong>：Yahoo Finance 日线数据（{escape(error_text)}）</div>
      <div>• <strong>提醒阈值</strong>：指数严格低于 {LOW:g} 或严格高于 {HIGH:g} 时发送（FORCE_SEND=true 时强制发送）</div>
      <div>• <strong>免责声明</strong>：本报告由自动化程序生成，仅供个人研究参考，不构成任何投资或交易建议。</div>
    </div>
  </div>

</div>
</body>
</html>"""

    # 收件箱外部摘要文字保持与此前完全一致的清爽样式
    plain = (
        f"DAILY MARKET BRIEF CNN 恐惧与贪婪指数 生成时间：{now:%Y-%m-%d %H:%M:%S}（北京时间） "
        f"当前指数 {score:.1f} / 100 {label} 市场监测 {title} 规则型观察，不是买卖指令\n\n"
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
        "ETF价格变化不等于资金净流入或净流出。\n"
        "本报告仅供研究参考，不构成投资建议。\n"
    )

    return plain, html, title


# ============================================================
# Gmail SMTP（纯文本 + HTML，无任何图片/附件挂载，收件箱外部不显示图片标签）
# ============================================================

def send_email(subject, plain, html):
    username = os.getenv("MAIL_USER")
    password = os.getenv("MAIL_PASS")
    recipient_raw = os.getenv("MAIL_TO")

    if not username or not password or not recipient_raw:
        raise RuntimeError(
            "邮件配置不完整，请设置 MAIL_USER、MAIL_PASS、MAIL_TO"
        )

    recipients = [
        parseaddr(addr)[1]
        for addr in recipient_raw.replace(";", ",").split(",")
        if parseaddr(addr)[1]
    ]

    if not recipients:
        raise RuntimeError("MAIL_TO 未包含有效收件人邮箱地址")

    message = EmailMessage()
    message["Subject"] = str(Header(subject, "utf-8"))
    message["From"] = username
    message["To"] = ", ".join(recipients)

    message.set_content(plain)
    message.add_alternative(html, subtype="html")

    context = ssl.create_default_context()

    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(
            SMTP_HOST,
            SMTP_PORT,
            timeout=30,
            context=context,
        ) as server:
            server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(
            SMTP_HOST,
            SMTP_PORT,
            timeout=30,
        ) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(username, password)
            server.send_message(message)

    logging.info("邮件发送成功：%s", ", ".join(recipients))


# ============================================================
# 主程序
# ============================================================

def main():
    logging.info(
        "开始运行；北京时间=%s",
        datetime.now(BEIJING).isoformat(),
    )

    (
        data,
        score,
        components,
        history,
        timestamp,
    ) = get_cnn_data()

    # 非强制发送模式下，只有指数低于 LOW 或高于 HIGH 时才继续生成并发送邮件
    if not FORCE_SEND and not (score < LOW or score > HIGH):
        logging.info(
            "CNN指数 %.2f，未触发极端情绪邮件阈值（<%g 或 >%g）。",
            score,
            LOW,
            HIGH,
        )
        return

    market, errors = get_market_data()
    chart_block = make_chart_html(history)

    plain, html, title = build_email(
        data=data,
        score=score,
        components=components,
        history=history,
        chart_block=chart_block,
        market=market,
        errors=errors,
        timestamp=timestamp,
    )

    if score < LOW:
        prefix = "[极度恐惧提醒]"
    elif score > HIGH:
        prefix = "[极度贪婪提醒]"
    else:
        prefix = "[强制发送]"

    subject = f"{prefix} CNN指数{score:.1f}/100｜{title}"

    send_email(
        subject=subject,
        plain=plain,
        html=html,
    )


if __name__ == "__main__":
    try:
        main()

    except Exception:
        logging.exception("程序运行失败")
        raise
