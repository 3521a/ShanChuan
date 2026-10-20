import os
import html
import math
import logging
import smtplib
import ssl
from io import BytesIO
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Wedge, Circle
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ========= 基本设置 =========
LOW = 25
HIGH = 75
URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
BEIJING = ZoneInfo("Asia/Shanghai")
FORCE = os.getenv("FORCE_SEND", "").lower() == "true"
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("fng-alert")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
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

INDICATORS = [
    ("market_momentum_sp500", "市场动量（标普500）"),
    ("stock_price_strength", "股价强度"),
    ("stock_price_breadth", "股价广度"),
    ("put_call_options", "看跌/看涨期权"),
    ("market_volatility_vix", "市场波动率（VIX）"),
    ("junk_bond_demand", "垃圾债需求"),
    ("safe_haven_demand", "避险需求"),
]

RATINGS = {
    "extreme fear": "极度恐惧",
    "fear": "恐惧",
    "neutral": "中性",
    "greed": "贪婪",
    "extreme greed": "极度贪婪",
}


def zone(score):
    for lo, hi, color, name in ZONES:
        if score <= hi:
            return color, name
    return ZONES[-1][2], ZONES[-1][3]


def fetch_data():
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(HEADERS)

    try:
        response = session.get(URL, timeout=25)
        response.raise_for_status()
        data = response.json()
    finally:
        session.close()

    if not isinstance(data, dict):
        raise ValueError("CNN 返回的数据格式不正确")

    fg = data.get("fear_and_greed")
    if not isinstance(fg, dict) or fg.get("score") is None:
        raise ValueError("CNN 数据中找不到恐惧与贪婪指数")

    score = float(fg["score"])
    if not math.isfinite(score) or not 0 <= score <= 100:
        raise ValueError(f"指数值异常：{score}")

    return data, score


def gauge_png(score):
    fig, ax = plt.subplots(figsize=(6, 3.5))
    fig.patch.set_facecolor("white")

    for lo, hi, color, _ in ZONES:
        ax.add_patch(Wedge(
            (0, 0), 1,
            180 - hi * 1.8, 180 - lo * 1.8,
            width=0.28,
            facecolor=color,
            edgecolor="white",
            linewidth=2,
        ))

    for value in (0, 25, 50, 75, 100):
        angle = math.radians(180 - value * 1.8)
        ax.text(
            1.13 * math.cos(angle),
            1.13 * math.sin(angle),
            str(value),
            ha="center",
            va="center",
            fontsize=9,
            color="#666666",
        )

    angle = math.radians(180 - score * 1.8)
    ax.plot(
        [0, 0.75 * math.cos(angle)],
        [0, 0.75 * math.sin(angle)],
        color="#222222",
        linewidth=3,
        solid_capstyle="round",
    )
    ax.add_patch(Circle((0, 0), 0.055, color="#222222"))

    color, _ = zone(score)
    ax.text(
        0, -0.27, str(round(score)),
        ha="center", va="center",
        fontsize=32, fontweight="bold", color=color,
    )

    ax.set_xlim(-1.25, 1.25)
    ax.set_ylim(-0.45, 1.2)
    ax.set_aspect("equal")
    ax.axis("off")

    buffer = BytesIO()
    fig.savefig(
        buffer, format="png", dpi=150,
        facecolor="white", bbox_inches="tight",
    )
    plt.close(fig)
    return buffer.getvalue()


def trend_png(data):
    history = (
        (data.get("fear_and_greed_historical") or {}).get("data") or []
    )
    points = []

    for item in history:
        try:
            timestamp = float(item["x"]) / 1000
            value = float(item["y"])
            if 0 <= value <= 100:
                dt = datetime.fromtimestamp(
                    timestamp, tz=timezone.utc
                ).astimezone(BEIJING)
                points.append((dt, value))
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            continue

    # 按时间排序并去重
    points = sorted(dict(points).items(), key=lambda p: p[0])
    points = points[-90:]

    if len(points) < 5:
        log.warning("历史数据不足，跳过趋势图")
        return None

    dates = [p[0] for p in points]
    values = [p[1] for p in points]

    fig, ax = plt.subplots(figsize=(6.4, 2.6))
    fig.patch.set_facecolor("white")

    ax.axhspan(0, LOW, color="#d9534f", alpha=0.12)
    ax.axhspan(HIGH, 100, color="#2e7d32", alpha=0.12)
    ax.axhline(LOW, color="#d9534f", linestyle="--", linewidth=1)
    ax.axhline(HIGH, color="#2e7d32", linestyle="--", linewidth=1)
    ax.plot(dates, values, color="#1a73e8", linewidth=2)
    ax.scatter([dates[-1]], [values[-1]], color="#1a73e8", s=18)

    ax.set_ylim(0, 100)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.xaxis.set_major_locator(
        mdates.AutoDateLocator(minticks=3, maxticks=6)
    )
    ax.xaxis.set_major_formatter(
        mdates.DateFormatter("%m-%d", tz=BEIJING)
    )
    ax.tick_params(labelsize=8, colors="#666666")
    ax.grid(axis="y", alpha=0.2)

    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)

    fig.tight_layout()
    buffer = BytesIO()
    fig.savefig(
        buffer, format="png", dpi=150,
        facecolor="white", bbox_inches="tight",
    )
    plt.close(fig)
    return buffer.getvalue()


def build_html(data, score, images, forced, triggered, now):
    fg = data["fear_and_greed"]
    color, name = zone(score)
    score_int = round(score)
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")

    if triggered and score < LOW:
        summary = "指数低于25，触发低位恐惧提醒。"
    elif triggered and score > HIGH:
        summary = "指数高于75，触发高位贪婪提醒。"
    elif forced:
        summary = "当前指数未触发正式阈值；本邮件由手动测试触发。"
    else:
        summary = "当前指数未触发提醒阈值。"

    periods = [
        ("昨日收盘", "previous_close"),
        ("1周前", "previous_1_week"),
        ("1个月前", "previous_1_month"),
        ("1年前", "previous_1_year"),
    ]

    cells = ""
    for label, key in periods:
        try:
            old = round(float(fg[key]))
            if not 0 <= old <= 100:
                raise ValueError()
            old_color, old_name = zone(old)
            diff = score_int - old
            arrow = "▲" if diff > 0 else "▼" if diff < 0 else "—"
            cells += (
                '<td style="text-align:center;padding:10px 3px;width:25%">'
                f'<div style="font-size:12px;color:#777">{label}</div>'
                f'<div style="font-size:22px;font-weight:bold;color:{old_color}">{old}</div>'
                f'<div style="font-size:11px;color:{old_color}">{old_name}</div>'
                f'<div style="font-size:11px;color:#777">{arrow} {abs(diff)}</div>'
                '</td>'
            )
        except (KeyError, TypeError, ValueError):
            cells += (
                '<td style="text-align:center;padding:10px 3px;width:25%">'
                f'<div style="font-size:12px;color:#777">{label}</div>—</td>'
            )

    rows = ""
    for key, label in INDICATORS:
        item = data.get(key) or {}
        if not isinstance(item, dict):
            continue

        try:
            value = round(float(item["score"]))
            if not 0 <= value <= 100:
                continue
        except (KeyError, TypeError, ValueError):
            continue

        c, _ = zone(value)
        rating = RATINGS.get(str(item.get("rating", "")).lower(), "")
        rows += (
            '<tr style="border-bottom:1px solid #eeeeee">'
            f'<td style="padding:10px 5px 10px 0;font-size:13px">{label}</td>'
            '<td style="padding:10px 6px">'
            '<div style="background:#eeeeee;height:8px;border-radius:4px">'
            f'<div style="width:{value}%;height:8px;border-radius:4px;background:{c}"></div>'
            '</div></td>'
            f'<td style="text-align:right;white-space:nowrap;color:{c};font-weight:bold">'
            f'{value} {rating}</td></tr>'
        )

    trend = ""
    if "trend" in images:
        trend = (
            '<h3>近90个历史数据点走势</h3>'
            '<img src="cid:trend" style="width:100%;max-width:550px" alt="指数走势">'
            '<p style="font-size:11px;color:#777">红色区域：低位；绿色区域：高位。</p>'
        )

    status = "已触发提醒" if triggered else (
        "手动测试邮件" if forced else "未触发提醒"
    )

    safe_rating = html.escape(str(fg.get("rating") or name))
    return f"""<!doctype html>
<html lang="zh-CN">
<body style="margin:0;padding:16px;background:#f1f3f6;font-family:Arial,'Microsoft YaHei',sans-serif">
<div style="max-width:620px;margin:auto;background:white;border-radius:14px;overflow:hidden">
  <div style="background:{color};padding:22px 24px;color:white">
    <div style="font-size:13px">美股 · CNN 恐惧与贪婪指数</div>
    <div style="font-size:27px;font-weight:bold;margin-top:5px">{name}</div>
    <div style="font-size:12px;margin-top:5px">{now_text}（北京时间）</div>
  </div>
  <div style="padding:12px 20px;text-align:center">
    <img src="cid:gauge" style="width:100%;max-width:420px" alt="指数仪表盘">
  </div>
  <div style="margin:0 22px;padding:14px;background:#f5f6f8;border-left:4px solid {color};line-height:1.7">
    <b>{status}</b><br>{summary}<br>
    <span style="font-size:12px;color:#777">CNN原始评级：{safe_rating}</span>
  </div>
  <div style="padding:20px 22px">
    <h3>历史对比</h3>
    <table style="width:100%;border-collapse:collapse"><tr>{cells}</tr></table>
    {trend}
    <h3>七项子指标</h3>
    <table style="width:100%;border-collapse:collapse">{rows}</table>
  </div>
  <div style="padding:0 22px 22px;font-size:11px;color:#888;line-height:1.7">
    数据来源：CNN Fear &amp; Greed Index。<br>
    正式提醒规则：指数严格小于25或严格大于75。<br>
    缺失数据不会使用虚构数值填充。本邮件仅供参考，不构成投资建议。
  </div>
</div>
</body></html>"""


def send_mail(subject, plain, html_body, images):
    user = os.getenv("MAIL_USER", "").strip()
    password = os.getenv("MAIL_PASS", "").strip()
    recipient = os.getenv("MAIL_TO", "").strip()

    missing = [
        key for key, value in [
            ("MAIL_USER", user),
            ("MAIL_PASS", password),
            ("MAIL_TO", recipient),
        ] if not value
    ]
    if missing:
        raise RuntimeError("缺少 GitHub Secrets：" + ", ".join(missing))

    msg = MIMEMultipart("related")
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"] = user
    msg["To"] = recipient

    alt = MIMEMultipart("alternative")
    msg.attach(alt)
    alt.attach(MIMEText(plain, "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html", "utf-8"))

    for cid, image_bytes in images.items():
        part = MIMEImage(image_bytes, _subtype="png")
        part.add_header("Content-ID", f"<{cid}>")
        part.add_header(
            "Content-Disposition", "inline", filename=f"{cid}.png"
        )
        msg.attach(part)

    with smtplib.SMTP_SSL(
        SMTP_HOST, SMTP_PORT,
        context=ssl.create_default_context(),
        timeout=30,
    ) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)


def main():
    now = datetime.now(BEIJING)
    log.info("开始检查指数，北京时间：%s", now.strftime("%Y-%m-%d %H:%M:%S"))

    data, score = fetch_data()
    color, name = zone(score)

    # 必须按未四舍五入的原始指数判断阈值
    triggered = score < LOW or score > HIGH

    log.info("CNN指数：%.2f；区间：%s", score, name)
    log.info("提醒触发：%s；手动强制发送：%s", triggered, FORCE)

    if not triggered and not FORCE:
        log.info("指数没有严格低于25或高于75，不发送邮件。")
        return

    images = {"gauge": gauge_png(score)}
    trend = trend_png(data)
    if trend is not None:
        images["trend"] = trend

    if score < LOW:
        emoji = "⚠️"
        status = "低位恐惧提醒"
    elif score > HIGH:
        emoji = "🔥"
        status = "高位贪婪提醒"
    else:
        emoji = "🧪"
        status = "手动测试"

    subject = f"{emoji} CNN美股情绪指数 {round(score)}/100｜{name}"
    plain = (
        f"CNN恐惧与贪婪指数：{score:.2f}/100（{name}）\n"
        f"状态：{status}\n"
        f"检查时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）\n"
        f"提醒规则：指数严格小于{LOW}或严格大于{HIGH}\n"
        f"数据来源：{URL}\n\n"
        "本邮件仅供市场情绪参考，不构成投资建议。"
    )

    html_body = build_html(
        data, score, images, FORCE and not triggered, triggered, now
    )
    send_mail(subject, plain, html_body, images)
    log.info("邮件发送成功：%s", status)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("任务执行失败")
        raise
