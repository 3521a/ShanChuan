
import os
import smtplib
import requests
from io import BytesIO
from datetime import datetime, timezone, timedelta
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Wedge, Circle

LOW = 25
HIGH = 75
BEIJING = timezone(timedelta(hours=8))

FORCE = os.getenv("FORCE_SEND", "false").lower() == "true"
URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://edition.cnn.com/markets/fear-and-greed",
    "Origin": "https://edition.cnn.com",
}

ZONES = [
    (0, 25, "#d94b40", "极度恐惧"),
    (25, 45, "#e9a23b", "恐惧"),
    (45, 55, "#9299a1", "中性"),
    (55, 75, "#79a96b", "贪婪"),
    (75, 100, "#287d50", "极度贪婪"),
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


def zone_of(score):
    for lo, hi, color, name in ZONES:
        if score <= hi:
            return color, name
    return ZONES[-1][2], ZONES[-1][3]


def get_data():
    response = requests.get(URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    data = response.json()

    fg = data.get("fear_and_greed")
    if not isinstance(fg, dict) or "score" not in fg:
        raise ValueError("CNN 返回的数据中没有 fear_and_greed.score")

    score = float(fg["score"])
    if not 0 <= score <= 100:
        raise ValueError(f"CNN 指数超出有效范围：{score}")

    return data, score


def gauge_png(score):
    fig, ax = plt.subplots(figsize=(7, 4.0))

    for lo, hi, color, _ in ZONES:
        ax.add_patch(
            Wedge(
                (0, 0), 1,
                180 - hi * 1.8,
                180 - lo * 1.8,
                width=0.28,
                facecolor=color,
                edgecolor="white",
                linewidth=2,
            )
        )

    for value in (0, 25, 50, 75, 100):
        angle = (180 - value * 1.8) * 3.141592653589793 / 180
        ax.text(
            1.12 * __import__("math").cos(angle),
            1.12 * __import__("math").sin(angle),
            str(value),
            ha="center",
            va="center",
            fontsize=10,
            color="#666666",
        )

    import math
    angle = math.radians(180 - score * 1.8)
    ax.plot(
        [0, 0.72 * math.cos(angle)],
        [0, 0.72 * math.sin(angle)],
        color="#222222",
        linewidth=3,
        solid_capstyle="round",
    )
    ax.add_patch(Circle((0, 0), 0.055, color="#222222"))

    color, label = zone_of(score)
    ax.text(
        0, -0.28, f"{score:.0f}",
        ha="center", va="center",
        fontsize=34, fontweight="bold", color=color,
    )
    ax.text(
        0, -0.47, label,
        ha="center", va="center",
        fontsize=13, color=color,
    )

    ax.set_xlim(-1.25, 1.25)
    ax.set_ylim(-0.65, 1.22)
    ax.set_aspect("equal")
    ax.axis("off")

    output = BytesIO()
    fig.savefig(
        output, format="png", dpi=160,
        facecolor="white", bbox_inches="tight",
    )
    plt.close(fig)
    return output.getvalue()


def trend_png(history):
    if not isinstance(history, list):
        return None

    points = [
        item for item in history
        if isinstance(item, dict)
        and item.get("x") is not None
        and item.get("y") is not None
    ][-90:]

    if len(points) < 5:
        return None

    dates = [
        datetime.fromtimestamp(float(p["x"]) / 1000, tz=timezone.utc)
        for p in points
    ]
    values = [float(p["y"]) for p in points]

    fig, ax = plt.subplots(figsize=(7, 2.8))
    ax.axhspan(0, LOW, color="#d94b40", alpha=0.12)
    ax.axhspan(HIGH, 100, color="#287d50", alpha=0.12)
    ax.axhline(LOW, color="#d94b40", linestyle="--", linewidth=1)
    ax.axhline(HIGH, color="#287d50", linestyle="--", linewidth=1)
    ax.plot(dates, values, color="#2166d1", linewidth=2)
    ax.scatter([dates[-1]], [values[-1]], color="#2166d1", zorder=5)

    ax.set_ylim(0, 100)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.tick_params(labelsize=8, colors="#666666")

    for side in ("top", "right"):
        ax.spines[side].set_visible(False)

    ax.grid(axis="y", alpha=0.2)

    output = BytesIO()
    fig.savefig(
        output, format="png", dpi=160,
        facecolor="white", bbox_inches="tight",
    )
    plt.close(fig)
    return output.getvalue()


def build_html(data, score, has_trend, is_test):
    fg = data["fear_and_greed"]
    color, label = zone_of(score)
    now = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")

    if score < LOW:
        summary = "指数低于 25，已触发极度恐惧提醒。"
    elif score > HIGH:
        summary = "指数高于 75，已触发极度贪婪提醒。"
    elif is_test:
        summary = "这是手动测试邮件。当前指数没有达到日常提醒阈值。"
    else:
        summary = "当前指数处于常规区间。"

    periods = [
        ("昨日", "previous_close"),
        ("一周前", "previous_1_week"),
        ("一个月前", "previous_1_month"),
        ("一年前", "previous_1_year"),
    ]

    history_cells = ""
    for title, key in periods:
        value = fg.get(key)
        if value is None:
            display = "—"
            cell_color = "#999999"
        else:
            value = float(value)
            display = f"{value:.0f}"
            cell_color = zone_of(value)[0]

        history_cells += (
            '<td style="padding:12px 4px;text-align:center;width:25%">'
            f'<div style="font-size:12px;color:#777">{title}</div>'
            f'<div style="font-size:22px;font-weight:bold;color:{cell_color}">{display}</div>'
            '</td>'
        )

    rows = ""
    for key, title in INDICATORS:
        item = data.get(key) or {}
        value = item.get("score")
        if value is None:
            continue

        value = max(0, min(100, float(value)))
        item_color, _ = zone_of(value)
        rating = RATING_CN.get(str(item.get("rating", "")).lower(), "")

        rows += (
            '<tr>'
            f'<td style="padding:10px 5px;font-size:13px">{title}</td>'
            '<td style="padding:10px 5px;width:45%">'
            '<div style="height:8px;background:#eeeeee;border-radius:5px">'
            f'<div style="height:8px;width:{value:.1f}%;background:{item_color};border-radius:5px"></div>'
            '</div></td>'
            f'<td style="padding:10px 5px;text-align:right;color:{item_color};font-weight:bold">'
            f'{value:.0f} {rating}</td></tr>'
        )

    trend_block = ""
    if has_trend:
        trend_block = """
        <div style="padding:20px 24px 0">
          <h3 style="font-size:16px">近 90 天走势</h3>
          <img src="cid:trend" alt="指数历史走势"
               style="display:block;width:100%;max-width:552px">
          <p style="font-size:11px;color:#888">
            红色区域：低于 25；绿色区域：高于 75。
          </p>
        </div>
        """

    indicator_block = ""
    if rows:
        indicator_block = f"""
        <div style="padding:20px 24px 0">
          <h3 style="font-size:16px">七项子指标</h3>
          <table style="width:100%;border-collapse:collapse">{rows}</table>
        </div>
        """

    return f"""<!doctype html>
<html>
<body style="margin:0;padding:16px;background:#f0f2f5;font-family:Arial,'Microsoft YaHei',sans-serif">
<div style="max-width:620px;margin:auto;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid #e2e5e9">
  <div style="padding:22px 24px;background:{color};color:#ffffff">
    <div style="font-size:13px">FEAR &amp; GREED BRIEF</div>
    <h2 style="margin:10px 0;font-size:24px">CNN 恐惧与贪婪指数</h2>
    <div style="font-size:13px">{now}（北京时间）</div>
  </div>
  <div style="padding:18px 24px 0;text-align:center">
    <img src="cid:gauge" alt="指数仪表盘"
         style="width:100%;max-width:460px">
  </div>
  <div style="margin:12px 24px;padding:14px;background:#f6f7f9;border-left:4px solid {color};line-height:1.7">
    <b>当前指数：{score:.1f}/100 · {label}</b><br>
    {summary}
  </div>
  <div style="padding:10px 24px 0">
    <h3 style="font-size:16px">历史对比</h3>
    <table style="width:100%;border-collapse:collapse"><tr>{history_cells}</tr></table>
  </div>
  {trend_block}
  {indicator_block}
  <div style="padding:22px 24px;color:#888;font-size:11px;line-height:1.7">
    数据来源：CNN Fear &amp; Greed Index。<br>
    日常提醒条件：指数严格低于 {LOW} 或严格高于 {HIGH}。<br>
    {"本邮件为手动测试邮件。" if is_test else ""}
    本简报仅供参考，不构成投资建议。
  </div>
</div>
</body>
</html>"""


def send_mail(subject, plain_text, html, images):
    mail_user = os.environ["MAIL_USER"]
    mail_pass = os.environ["MAIL_PASS"]
    mail_to = os.environ["MAIL_TO"]

    message = MIMEMultipart("related")
    message["Subject"] = str(Header(subject, "utf-8"))
    message["From"] = mail_user
    message["To"] = mail_to

    alternative = MIMEMultipart("alternative")
    message.attach(alternative)
    alternative.attach(MIMEText(plain_text, "plain", "utf-8"))
    alternative.attach(MIMEText(html, "html", "utf-8"))

    for content_id, image_data in images.items():
        image = MIMEImage(image_data, _subtype="png")
        image.add_header("Content-ID", f"<{content_id}>")
        image.add_header(
            "Content-Disposition",
            "inline",
            filename=f"{content_id}.png",
        )
        message.attach(image)

    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    with smtplib.SMTP_SSL(smtp_host, 465, timeout=30) as smtp:
        smtp.login(mail_user, mail_pass)
        smtp.send_message(message)


def main():
    print("开始获取 CNN 恐惧与贪婪指数……")
    data, score = get_data()
    rounded_score = round(score, 1)
    color, label = zone_of(score)

    print(f"北京时间：{datetime.now(BEIJING):%Y-%m-%d %H:%M:%S}")
    print(f"当前指数：{rounded_score}（{label}）")

    triggered = score < LOW or score > HIGH

    if not triggered and not FORCE:
        print("未触发阈值：本次不发送邮件。")
        return

    history_data = (
        (data.get("fear_and_greed_historical") or {}).get("data") or []
    )

    images = {"gauge": gauge_png(score)}
    trend = trend_png(history_data)
    if trend:
        images["trend"] = trend

    if FORCE and not triggered:
        subject = f"[测试邮件] CNN 恐惧与贪婪指数 {rounded_score} - {label}"
    elif score < LOW:
        subject = f"⚠️ [市场提醒] CNN 指数低于 25：{rounded_score}"
    else:
        subject = f"🔥 [市场提醒] CNN 指数高于 75：{rounded_score}"

    plain = (
        f"CNN 恐惧与贪婪指数：{rounded_score}/100（{label}）\n"
        f"北京时间：{datetime.now(BEIJING):%Y-%m-%d %H:%M:%S}\n"
        f"提醒条件：严格低于 {LOW} 或严格高于 {HIGH}。\n"
        "请使用支持 HTML 的邮件客户端查看完整简报。"
    )

    html = build_html(data, score, "trend" in images, FORCE)
    send_mail(subject, plain, html, images)
    print("邮件发送成功。")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"运行失败：{type(error).__name__}: {error}")
        raise
