import os
import smtplib
import logging
from io import BytesIO
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.utils import formataddr

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Wedge, Circle

LOW, HIGH = 25, 75
BEIJING = timezone(timedelta(hours=8))
FORCE = os.getenv("FORCE_SEND", "false").lower() == "true"

URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://edition.cnn.com/markets/fear-and-greed",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ZONES = [
    (0, 25, "#d94f45", "极度恐惧"),
    (25, 45, "#efa83d", "恐惧"),
    (45, 55, "#9299a3", "中性"),
    (55, 75, "#79ad67", "贪婪"),
    (75, 100, "#24865a", "极度贪婪"),
]

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


def fetch_data():
    response = requests.get(URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    data = response.json()

    fg = data.get("fear_and_greed")
    if not isinstance(fg, dict) or fg.get("score") is None:
        raise ValueError("CNN 返回的数据中缺少 fear_and_greed.score")

    score = float(fg["score"])
    if not 0 <= score <= 100:
        raise ValueError(f"指数超出有效范围：{score}")

    return data, score


def gauge_png(score):
    fig, ax = plt.subplots(figsize=(7, 4))
    for lo, hi, color, _ in ZONES:
        ax.add_patch(Wedge(
            (0, 0), 1, 180 - hi * 1.8, 180 - lo * 1.8,
            width=0.25, facecolor=color, edgecolor="white", linewidth=2
        ))

    for v in (0, 25, 50, 75, 100):
        import math
        a = math.radians(180 - v * 1.8)
        ax.text(1.12 * math.cos(a), 1.12 * math.sin(a), str(v),
                ha="center", va="center", color="#666", fontsize=10)

    import math
    a = math.radians(180 - score * 1.8)
    ax.plot([0, 0.72 * math.cos(a)], [0, 0.72 * math.sin(a)],
            color="#222", linewidth=4, solid_capstyle="round")
    ax.add_patch(Circle((0, 0), 0.055, color="#222"))

    color, name = zone_of(score)
    ax.text(0, -0.28, f"{score:.1f}", ha="center", va="center",
            fontsize=32, fontweight="bold", color=color)
    ax.text(0, -0.47, name, ha="center", va="center",
            fontsize=13, color="#444")

    ax.set_xlim(-1.25, 1.25)
    ax.set_ylim(-0.58, 1.25)
    ax.set_aspect("equal")
    ax.axis("off")

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=160, facecolor="white",
                bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def trend_png(data):
    hist = (data.get("fear_and_greed_historical") or {}).get("data") or []
    points = []

    for item in hist:
        try:
            x = datetime.fromtimestamp(float(item["x"]) / 1000, tz=timezone.utc)
            y = float(item["y"])
            if 0 <= y <= 100:
                points.append((x, y))
        except (KeyError, TypeError, ValueError, OSError):
            continue

    points = sorted(points, key=lambda p: p[0])[-90:]
    if len(points) < 2:
        return None

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]

    fig, ax = plt.subplots(figsize=(8, 3.2))
    ax.axhspan(0, LOW, color="#d9534f", alpha=0.12)
    ax.axhspan(HIGH, 100, color="#2e7d32", alpha=0.12)
    ax.axhline(LOW, color="#d9534f", linestyle="--", linewidth=1)
    ax.axhline(HIGH, color="#2e7d32", linestyle="--", linewidth=1)
    ax.plot(xs, ys, color="#1769e0", linewidth=2)
    ax.scatter([xs[-1]], [ys[-1]], color="#1769e0", s=25, zorder=3)

    ax.set_ylim(0, 100)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.tick_params(labelsize=9, colors="#666")
    ax.grid(axis="y", alpha=0.2)

    for side in ("top", "right"):
        ax.spines[side].set_visible(False)

    fig.autofmt_xdate()
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor="white",
                bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def build_html(data, score, gauge_available, trend_available):
    fg = data["fear_and_greed"]
    color, name = zone_of(score)
    now = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")

    if score < LOW:
        summary = "指数低于 25，已触发极度恐惧提醒。"
    elif score > HIGH:
        summary = "指数高于 75，已触发极度贪婪提醒。"
    else:
        summary = "当前指数未触发自动提醒阈值，本邮件由手动测试触发。"

    periods = [
        ("昨日", "previous_close"),
        ("一周前", "previous_1_week"),
        ("一个月前", "previous_1_month"),
        ("一年前", "previous_1_year"),
    ]

    history_cells = ""
    for label, key in periods:
        value = fg.get(key)
        try:
            value = float(value)
            history_cells += (
                f"<td style='padding:12px;text-align:center'>"
                f"<div style='color:#777;font-size:12px'>{label}</div>"
                f"<b style='font-size:22px'>{value:.0f}</b></td>"
            )
        except (TypeError, ValueError):
            history_cells += (
                f"<td style='padding:12px;text-align:center;color:#999'>"
                f"<div style='font-size:12px'>{label}</div>暂无</td>"
            )

    rows = ""
    for key, label in INDICATORS:
        item = data.get(key) or {}
        try:
            value = float(item["score"])
        except (KeyError, TypeError, ValueError):
            continue

        if not 0 <= value <= 100:
            continue

        bar_color, _ = zone_of(value)
        rating = str(item.get("rating") or "")
        rows += f"""
        <tr>
          <td style="padding:11px 5px;font-size:13px">{label}</td>
          <td style="padding:11px 5px">
            <div style="background:#edf0f4;height:8px;border-radius:8px">
              <div style="background:{bar_color};width:{value:.1f}%;height:8px;border-radius:8px"></div>
            </div>
          </td>
          <td style="padding:11px 5px;text-align:right;font-weight:bold;color:{bar_color}">
            {value:.0f}
          </td>
        </tr>
        """

    gauge_block = (
        '<img src="cid:gauge" style="width:100%;max-width:500px" alt="指数仪表盘">'
        if gauge_available else ""
    )
    trend_block = (
        '<h3>近期走势</h3><img src="cid:trend" style="width:100%;max-width:600px" alt="指数历史走势">'
        if trend_available else ""
    )

    return f"""<!doctype html>
<html><body style="margin:0;background:#edf0f4;padding:18px;font-family:Arial,'Microsoft YaHei',sans-serif;color:#20242a">
<div style="max-width:650px;margin:auto;background:#fff;border-radius:16px;overflow:hidden">
  <div style="background:#15191f;color:#fff;padding:24px">
    <div style="font-size:12px;letter-spacing:2px;color:#c1c7d0">FEAR &amp; GREED BRIEF</div>
    <h2 style="margin:18px 0 6px">CNN 美股恐惧与贪婪指数</h2>
    <div style="font-size:13px;color:#c1c7d0">{now}（北京时间）</div>
  </div>
  <div style="padding:22px">
    <div style="background:{color};color:white;padding:12px 16px;border-radius:9px">
      <b style="font-size:21px">{score:.1f} / 100　{name}</b>
    </div>
