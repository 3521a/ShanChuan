import os, smtplib, requests
from email.mime.text import MIMEText

SOURCE = os.getenv("FNG_SOURCE", "cnn")

# 注意:下面这行是测试用的,指数是多少都会发邮件。
# 测试成功后,要改回 LOW, HIGH = 25, 75
LOW, HIGH = 100, 0

def get_index():
    if SOURCE == "cnn":
        r = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        d = r.json()["fear_and_greed"]
        return round(d["score"]), d["rating"]
    r = requests.get("https://api.alternative.me/fng/", timeout=30)
    r.raise_for_status()
    d = r.json()["data"][0]
    return int(d["value"]), d["value_classification"]

def send_mail(subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = os.environ["MAIL_USER"]
    msg["To"] = os.environ["MAIL_TO"]
    with smtplib.SMTP_SSL(os.getenv("SMTP_HOST", "smtp.gmail.com"), 465) as s:
        s.login(os.environ["MAIL_USER"], os.environ["MAIL_PASS"])
        s.send_message(msg)

score, rating = get_index()
print(f"当前指数: {score} ({rating})")

if score <= LOW:
    send_mail(f"⚠️ 极度恐惧:{score}", f"恐惧与贪婪指数为 {score}({rating}),已低于 {LOW}。")
elif score >= HIGH:
    send_mail(f"🔥 极度贪婪:{score}", f"恐惧与贪婪指数为 {score}({rating}),已高于 {HIGH}。")
