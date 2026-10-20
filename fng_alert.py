
import os
import ssl
import time
import smtplib
import logging
from io import BytesIO
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.header import Header
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Wedge, Circle


# ==================== 基本设置 ====================

LOW = 25
HIGH = 75
BEIJING = timezone(timedelta(hours=8))

FORCE = os.getenv("FORCE_SEND", "false").lower() == "true"

CNN_URL = (
    "https://production.dataviz.cnn.io/"
    "index/fearandgreed/graphdata"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://edition.cnn.com",
    "Referer": "https://edition.cnn.com/markets/fear-and-greed",
}

ZONES = [
    (0, 25, "#d9534f", "极度恐惧"),
    (25, 45, "#f0ad4e", "恐惧"),
    (45, 55, "#9aa0a6", "中性"),
    (55, 75, "#7cb342", "贪婪"),
    (75, 100, "#2e7d32", "极度贪婪"),
]

RATING_CN = {
    "extreme fear": "极度恐惧",
    "fear": "恐惧",
    "neutral": "中性",
    "greed": "贪婪",
    "extreme greed": "极度贪婪",
}

INDICATORS = [
    ("market_momentum_sp500", "市场动量"),
    ("stock_price_strength", "股价强度"),
    ("stock_price_breadth", "股价广度"),
    ("put_call_options", "看跌/看涨期权"),
    ("market_volatility_vix", "市场波动率"),
    ("safe_haven_demand", "避险需求"),
    ("junk_bond_demand", "垃圾债需求"),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


# ==================== 数据获取 ====================

def get_data():
    """获取 CNN 指数。失败时重试，绝不伪造数据。"""
    last_error = None

    with requests.Session() as session:
        session.headers.update(HEADERS)

        for attempt in range(3):
            try:
                response = session.get(CNN_URL, timeout=25)

                if response.status_code == 418:
                    raise RuntimeError(
                        "CNN 返回 HTTP 418，拒绝当前云端请求"
                    )

                response.raise_for_status()
                data = response.json()

                fg = data.get("fear_and_greed")
                if not isinstance(fg, dict) or "score" not in fg:
                    raise ValueError("CNN 响应中没有有效的指数数据")

                score = float(fg["score"])
                if not 0 <= score <= 100:
                    raise ValueError(f"指数超出有效范围：{score}")

                logging.info("CNN 指数获取成功：%.2f", score)
                return data, score

            except Exception as exc:
                last_error = exc
                logging.warning(
                    "获取指数失败，第 %s/3 次：%s",
                    attempt + 1,
                    exc,
                )
                if attempt < 2:
                    time.sleep(2 ** attempt)

    raise RuntimeError(
        "无法取得可靠的 CNN 指数。"
        "本次不会发送市场阈值提醒，请查看运行日志。"
    ) from last_error


# ==================== 指数区间 ====================

def zone_of(score):
    score = float(score)

    if score < 25:
        return "#d9534f", "极度恐惧"
    if score < 45:
        return "#f0ad4e", "恐惧"
    if score <= 55:
        return "#9aa0a6", "中性"
    if score <= 75:
        return "#7cb342", "贪婪"
    return "#2e7d32", "极度贪婪"


def cn_rating(value):
    return RATING_CN.get(
        str(value or "").lower(),
        "暂无评级",
    )


def safe_number(value):
    try:
        result = float(value)
        if 0 <= result <= 100:
            return result
    except (TypeError, ValueError):
        pass
    return None


# ==================== 图表生成 ====================

def gauge_png(score):
    fig, ax = plt.subplots(figsize=(7, 4.1))

    for lo, hi, color, _ in ZONES:
        ax.add_patch(
            Wedge(
                (0, 0),
                1,
                180 - hi * 1.8,
                180 - lo * 1.8,
                width=0.25,
                facecolor=color,
                edgecolor="white",
                linewidth=2,
            )
        )

    for value in (0, 25, 50, 75, 100):
        angle = (180 - value * 1.8) * 3.141592653589793 / 180
        import math
        ax.text(
            1.12 * math.cos(angle),
            1.12 * math.sin(angle),
            str(value),
            ha="center",
            va="center",
            fontsize=10,
            color="#666666",
        )

    import math
    angle = (180 - score * 1.8) * 3.141592653589793 / 180

    ax.plot(
        [0, 0.70 * math.cos(angle)],
        [0, 0.70 * math.sin(angle)],
        color="#202124",
        linewidth=4,
        solid_capstyle="round",
    )
    ax.add_patch(Circle((0, 0), 0.06, color="#202124"))

    color, label = zone_of(score)
    ax.text(
        0,
        -0.28,
        f"{score:.1f}",
        ha="center",
        va="center",
        fontsize=34,
        fontweight="bold",
        color=color,
    )
    ax.text(
        0,
        -0.48,
        label,
        ha="center",
        va="center",
        fontsize=13,
        color=color,
    )

    ax.set_xlim(-1.25, 1.25)
    ax.set_ylim(-0.65, 1.25)
    ax.set_aspect("equal")
    ax.axis("off")

    buf = BytesIO()
    fig.savefig(
        buf,
        format="png",
        dpi=160,
        facecolor="white",
        bbox_inches="tight",
    )
    plt.close(fig)
    return buf.getvalue()


def trend_png(data):
    history = (
        data.get("fear_and_greed_historical") or {}
    ).get("data") or []

    points = []
    for item in history:
        try:
            timestamp = float(item["x"]) / 1000
            value = float(item["y"])
            if 0 <= value <= 100:
                points.append(
                    (
                        datetime.fromtimestamp(
                            timestamp, tz=timezone.utc
                        ),
                        value,
                    )
                )
        except (KeyError, TypeError, ValueError, OSError):
            continue

    points = points[-90:]
    if len(points) < 2:
        return None

    dates = [p[0] for p in points]
    values = [p[1] for p in points]

    fig, ax = plt.subplots(figsize=(7, 2.8))
    ax.axhspan(0, LOW, color="#d9534f", alpha=0.12)
    ax.axhspan(HIGH, 100, color="#2e7d32", alpha=0.12)
    ax.axhline(LOW, color="#d9534f", linestyle="--", linewidth=1)
    ax.axhline(HIGH, color="#2e7d32", linestyle="--", linewidth=1)

    ax.plot(dates, values, linewidth=2)
    ax.scatter([dates[-1]], [values[-1]], zorder=5)

    ax.set_ylim(0, 100)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.tick_params(labelsize=8)

    for side in ("top", "right"):
        ax.spines[side].set_visible(False)

    ax.grid(axis="y", alpha=0.2)
    fig.autofmt_xdate()

    buf = BytesIO()
    fig.savefig(
        buf,
        format="png",
        dpi=160,
        facecolor="white",
        bbox_inches="tight",
    )
    plt.close(fig)
    return buf.getvalue()


# ==================== 邮件 HTML 面板 ====================

def build_html(data, score, has_trend):
    fg = data.get("fear_and_greed") or {}
    color, label = zone_of(score)
    now = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")

    if score < LOW:
        summary = (
            "指数低于 25，已触发极度恐惧提醒。"
            "极端情绪不代表市场必然见底。"
        )
    elif score > HIGH:
        summary = (
            "指数高于 75，已触发极度贪婪提醒。"
            "极端情绪不代表市场必然见顶。"
        )
    elif FORCE:
        summary = "这是一封手动测试邮件，当前指数未达到提醒阈值。"
    else:
        summary = "当前指数处于常规区间。"

    history_items = [
        ("昨日收盘", "previous_close"),
        ("一周前", "previous_1_week"),
        ("一个月前", "previous_1_month"),
        ("一年前", "previous_1_year"),
    ]

    history_cells = ""
    for title, key in history_items:
        value = safe_number(fg.get(key))
        if value is None:
            display = "—"
            value_color = "#999999"
            rating = ""
        else:
            display = f"{value:.0f}"
            value_color, rating = zone_of(value)

        history_cells += f"""
        <td style="text-align:center;padding:12px 3px">
          <div style="font-size:12px;color:#7b8490">{title}</div>
          <div style="font-size:23px;font-weight:bold;color:{value_color}">
            {display}
          </div>
          <div style="font-size:11px;color:{value_color}">{rating}</div>
        </td>
        """

    indicator_rows = ""
    for key, name in INDICATORS:
        item = data.get(key) or {}
        value = safe_number(item.get("score"))

        if value is None:
            continue

        bar_color, _ = zone_of(value)
        rating = cn_rating(item.get("rating"))

        indicator_rows += f"""
        <tr>
          <td style="padding:10px 5px 10px 0;font-size:13px">
            {name}
          </td>
          <td style="padding:10px 5px;width:38%">
            <div style="height:8px;background:#edf0f3;border-radius:8px">
              <div style="height:8px;width:{value}%;
                background:{bar_color};border-radius:8px"></div>
            </div>
          </td>
          <td style="padding:10px 0;text-align:right;
            white-space:nowrap;color:{bar_color};font-weight:bold">
            {value:.0f} {rating}
          </td>
        </tr>
        """

    if not indicator_rows:
        indicator_rows = """
        <tr><td colspan="3" style="padding:12px;color:#888">
          当前数据源没有提供子指标数据。
        </td></tr>
        """

    trend_section = ""
    if has_trend:
        trend_section = """
        <section style="padding:22px 24px 0">
          <h3 style="font-size:16px;margin:0 0 10px">
            近期指数走势
          </h3>
          <img src="cid:trend" alt="指数走势"
            style="width:100%;max-width:600px;display:block">
          <p style="font-size:11px;color:#8b929c">
            虚线为 25 和 75 的提醒阈值。
          </p>
        </section>
        """

    return f"""<!doctype html>
<html>
<body style="margin:0;padding:16px;background:#edf0f4;
font-family:Arial,'Microsoft YaHei',sans-serif;color:#222">
<div style="max-width:680px;margin:auto;background:#fff;
border-radius:16px;overflow:hidden;border:1px solid #e2e6eb">

  <header style="padding:24px;background:#17191d;color:#fff">
    <div style="font-size:12px;letter-spacing:2px;color:#c6cbd2">
      FEAR &amp; GREED BRIEF
    </div>
    <h1 style="font-size:21px;margin:14px 0 5px">
      美股 · CNN 恐惧与贪婪指数
    </h1>
    <div style="font-size:13px;color:#c6cbd2">
      数据检查时间：北京时间 {now}
    </div>
  </header>

  <section style="padding:18px 22px 0;text-align:center">
    <div style="font-size:14px;color:#7b8490">当前指数</div>
    <div style="font-size:48px;font-weight:bold;color:{color}">
      {score:.1f}
    </div>
    <div style="font-size:17px;font-weight:bold;color:{color}">
      {label}
    </div>
    <img src="cid:gauge" alt="指数仪表盘"
      style="width:100%;max-width:460px;display:block;margin:auto">
  </section>

  <section style="margin:12px 22px;padding:15px;
  background:#f6f7f9;border-left:4px solid {color};
  line-height:1.7;font-size:14px">
    {summary}
  </section>

  <section style="padding:18px 22px 0">
    <h3 style="font-size:16px;margin:0">历史对比</h3>
    <table style="width:100%;border-collapse:collapse">
      <tr>{history_cells}</tr>
    </table>
  </section>

  {trend_section}

  <section style="padding:22px">
    <h3 style="font-size:16px;margin:0 0 8px">七项市场子指标</h3>
    <table style="width:100%;border-collapse:collapse">
      {indicator_rows}
    </table>
  </section>

  <footer style="padding:18px 22px;background:#f6f7f9;
  font-size:11px;line-height:1.8;color:#7b8490">
    数据来源：CNN Fear &amp; Greed Index。<br>
    提醒条件：指数严格低于 {LOW} 或严格高于 {HIGH}。<br>
    指数和子指标可能存在更新延迟。本简报仅供参考，不构成投资建议。
  </footer>
</div>
</body>
</html>"""


# ==================== 邮件发送 ====================

def send_mail(subject, plain, html, images):
    username = os.environ["MAIL_USER"]
    password = os.environ["MAIL_PASS"]
    recipient = os.environ["MAIL_TO"]

    message = MIMEMultipart("related")
    message["Subject"] = str(Header(subject, "utf-8"))
    message["From"] = username
    message["To"] = recipient
    message["Date"] = formatdate(localtime=True)

    alternative = MIMEMultipart("alternative")
    message.attach(alternative)

    alternative.attach(MIMEText(plain, "plain", "utf-8"))
    alternative.attach(MIMEText(html, "html", "utf-8"))

    for content_id, image_bytes in images.items():
        image = MIMEImage(image_bytes, _subtype="png")
        image.add_header("Content-ID", f"<{content_id}>")
        image.add_header(
            "Content-Disposition",
            "inline",
            filename=f"{content_id}.png",
        )
        message.attach(image)

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
    now = datetime.now(BEIJING)
    logging.info("北京时间：%s", now.isoformat())

    # 测试邮件不依赖 CNN 接口，先单独验证邮件配置。
    if FORCE:
        test_html = """
        <html><body style="font-family:Arial,sans-serif;padding:24px">
        <h2>CNN Fear &amp; Greed Alert</h2>
        <p>测试邮件发送成功。</p>
        <p>邮件服务配置可用；这封邮件不代表当前指数已触发阈值。</p>
        </body></html>
        """
        send_mail(
            "[测试] CNN 恐惧与贪婪指数邮件系统",
            "测试邮件发送成功。此邮件不代表指数已触发阈值。",
            test_html,
            {},
        )
        return

    data, score = get_data()

    logging.info("当前 CNN 指数：%.2f", score)

    # 严格阈值：小于 25 或大于 75。
    if not (score < LOW or score > HIGH):
        logging.info("指数未触发阈值，不发送邮件。")
        return

    color, label = zone_of(score)
    images = {"gauge": gauge_png(score)}

    trend = trend_png(data)
    if trend:
        images["trend"] = trend

    if score < LOW:
        emoji = "⚠️"
        direction = "低位恐惧提醒"
    else:
        emoji = "🔥"
        direction = "高位贪婪提醒"

    subject = f"{emoji} 美股情绪提醒：{score:.1f}｜{direction}"

    plain = (
        f"CNN 恐惧与贪婪指数：{score:.1f}/100\n"
        f"情绪区间：{label}\n"
        f"北京时间：{now:%Y-%m-%d %H:%M:%S}\n"
        f"触发条件：指数严格低于 {LOW} 或严格高于 {HIGH}\n"
        "本邮件仅供参考，不构成投资建议。"
    )

    html = build_html(data, score, "trend" in images)

    send_mail(subject, plain, html, images)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("程序运行失败")
        raise
