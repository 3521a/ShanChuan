import os
import smtplib
import logging
from io import BytesIO
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.utils import formatdate

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Wedge, Circle

# ==================== 基础配置 ====================
LOW, HIGH = 25, 75
BEIJING = timezone(timedelta(hours=8))
FORCE = os.getenv("FORCE_SEND", "false").lower() == "true"

URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://edition.cnn.com/markets/fear-and-greed",
    "Origin": "https://edition.cnn.com",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ZONES = [
    (0, 25, "#d94841", "极度恐惧"),
    (25, 45, "#ed9f43", "恐惧"),
    (45, 55, "#87919c", "中性"),
    (55, 75, "#70a96b", "贪婪"),
    (75, 100, "#21845b", "极度贪婪"),
]

RATING_CN = {
    "extreme fear": "极度恐惧",
    "fear": "恐惧",
    "neutral": "中性",
    "greed": "贪婪",
    "extreme greed": "极度贪婪",
}

INDICATORS = [
    ("market_momentum_sp500", "市场动能"),
    ("stock_price_strength", "股价强度"),
    ("stock_price_breadth", "股价广度"),
    ("put_call_options", "看跌 / 看涨期权"),
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
    if not isinstance(fg, dict) or fg.get("score") is None:
        raise ValueError("CNN 返回的数据中没有 fear_and_greed.score")

    score = float(fg["score"])
    if not 0 <= score <= 100:
        raise ValueError(f"指数超出合理范围：{score}")

    return data, score


def gauge_png(score):
    fig, ax = plt.subplots(figsize=(7, 4))
    fig.patch.set_facecolor("#15191f")
    ax.set_facecolor("#15191f")

    for lo, hi, color, _ in ZONES:
        ax.add_patch(Wedge(
            (0, 0), 1, 180 - hi * 1.8, 180 - lo * 1.8,
            width=0.22, facecolor=color, edgecolor="#15191f", linewidth=2
        ))

    for v in (0, 25, 50, 75, 100):
        import math
        a = math.radians(180 - v * 1.8)
        ax.text(1.13 * math.cos(a), 1.13 * math.sin(a), str(v),
                ha="center", va="center", color="#aab2bd", fontsize=10)

    import math
    a = math.radians(180 - score * 1.8)
    ax.plot([0, 0.72 * math.cos(a)], [0, 0.72 * math.sin(a)],
            color="#ffffff", linewidth=4, solid_capstyle="round")
    ax.add_patch(Circle((0, 0), 0.055, color="#ffffff"))

    color, name = zone_of(score)
    ax.text(0, -0.25, f"{score:.1f}", ha="center", va="center",
            fontsize=35, fontweight="bold", color="#ffffff")
    ax.text(0, -0.49, name, ha="center", va="center",
            fontsize=15, fontweight="bold", color=color)

    ax.set_xlim(-1.25, 1.25)
    ax.set_ylim(-0.65, 1.25)
    ax.set_aspect("equal")
    ax.axis("off")

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=160, facecolor=fig.get_facecolor(),
                bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def trend_png(data):
    hist = (data.get("fear_and_greed_historical") or {}).get("data") or []
    points = []

    for item in hist:
        try:
            x = float(item["x"])
            y = float(item["y"])
            if 0 <= y <= 100:
                points.append((datetime.fromtimestamp(x / 1000, timezone.utc), y))
        except (KeyError, TypeError, ValueError, OSError):
            continue

    points.sort(key=lambda p: p[0])
    points = points[-365:]

    if len(points) < 2:
        return None

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]

    fig, ax = plt.subplots(figsize=(8, 3.2))
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#ffffff")

    ax.axhspan(0, LOW, color="#d94841", alpha=0.10)
    ax.axhspan(HIGH, 100, color="#21845b", alpha=0.10)
    ax.axhline(LOW, color="#d94841", linestyle="--", linewidth=1)
    ax.axhline(HIGH, color="#21845b", linestyle="--", linewidth=1)
    ax.plot(xs, ys, color="#2463eb", linewidth=2)
    ax.scatter([xs[-1]], [ys[-1]], color="#2463eb", s=25, zorder=3)

    ax.set_ylim(0, 100)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m", tz=timezone.utc))
    ax.tick_params(labelsize=8, colors="#6b7280")
    ax.grid(axis="y", alpha=0.18)

    for side in ("top", "right"):
        ax.spines[side].set_visible(False)

    fig.autofmt_xdate()
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=160, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def delta_html(current, previous):
    if previous is None:
        return ""
    diff = current - previous
    arrow = "▲" if diff > 0 else "▼" if diff < 0 else "—"
    color = "#21845b" if diff > 0 else "#d94841" if diff < 0 else "#87919c"
    return (
        f'<div style="font-size:12px;color:{color};margin-top:3px">'
        f'{arrow} {abs(diff):.1f}</div>'
    )


def build_html(data, score, images):
    fg = data["fear_and_greed"]
    color, zone_name = zone_of(score)
    now = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M")
    rating = RATING_CN.get(str(fg.get("rating", "")).lower(), zone_name)

    if score <= LOW:
        status = "低位情绪提醒已触发"
    elif score >= HIGH:
        status = "高位情绪提醒已触发"
    else:
        status = "手动测试邮件"

    periods = [
        ("昨日", "previous_close"),
        ("一周前", "previous_1_week"),
        ("一个月前", "previous_1_month"),
        ("一年前", "previous_1_year"),
    ]

    cells = ""
    for label, key in periods:
        value = fg.get(key)
        if value is None:
            value_html = '<div style="font-size:23px;color:#aab2bd">—</div>'
        else:
            value = float(value)
            pc, pn = zone_of(value)
            value_html = (
                f'<div style="font-size:23px;font-weight:bold;color:{pc}">{value:.0f}</div>'
                f'<div style="font-size:11px;color:{pc}">{pn}</div>'
                f'{delta_html(score, value)}'
            )
        cells += (
            f'<td style="width:25%;text-align:center;padding:12px 3px;'
            f'border-right:1px solid #edf0f3">'
            f'<div style="font-size:12px;color:#7b8490">{label}</div>'
            f'{value_html}</td>'
        )

    rows = ""
    for key, label in INDICATORS:
        item = data.get(key) or {}
        value = item.get("score")
        if value is None:
            continue
        try:
            value = max(0, min(100, float(value)))
        except (ValueError, TypeError):
            continue

        pc, pn = zone_of(value)
        rating_cn = RATING_CN.get(str(item.get("rating", "")).lower(), pn)
        rows += (
            f'<tr><td style="padding:12px 6px 12px 0;font-size:13px;'
            f'color:#28313c;border-bottom:1px solid #edf0f3">{label}</td>'
            f'<td style="padding:12px 8px;border-bottom:1px solid #edf0f3">'
            f'<div style="background:#edf0f3;height:8px;border-radius:5px">'
            f'<div style="background:{pc};width:{value}%;height:8px;border-radius:5px"></div>'
            f'</div></td>'
            f'<td style="padding:12px 0;text-align:right;border-bottom:1px solid #edf0f3;'
            f'white-space:nowrap;color:{pc};font-weight:bold;font-size:13px">'
            f'{value:.0f} {rating_cn}</td></tr>'
        )

    trend_block = ""
    if "trend" in images:
        trend_block = (
            '<div style="padding:22px 24px 0">'
            '<div style="font-size:16px;font-weight:bold;margin-bottom:10px;color:#202833">'
            '近一年历史走势</div>'
            '<img src="cid:trend" alt="指数走势" '
            'style="display:block;width:100%;max-width:540px;height:auto">'
            '<div style="font-size:11px;color:#7b8490;margin-top:7px">'
            '红色虚线：25；绿色虚线：75</div></div>'
        )

    indicators_block = ""
    if rows:
        indicators_block = (
            '<div style="padding:22px 24px 0">'
            '<div style="font-size:16px;font-weight:bold;margin-bottom:6px;color:#202833">'
            '市场情绪子指标</div>'
            '<table style="width:100%;border-collapse:collapse">'
            f'{rows}</table></div>'
        )

    summary = (
        "市场情绪处于低位，建议关注波动和风险管理。"
        if score <= LOW else
        "市场情绪处于高位，注意避免盲目追涨。"
        if score >= HIGH else
        "当前指数未触发自动提醒阈值，本邮件用于手动测试。"
    )

    return f"""<!doctype html>
<html><body style="margin:0;padding:16px;background:#eef1f5;font-family:Arial,'Microsoft YaHei',sans-serif">
<div style="max-width:620px;margin:0 auto;background:#fff;border:1px solid #e1e5eb;border-radius:16px;overflow:hidden">
  <div style="background:#15191f;padding:22px 24px;color:#fff">
    <div style="font-size:12px;letter-spacing:2px;color:#c2c9d2">FEAR &amp; GREED BRIEF</div>
    <div style="font-size:14px;margin-top:18px;color:#c2c9d2">美股 · CNN 恐惧与贪婪指数</div>
    <div style="font-size:30px;font-weight:bold;margin-top:6px">{score:.1f}<span style="font-size:15px;color:{color};margin-left:12px">{zone_name}</span></div>
    <div style="font-size:12px;color:#c2c9d2;margin-top:7px">{now} 北京时间 · {status}</div>
  </div>
  <div style="padding:18px 24px 0;text-align:center;background:#15191f">
    <img src="cid:gauge" alt="指数仪表盘" style="width:100%;max-width:420px;height:auto">
  </div>
  <div style="padding:18px 24px;background:#fff">
    <div style="padding:13px 15px;background:#f5f7fa;border-left:4px solid {color};border-radius:5px;color:#28313c;font-size:14px;line-height:1.7">
      <b>{rating}</b> · {summary}
    </div>
    <div style="font-size:16px;font-weight:bold;margin-top:22px;color:#202833">历史对比</div>
    <table style="width:100%;border-collapse:collapse;margin-top:8px"><tr>{cells}</tr></table>
  </div>
  {trend_block}
  {indicators_block}
  <div style="padding:24px;color:#87919c;font-size:11px;line-height:1.8">
    数据来源：CNN Fear &amp; Greed Index<br>
    自动提醒阈值：指数 ≤ {LOW} 或 ≥ {HIGH}。<br>
    本报告为市场情绪参考，不构成投资建议。指数不代表未来市场走势。
  </div>
</div></body></html>"""


def send_mail(subject, plain, html, images):
    user = os.environ["MAIL_USER"]
    password = os.environ["MAIL_PASS"]
    recipient = os.environ["MAIL_TO"]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")

    html_part = msg.get_payload()[-1]
    for cid, image_data in images.items():
        html_part.add_related(
            image_data,
            maintype="image",
            subtype="png",
            cid=f"<{cid}>",
            filename=f"{cid}.png",
            disposition="inline",
        )

    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    with smtplib.SMTP_SSL(host, 465, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)

    logging.info("邮件发送成功：%s", recipient)


def main():
    if FORCE:
        logging.warning("当前为强制发送模式：即使未触发阈值也会发送邮件")

    data, score = get_data()
    color, zone_name = zone_of(score)
    logging.info("CNN 指数：%.1f；区间：%s", score, zone_name)

    triggered = score <= LOW or score >= HIGH
    if not triggered and not FORCE:
        logging.info("指数未触发阈值，不发送邮件")
        return

    images = {"gauge": gauge_png(score)}
    trend = trend_png(data)
    if trend:
        images["trend"] = trend

    if score <= LOW:
        subject = f"⚠️ CNN 恐惧与贪婪指数低位提醒：{score:.1f}"
    elif score >= HIGH:
        subject = f"🔥 CNN 恐惧与贪婪指数高位提醒：{score:.1f}"
    else:
        subject = f"[测试] CNN 恐惧与贪婪指数简报：{score:.1f}"

    plain = (
        f"CNN 恐惧与贪婪指数：{score:.1f}/100\n"
        f"情绪区间：{zone_name}\n"
        f"检查时间：{datetime.now(BEIJING):%Y-%m-%d %H:%M} 北京时间\n"
        f"提醒阈值：≤{LOW} 或 ≥{HIGH}\n"
        "本邮件仅供市场情绪参考，不构成投资建议。"
    )

    html = build_html(data, score, images)
    send_mail(subject, plain, html, images)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("程序运行失败")
        raise
```
