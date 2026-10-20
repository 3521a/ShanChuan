
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
# 配置：继续使用你在 GitHub Secrets 中配置的 Gmail 信息
# =========================================================

LOW = 25
HIGH = 75
BEIJING = timezone(timedelta(hours=8))

FORCE = os.getenv("FORCE_SEND", "false").lower() == "true"

CNN_URL = (
    "https://production.dataviz.cnn.io/"
    "index/fearandgreed/graphdata"
)

YAHOO_CHART_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
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
# 通用工具
# =========================================================

def number(value, digits=2):
    try:
        x = float(value)
        if x != x or abs(x) == float("inf"):
            return "—"
        return f"{x:,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def valid_score(value):
    try:
        value = float(value)
        if 0 <= value <= 100:
            return value
    except (TypeError, ValueError):
        pass
    return None


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


def safe_text(value, default="—"):
    if value is None or value == "":
        return default
    return escape(str(value))


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
        return "#667085"
    if value > 0:
        return "#16804a"
    if value < 0:
        return "#c62828"
    return "#667085"


def rating_cn(value):
    value = str(value or "").strip().lower()
    mapping = {
        "extreme fear": "极度恐惧",
        "fear": "恐惧",
        "neutral": "中性",
        "greed": "贪婪",
        "extreme greed": "极度贪婪",
    }
    return mapping.get(value, value or "暂无评级")


# =========================================================
# CNN 恐惧与贪婪指数
# =========================================================

def get_cnn_data():
    last_error = None

    for attempt in range(3):
        try:
            response = requests.get(
                CNN_URL,
                headers=HEADERS,
                timeout=25,
            )

            if response.status_code == 418:
                raise RuntimeError(
                    "CNN 返回 HTTP 418，当前云端请求被拒绝"
                )

            response.raise_for_status()
            data = response.json()

            fg = data.get("fear_and_greed") or {}
            score = valid_score(fg.get("score"))

            if score is None:
                raise ValueError("CNN 响应中没有有效指数")

            logging.info("CNN 指数：%.2f", score)
            return data, score

        except Exception as exc:
            last_error = exc
            logging.warning(
                "CNN 请求第 %s/3 次失败：%s",
                attempt + 1,
                exc,
            )
            if attempt < 2:
                time.sleep(2 ** attempt)

    raise RuntimeError(
        "无法获取可靠的 CNN 指数，本次不发送市场提醒。"
    ) from last_error


# =========================================================
# Yahoo Finance 行情
# 说明：价格表现和成交量不是 ETF 净资金流量
# =========================================================

def get_yahoo_chart(symbol, range_value="3mo"):
    url = f"{YAHOO_CHART_URL}{symbol}"

    response = requests.get(
        url,
        params={
            "range": range_value,
            "interval": "1d",
        },
        headers=HEADERS,
        timeout=20,
    )
    response.raise_for_status()

    payload = response.json()
    result = (
        payload.get("chart", {})
        .get("result")
    )

    if not result:
        raise ValueError(f"{symbol} 没有可用行情")

    item = result[0]
    timestamps = item.get("timestamp") or []
    indicators = item.get("indicators") or {}
    quote_list = indicators.get("quote") or []

    if not timestamps or not quote_list:
        raise ValueError(f"{symbol} 行情数据不完整")

    quote = quote_list[0]
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    points = []

    for i, timestamp in enumerate(timestamps):
        if i >= len(closes):
            break

        close = closes[i]
        if close is None:
            continue

        try:
            close = float(close)
            volume = (
                float(volumes[i])
                if i < len(volumes) and volumes[i] is not None
                else None
            )

            points.append({
                "timestamp": int(timestamp),
                "close": close,
                "volume": volume,
            })
        except (TypeError, ValueError):
            continue

    if not points:
        raise ValueError(f"{symbol} 没有有效收盘价")

    latest = points[-1]
    previous = points[-2] if len(points) >= 2 else None

    def trailing_return(days):
        # 仅使用已获取的日线记录；不把日历天数当交易日。
        if len(points) <= days:
            return None
        return pct_change(
            latest["close"],
            points[-(days + 1)]["close"],
        )

    daily_return = (
        pct_change(latest["close"], previous["close"])
        if previous else None
    )

    latest_date = datetime.fromtimestamp(
        latest["timestamp"],
        tz=timezone.utc,
    ).astimezone(BEIJING)

    return {
        "symbol": symbol,
        "price": latest["close"],
        "daily_return": daily_return,
        "week_return": trailing_return(5),
        "month_return": trailing_return(21),
        "volume": latest["volume"],
        "date": latest_date.strftime("%Y-%m-%d"),
        "points": points,
    }


def get_market_data():
    symbols = {
        "SPY": "标普 500 ETF（SPY）",
        "VOO": "先锋标普 500 ETF（VOO）",
        "IVV": "iShares 标普 500 ETF（IVV）",
        "RSP": "标普 500 等权重 ETF（RSP）",
        "^VIX": "VIX 波动率指数",
    }

    result = {}
    errors = {}

    for symbol, name in symbols.items():
        try:
            result[symbol] = get_yahoo_chart(symbol)
            result[symbol]["name"] = name
            logging.info(
                "%s 行情获取成功，日期：%s",
                symbol,
                result[symbol]["date"],
            )
        except Exception as exc:
            errors[symbol] = str(exc)
            logging.warning("%s 行情获取失败：%s", symbol, exc)

    return result, errors


# =========================================================
# HTML 样式组件：全程不使用图片
# =========================================================

def metric_card(title, value, subtitle="", color="#202832"):
    return f"""
    <td style="width:50%;vertical-align:top;padding:6px">
      <div style="border:1px solid #e5e7eb;border-radius:10px;
        padding:14px;background:#ffffff">
        <div style="font-size:12px;color:#667085">{title}</div>
        <div style="font-size:24px;font-weight:700;
          color:{color};margin-top:6px">{value}</div>
        <div style="font-size:12px;color:#667085;margin-top:5px">
          {subtitle}
        </div>
      </div>
    </td>
    """


def section_title(title, subtitle=""):
    return f"""
    <div style="padding:22px 0 10px">
      <div style="font-size:17px;font-weight:700;color:#202832">
        {title}
      </div>
      <div style="font-size:12px;color:#667085;margin-top:4px">
        {subtitle}
      </div>
    </div>
    """


def table_row(cells, header=False):
    background = "#f2f4f7" if header else "#ffffff"
    weight = "700" if header else "400"
    return (
        f'<tr style="background:{background};font-weight:{weight}">'
        + "".join(
            f'<td style="padding:10px 7px;border-bottom:1px solid '
            f'#eaecf0;font-size:12px;text-align:left">{cell}</td>'
            for cell in cells
        )
        + "</tr>"
    )


def progress_bar(score):
    score = max(0, min(100, float(score)))
    label, color = zone(score)
    width = f"{score:.1f}%"

    return f"""
    <div style="height:8px;background:#edf0f4;border-radius:8px;
      overflow:hidden;min-width:80px">
      <div style="height:8px;width:{width};
        background:{color};border-radius:8px"></div>
    </div>
    """


# =========================================================
# 市场分析
# =========================================================

def analyze_market(score, market):
    spy = market.get("SPY")
    rsp = market.get("RSP")
    vix = market.get("^VIX")

    notes = []
    title = "市场信号仍需确认"
    title_color = "#667085"

    if score < LOW:
        notes.append(
            "CNN 指数处于极度恐惧区间，市场情绪偏谨慎。"
            "这本身不能证明市场已经见底。"
        )
    elif score > HIGH:
        notes.append(
            "CNN 指数处于极度贪婪区间，需留意拥挤交易和回撤风险。"
            "这本身不能证明市场即将见顶。"
        )
    else:
        notes.append(
            "CNN 指数未处于极端区间，仍需结合价格趋势、"
            "市场宽度和波动率判断风险。"
        )

    if spy and rsp:
        spy_week = spy.get("week_return")
        rsp_week = rsp.get("week_return")

        if spy_week is not None and rsp_week is not None:
            gap = spy_week - rsp_week

            if gap > 1.0:
                notes.append(
                    "SPY 近似一周表现明显强于 RSP，"
                    "市值加权指数相对占优；需留意上涨是否集中于大型权重股。"
                )
            elif gap < -1.0:
                notes.append(
                    "RSP 近似一周表现强于 SPY，"
                    "等权重指数相对占优，可能反映上涨参与度有所扩散。"
                )
            else:
                notes.append(
                    "SPY 与 RSP 近似一周表现接近，"
                    "暂未见到明显的相对强弱分化。"
                )

    if vix:
        vix_daily = vix.get("daily_return")
        vix_price = vix.get("price")

        if vix_price is not None and vix_daily is not None:
            if vix_daily < -5:
                notes.append(
                    "VIX 日跌幅较大，短期市场压力有所缓和；"
                    "仍需观察是否持续。"
                )
            elif vix_daily > 5:
                notes.append(
                    "VIX 日涨幅较大，市场对风险的定价有所上升。"
                )
            else:
                notes.append(
                    f"VIX 最新可用值为 {vix_price:.2f}，"
                    "请结合其持续趋势而非单日数值判断风险。"
                )

    if spy:
        spy_daily = spy.get("daily_return")
        spy_week = spy.get("week_return")

        if spy_daily is not None and spy_week is not None:
            if spy_daily > 0 and spy_week > 0:
                title = "短期价格偏强"
                title_color = "#16804a"
            elif spy_daily < 0 and spy_week < 0:
                title = "短期价格偏弱"
                title_color = "#c62828"
            else:
                title = "短期趋势分化"
                title_color = "#b26a00"

    notes.append(
        "本报告未将 ETF 价格涨跌或成交量直接解释为净资金流入流出。"
        "可靠资金流判断需要独立的份额变化、申购赎回或基金流量数据。"
    )

    return title, title_color, notes


# =========================================================
# 生成纯 HTML 邮件
# =========================================================

def build_email(data, score, market, market_errors):
    now = datetime.now(BEIJING)
    fg = data.get("fear_and_greed") or {}
    label, score_color = zone(score)

    market_title, market_color, notes = analyze_market(score, market)

    # CNN 历史对比
    history_rows = [
        ("昨日收盘", "previous_close"),
        ("一周前", "previous_1_week"),
        ("一个月前", "previous_1_month"),
        ("一年前", "previous_1_year"),
    ]

    history_html = ""
    for title, key in history_rows:
        value = valid_score(fg.get(key))
        if value is None:
            display = "—"
            rating = "暂无数据"
            color = "#667085"
        else:
            rating, color = zone(value)
            display = f"{value:.1f}"

        history_html += table_row([
            title,
            f'<span style="font-weight:700;color:{color}">{display}</span>',
            f'<span style="color:{color}">{rating}</span>',
        ])

    # 七项 CNN 子指标
    indicators = [
        ("market_momentum_sp500", "标普 500 市场动量"),
        ("stock_price_strength", "股价强度"),
        ("stock_price_breadth", "股价广度"),
        ("put_call_options", "看跌/看涨期权"),
        ("market_volatility_vix", "市场波动率"),
        ("safe_haven_demand", "避险需求"),
        ("junk_bond_demand", "垃圾债需求"),
    ]

    indicator_html = ""
    available_count = 0

    for key, title in indicators:
        item = data.get(key) or {}
        value = valid_score(item.get("score"))

        if value is None:
            continue

        available_count += 1
        rating, color = zone(value)

        indicator_html += table_row([
            title,
            progress_bar(value),
            f'<span style="font-weight:700;color:{color}">{value:.0f}</span>',
            f'<span style="color:{color}">{rating}</span>',
        ])

    if not indicator_html:
        indicator_html = table_row([
            "子指标",
            "CNN 本次响应未提供可用数据",
            "—",
            "—",
        ])

    # 行情表格
    market_html = ""
    symbols = [
        ("SPY", "标普 500 ETF"),
        ("VOO", "先锋标普 500 ETF"),
        ("IVV", "iShares 标普 500 ETF"),
        ("RSP", "标普 500 等权重 ETF"),
        ("^VIX", "VIX 波动率指数"),
    ]

    for symbol, display_name in symbols:
        item = market.get(symbol)

        if not item:
            market_html += table_row([
                display_name, "—", "—", "—", "行情暂不可用"
            ])
            continue

        daily = item.get("daily_return")
        weekly = item.get("week_return")
        monthly = item.get("month_return")

        market_html += table_row([
            display_name,
            number(item.get("price")),
            f'<span style="color:{pct_color(daily)}">'
            f'{pct_text(daily)}</span>',
            f'<span style="color:{pct_color(weekly)}">'
            f'{pct_text(weekly)}</span>',
            f'<span style="color:{pct_color(monthly)}">'
            f'{pct_text(monthly)}</span>',
        ])

    title, title_color, notes = analyze_market(score, market)
    analysis_html = "".join(
        f'<li style="margin:0 0 9px;line-height:1.7">'
        f'{escape(note)}</li>'
        for note in notes
    )

    latest_market_dates = sorted({
        item.get("date")
        for item in market.values()
        if item.get("date")
    })

    if latest_market_dates:
        market_date_text = "、".join(latest_market_dates)
    else:
        market_date_text = "行情暂不可用"

    if market_errors:
        error_text = (
            "部分行情源未能返回数据：" +
            "；".join(
                f"{symbol}: {err}"
                for symbol, err in market_errors.items()
            )
        )
    else:
        error_text = "本次所请求的行情均已返回。"

    data_timestamp = fg.get("timestamp")
    if data_timestamp:
        timestamp_text = safe_text(data_timestamp)
    else:
        timestamp_text = "CNN 未提供更新时间"

    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body style="margin:0;background:#f2f4f7;padding:16px;
font-family:Arial,'Microsoft YaHei',sans-serif;color:#202832">
<div style="max-width:720px;margin:auto;background:#ffffff;
border:1px solid #e4e7ec;border-radius:12px;overflow:hidden">

  <div style="background:#202832;color:#ffffff;padding:24px">
    <div style="font-size:11px;letter-spacing:2px;color:#cbd5e1">
      DAILY MARKET BRIEF
    </div>
    <div style="font-size:23px;font-weight:700;margin-top:10px">
      美股市场风险监测
    </div>
    <div style="font-size:12px;color:#cbd5e1;margin-top:8px">
      邮件生成时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）
    </div>
  </div>

  <div style="padding:20px">
    <table role="presentation" style="width:100%;border-collapse:collapse">
      <tr>
        {metric_card(
            "CNN 恐惧与贪婪指数",
            f"{score:.1f} / 100",
            label,
            score_color,
        )}
        {metric_card(
            "市场监测结论",
            escape(market_title),
            "基于可获取的行情与情绪数据",
            market_color,
        )}
      </tr>
    </table>

    <div style="margin-top:16px;padding:15px;border-radius:9px;
      background:#f8fafc;border:1px solid #eaecf0">
      <div style="font-size:12px;color:#667085">指数位置</div>
      <div style="height:10px;background:#edf0f4;border-radius:8px;
        margin-top:12px;overflow:hidden">
        <div style="height:10px;width:{score:.1f}%;
          background:{score_color};border-radius:8px"></div>
      </div>
      <div style="display:flex;justify-content:space-between;
        font-size:11px;color:#667085;margin-top:6px">
        <span>0 极度恐惧</span>
        <span>50 中性</span>
        <span>100 极度贪婪</span>
      </div>
    </div>

    {section_title("一、历史情绪对比", "CNN 公布的历史参考值")}
    <table style="width:100%;border-collapse:collapse">
      {table_row(["时间", "指数", "情绪"], header=True)}
      {history_html}
    </table>

    {section_title(
        "二、标普 500 与波动率",
        "日涨跌、约一周和约一个月表现；收益基于可用日线数据",
    )}
    <div style="overflow-wrap:anywhere">
    <table style="width:100%;border-collapse:collapse">
      {table_row(
          ["标的", "最新值", "日涨跌", "约一周", "约一月"],
          header=True,
      )}
      {market_html}
    </table>
    </div>
    <div style="font-size:11px;color:#667085;margin-top:7px;line-height:1.6">
      行情日期：{escape(market_date_text)}。约一周和约一月按可用交易日记录估算。
      VIX 是波动率指数，不是可直接买入的股票资产。
    </div>

    {section_title(
        "三、CNN 七项子指标",
        f"本次成功取得 {available_count}/7 项；缺失项目不作推测",
    )}
    <table style="width:100%;border-collapse:collapse">
      {table_row(["指标", "区间", "数值", "状态"], header=True)}
      {indicator_html}
    </table>

    {section_title("四、市场分析", "自动分析规则，不是买卖指令")}
    <div style="padding:16px;background:#f8fafc;
      border:1px solid #eaecf0;border-radius:9px">
      <div style="font-size:18px;font-weight:700;color:{market_color}">
        {escape(market_title)}
      </div>
      <ul style="padding-left:20px;margin-bottom:0">
        {analysis_html}
      </ul>
    </div>

    {section_title("五、资金流向：数据纪律", "不把价格变化冒充资金流")}
    <div style="font-size:13px;line-height:1.8">
      <p>本版本跟踪 SPY、VOO、IVV 与 RSP 的价格表现，并比较市值加权与等权重指数。</p>
      <p><b>注意：</b>这些 ETF 的涨跌幅和成交量并不等于净申购赎回金额。本版本尚未接入经过核验的每日 ETF 份额变化或基金净流量数据，因此不会编造“今日净流入/流出金额”。</p>
      <p>当 SPY 相对 RSP 更强时，可能意味着大型权重股相对占优；当 RSP 相对更强时，可能意味着涨势更广泛。两者都只是价格相对强弱线索，不是直接的资金流证明。</p>
    </div>

    {section_title("六、数据来源与完整性")}
    <div style="font-size:11px;line-height:1.8;color:#667085">
      <div>CNN 指数时间字段：{timestamp_text}</div>
      <div>行情数据：Yahoo Finance 日线接口；行情日期以各标的实际返回为准。</div>
      <div>{escape(error_text)}</div>
      <div>指数阈值：严格低于 {LOW} 或严格高于 {HIGH} 时触发邮件。</div>
      <div>数据可能存在延迟或缺失；本报告仅供研究参考，不构成投资建议。</div>
    </div>
  </div>
</div>
</body>
</html>"""

    plain = f"""美股市场风险监测
生成时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）

CNN 恐惧与贪婪指数：{score:.1f}/100（{label}）
市场监测结论：{market_title}

市场观察：
""" + "\n".join(f"- {note}" for note in notes) + f"""

行情数据日期：{market_date_text}
CNN 数据时间字段：{data_timestamp or "未提供"}

资金流说明：
ETF 价格涨跌和成交量不等于净申购赎回。本版本没有经过核验的每日 ETF 净流量数据，因此不报告虚构的资金流入/流出金额。

行情缺失说明：
{error_text}

本报告仅供研究参考，不构成投资建议。
"""

    return plain, html


# =========================================================
# 发送邮件：纯文本 + HTML，不带图片附件
# =========================================================

def send_email(subject, plain, html):
    username = os.environ.get("MAIL_USER")
    password = os.environ.get("MAIL_PASS")
    recipient = os.environ.get("MAIL_TO")

    if not username or not password or not recipient:
        raise RuntimeError(
            "邮件配置不完整，请检查 MAIL_USER、MAIL_PASS、MAIL_TO Secrets"
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

    logging.info("邮件发送成功，收件人：%s", recipient)


# =========================================================
# 主程序
# =========================================================

def main():
    now = datetime.now(BEIJING)
    logging.info("北京时间：%s", now.isoformat())

    # 测试模式不访问 CNN 或 Yahoo，专门验证 Gmail 是否能发信。
    if FORCE:
        plain = (
            "邮件系统测试成功。\n"
            "此邮件不代表当前市场触发阈值，也不包含实时市场数据。\n"
        )
        html = """
        <html><body style="font-family:Arial,'Microsoft YaHei',sans-serif;
        padding:24px;color:#202832">
        <h2>美股市场监测邮件测试</h2>
        <p>如果你看到这封邮件，说明 Gmail 邮件发送配置可用。</p>
        <p>此测试没有获取实时市场数据。</p>
        </body></html>
        """
        send_email("[测试] 美股市场监测邮件", plain, html)
        return

    # CNN 数据是触发提醒的依据；取不到就停止，不猜指数。
    data, score = get_cnn_data()

    # 日常监测市场行情。单个标的失败不会伪造数据。
    market, errors = get_market_data()

    # 只在严格小于 25 或严格大于 75 时发送。
    if not (score < LOW or score > HIGH):
        logging.info(
            "当前指数 %.2f，未严格低于 %s 或高于 %s，不发送提醒。",
            score,
            LOW,
            HIGH,
        )
        return

    label, _ = zone(score)
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
