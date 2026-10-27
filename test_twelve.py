import os
import requests

key = os.environ["TWELVE_API_KEY"].strip()
r = requests.get(
    "https://api.twelvedata.com/time_series",
    params={"symbol": "XAU/USD", "interval": "1min",
            "outputsize": 5, "apikey": key},
    timeout=20,
)
print("HTTP:", r.status_code)
d = r.json()
if d.get("status") == "error":
    print("ERROR:", d.get("code"), d.get("message"))
else:
    print("OK, symbol:", d.get("meta", {}).get("symbol"))
    for v in d.get("values", []):
        print(v["datetime"], "O", v["open"], "C", v["close"])
