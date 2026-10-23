import os
import time
import json
import logging
from datetime import datetime, timezone, timedelta

import requests
import smtplib
import ssl
from email.message import EmailMessage
from email.header import Header
from html import escape

# =========================================================
# 配置
# =========================================================

LOW = 25
HIGH = 75
BEIJING = timezone(timedelta(hours=8))
FORCE = os.getenv("FORCE_SEND", "false").lower() == "true"

CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

# 关键修正：使用 Macintosh UA，避免 CNN 对 Windows UA 返回 418
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
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
# 工具函数
# =========================================================

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


def rating_cn(rating):
    mapping = {
        "extreme fear": "极度恐惧",
        "fear": "恐惧",
        "neutral": "中性",
        "greed": "贪婪",
        "extreme greed": "极度贪婪",
    }
    return mapping.get(str(rating).strip().lower(), rating or "暂无评级")


# =========================================================
# 获取 CNN 指数
# =========================================================

def get_cnn_data():
    last_error = None

    for attempt in range(3):
        try:
            resp = requests.get(CNN_URL, headers=HEADERS, timeout=25)
            resp.raise_for_status()
            data = resp.json()

            fg = data.get("fear_and_greed") or {}
            score = valid_score(fg.get("score"))

            if score is None:
                raise ValueError("CNN 响应中没有有效指数")

            logging.info("CNN 指数：%.2f（%s）", score, fg.get("rating"))
            return data, score

        except Exception as exc:
            last_error = exc
            logging.warning("CNN 请求第 %s/3 次失败：%s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(2 ** attempt)

    raise RuntimeError("无法获取 CNN 指数") from last_error


# =========================================================
# 生成邮件
# =========================================================

def build_email(data, score):
    now = datetime.now(BEIJING)
    fg = data.get("fear_and_greed") or {}
    label, color = zone(score)

    history = []
    for title, key in [
        ("昨日收盘", "previous_close"),
        ("一周前", "previous_1_week"),
        ("一个月前", "previous_1_month"),
        ("一年前", "previous_1_year"),
    ]:
        v = valid_score(fg.get(key))
        if v is not None:
            r, c = zone(v)
            history.append(f"{title}：{v:.1f}（{r}）")

    history_text = "；".join(history) if history else "CNN 未提供历史对比值"

    plain = f"""美股市场情绪提醒
生成时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）

CNN 恐惧与贪婪指数：{score:.1f}/100（{label}）

历史对比：{history_text}

触发条件：指数严格低于 {LOW} 或严格高于 {HIGH}。
本邮件仅供参考，不构成投资建议。
"""

    html = f"""<!doctype html>
<html><body style="font-family:Arial,'Microsoft YaHei',sans-serif;
padding:24px;color:#202832">
<h2>美股市场情绪提醒</h2>
<p>生成时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）</p>
<p style="font-size:28px;font-weight:700;color:{color}">
CNN 恐惧与贪婪指数：{score:.1f}/100
<span style="font-size:16px">（{label}）</span>
</p>
<p style="color:#667085">{history_text}</p>
<p style="font-size:12px;color:#667085;margin-top:24px">
触发条件：指数严格低于 {LOW} 或严格高于 {HIGH}。<br>
本邮件仅供参考，不构成投资建议。
</p>
</body></html>"""

    return plain, html


# =========================================================
# 发送邮件
# =========================================================

def send_email(subject, plain, html):
    username = os.environ.get("MAIL_USER")
    password = os.environ.get("MAIL_PASS")
    recipient = os.environ.get("MAIL_TO")

    if not username or not password or not recipient:
        raise RuntimeError("邮件配置不完整")

    message = EmailMessage()
    message["Subject"] = str(Header(subject, "utf-8"))
    message["From"] = username
    message["To"] = recipient
    message.set_content(plain)
    message.add_alternative(html, subtype="html")

    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))

    with smtplib.SMTP_SSL(host, port, timeout=30,
                          context=ssl.create_default_context()) as server:
        server.login(username, password)
        server.send_message(message)

    logging.info("邮件发送成功：%s", recipient)


# =========================================================
# 主程序
# =========================================================

def main():
    now = datetime.now(BEIJING)
    logging.info("北京时间：%s", now.isoformat())

    if FORCE:
        plain = "邮件系统测试成功。\n此邮件不代表当前市场触发阈值。\n"
        html = "<html><body><h2>美股市场监测邮件测试</h2><p>邮件发送配置可用。</p></body></html>"
        send_email("[测试] 美股市场监测邮件", plain, html)
        return

    data, score = get_cnn_data()

    if not (score < LOW or score > HIGH):
        logging.info("当前指数 %.2f，未严格低于 %s 或高于 %s，不发送提醒。",
                     score, LOW, HIGH)
        return

    plain, html = build_email(data, score)

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
