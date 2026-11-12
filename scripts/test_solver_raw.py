#!/usr/bin/env python3
import urllib.request
import json

url = "http://127.0.0.1:8192/solve"
payload = {
    "url": "https://002.hindianimeszone.com/download1.php?code=0YVFP&q=720p"
}
req = urllib.request.Request(
    url,
    data=json.dumps(payload).encode('utf-8'),
    headers={"Content-Type": "application/json"}
)
print("Sending solve request to", url)
try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        print("Solve Result:", json.dumps(data, indent=2))
except Exception as e:
    print("Error:", e)
