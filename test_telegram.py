import os, requests

token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
chat = os.environ["TELEGRAM_CHAT_ID"].strip()
r = requests.post(
    f"https://api.telegram.org/bot{token}/sendMessage",
    data={"chat_id": chat, "text": "TEST: Telegram connection working"},
    timeout=15,
)
print("Status:", r.status_code)
print(r.text[:300])
