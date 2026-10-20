import os, smtplib, requests
from io import BytesIO
from math import cos, sin, radians
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

LOW, HIGH = 25, 75
FORCE = os.getenv("FORCE_SEND", "").lower() == "true"

URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
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
RATING_CN = {"extreme fear": "极度恐惧", "fear": "恐惧", "neutral": "中性",
             "greed": "贪婪", "extreme greed": "极度贪婪"}
INDICATORS = [
    ("market_momentum_sp500", "市场动量(标普500)"),
    ("stock_price_strength", "股价强度"),
    ("stock_price_breadth", "股价广度"),
    ("put_call_options", "看跌/看涨期权"),
    ("market_volatility_vix", "市场波动率(VIX)"),
    ("junk_bond_demand", "垃圾债需求"),
    ("safe_haven_demand", "避险需求"),
]


def zone_of(score):
    for lo, hi, color, name in ZONES:
        if score <= hi:
            return color, name
    return ZONES[-1][2], ZONES[-1][3]


def gauge_png(score):
    fig, ax = plt.subplots(figsize=(6, 3.4))
    for lo, hi, color, _ in ZONES:
        ax.add_patch(Wedge((0, 0), 1, 180 - hi * 1.8, 180 - lo * 1.8,
                           width=0.3, facecolor=color, edgecolor="white", linewidth=2))
    for v in (0, 25, 50, 75, 100):
        a = radians(180 - v * 1.8)
        ax.text(1.13 * cos(a), 1.13 * sin(a), str(v), ha="center", va="center",
                fontsize=10, color="#666")
    a = radians(180 - score * 1.8)
    ax.plot([0, 0.78 * cos(a)], [0, 0.78 * sin(a)], color="#222", linewidth=3.5,
            solid_capstyle="round")
    ax.add_patch(Circle((0, 0), 0.06, color="#222"))
    color, _ = zone_of(score)
    ax.text(0, -0.28, str(score), ha="center", va="center", fontsize=34,
            fontweight="bold", color=color)
    ax.set_xlim(-1.25, 1.25)
    ax.set_ylim(-0.5, 1.2)
    ax.set_aspect("equal")
    ax.axis("off")
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=160, facecolor="white", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def trend_png(hist):
    pts = hist[-90:]
    if len(pts) < 5:
        return None
    xs = [datetime.fromtimestamp(p["x"] / 1000, tz=timezone.utc) for p in pts]
    ys = [p["y"] for p in pts]
    fig, ax = plt.subplots(figsize=(6.4, 2.6))
    ax.axhspan(0, LOW, color="#d9534f", alpha=0.12)
    ax.axhspan(HIGH, 100, color="#2e7d32", alpha=0.12)
    ax.axhline(LOW, color="#d9534f", linestyle="--", linewidth=1)
    ax.axhline(HIGH, color="#2e7d32", linestyle="--", linewidth=1)
    ax.plot(xs, ys, color="#1a73e8", linewidth=2)
    ax.scatter([xs[-1]], [ys[-1]], color="#1a73e8", zorder=5)
    ax.set_ylim(0, 100)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.tick_params(labelsize=9, colors="#666")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="y", alpha=0.2)
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=160, facecolor="white", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def delta_html(now, old):
    if old is None:
        return ""
    diff = now - old
    arrow = "▲" if diff > 0 else "▼" if diff < 0 else "–"
    color = "#2e7d32" if diff > 0 else "#d9534f" if diff < 0 else "#888"
    return f'<div style="font-size:12px;color:{color};margin-top:2px">{arrow} {abs(diff):.0f}</div>'


def build_html(data, score, has_trend):
    fg = data["fear_and_greed"]
    color, zone_name = zone_of(score)
    now_cn = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")

    if score <= LOW:
        summary = "市场处于<b>极度恐惧</b>区间,投资者情绪极度悲观,已触发低位提醒。"
    elif score >= HIGH:
        summary = "市场处于<b>极度贪婪</b>区间,投资者情绪极度乐观,已触发高位提醒。"
    else:
        summary = f"当前指数处于「{zone_name}」区间,未触发提醒阈值,本邮件为测试发送。"

    periods = [("昨日收盘", "previous_close"), ("1 周前", "previous_1_week"),
               ("1 个月前", "previous_1_month"), ("1 年前", "previous_1_year")]
    cells = ""
    for label, key in periods:
        v = fg.get(key)
        if v is None:
            cells += f'<td style="width:25%;text-align:center;padding:10px 4px"><div style="font-size:12px;color:#888">{label}</div><div style="font-size:18px;color:#bbb">-</div></td>'
            continue
        v = round(v)
        c, n = zone_of(v)
        cells += (f'<td style="width:25%;text-align:center;padding:10px 4px">'
                  f'<div style="font-size:12px;color:#888">{label}</div>'
                  f'<div style="font-size:22px;font-weight:bold;color:{c}">{v}</div>'
                  f'<div style="font-size:12px;color:{c}">{n}</div>'
                  f'{delta_html(score, v)}</td>')

    rows = ""
    for key, name in INDICATORS:
        it = data.get(key) or {}
        s = it.get("score")
        if s is None:
            continue
        s = round(s)
        c, _ = zone_of(s)
        rt = RATING_CN.get((it.get("rating") or "").lower(), "")
        rows += (f'<tr><td style="padding:8px 0;font-size:13px;color:#333;width:38%">{name}</td>'
                 f'<td style="padding:8px 8px"><div style="background:#eee;border-radius:4px;height:8px">'
                 f'<div style="background:{c};width:{s}%;height:8px;border-radius:4px"></div></div></td>'
                 f'<td style="padding:8px 0;font-size:13px;text-align:right;width:22%;color:{c};font-weight:bold">{s} {rt}</td></tr>')

    trend_block = ""
    if has_trend:
        trend_block = ('<div style="padding:20px 24px 0"><div style="font-size:15px;font-weight:bold;color:#222;margin-bottom:8px">近 90 天走势</div>'
                       '<img src="cid:trend" style="width:100%;max-width:552px;display:block" alt="trend">'
                       '<div style="font-size:11px;color:#999;margin-top:4px">红色区域:极度恐惧(≤25)　绿色区域:极度贪婪(≥75)</div></div>')

    indicator_block = ""
    if rows:
        indicator_block = ('<div style="padding:20px 24px 0"><div style="font-size:15px;font-weight:bold;color:#222;margin-bottom:4px">七项子指标</div>'
                           f'<table style="width:100%;border-collapse:collapse">{rows}</table></div>')

    return f"""<html><body style="margin:0;padding:16px;background:#f2f4f7;font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',Arial,sans-serif">
<div style="max-width:600px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;border:1px solid #e3e6ea">
  <div style="background:{color};padding:22px 24px;color:#fff">
    <div style="font-size:13px;opacity:.9">美股 · CNN 恐惧与贪婪指数</div>
    <div style="font-size:26px;font-weight:bold;margin-top:4px">{zone_name}</div>
    <div style="font-size:12px;opacity:.85;margin-top:4px">{now_cn}(北京时间)</div>
  </div>
  <div style="padding:16px 24px 0;text-align:center">
    <img src="cid:gauge" style="width:100%;max-width:420px" alt="gauge">
  </div>
  <div style="margin:4px 24px 0;padding:12px 14px;background:#f7f8fa;border-left:4px solid {color};border-radius:4px;font-size:14px;color:#333;line-height:1.6">{summary}</div>
  <div style="padding:20px 24px 0"><div style="font-size:15px;font-weight:bold;color:#222">历史对比</div>
    <table style="width:100%;border-collapse:collapse;margin-top:4px"><tr>{cells}</tr></table></div>
  {trend_block}
  {indicator_block}
  <div style="padding:20px 24px 24px;font-size:11px;color:#999;line-height:1.6">
    数据来源:CNN Fear &amp; Greed Index。提醒阈值:≤{LOW} 或 ≥{HIGH}。<br>本邮件由脚本自动生成,仅供参考,不构成投资建议。
  </div>
</div></body></html>"""


def send_mail(subject, plain, html, images):
    msg = MIMEMultipart("related")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = os.environ["MAIL_USER"]
    msg["To"] = os.environ["MAIL_TO"]
    alt = MIMEMultipart("alternative")
    msg.attach(alt)
    alt.attach(MIMEText(plain, "plain", "utf-8"))
    alt.attach(MIMEText(html, "html", "utf-8"))
    for cid, data in images.items():
        img = MIMEImage(data, "png")
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
        msg.attach(img)
    with smtplib.SMTP_SSL(os.getenv("SMTP_HOST", "smtp.gmail.com"), 465) as s:
        s.login(os.environ["MAIL_USER"], os.environ["MAIL_PASS"])
        s.send_message(msg)


def main():
    r = requests.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    fg = data["fear_and_greed"]
    score = round(fg["score"])
    _, zone_name = zone_of(score)
    print(f"当前指数: {score} ({fg.get('rating')})")

    if not (score <= LOW or score >= HIGH or FORCE):
        print("未触发阈值,不发送邮件。")
        return

    images = {"gauge": gauge_png(score)}
    hist = (data.get("fear_and_greed_historical") or {}).get("data") or []
    trend = trend_png(hist)
    if trend:
        images["trend"] = trend

    emoji = "⚠️" if score <= LOW else "🔥" if score >= HIGH else "📊"
    subject = f"{emoji} 美股恐惧与贪婪指数 {score}|{zone_name}"
    plain = f"CNN 恐惧与贪婪指数:{score}({zone_name})。请使用支持 HTML 的邮件客户端查看完整简报。"
    send_mail(subject, plain, build_html(data, score, "trend" in images), images)
    print("邮件已发送")


main()
