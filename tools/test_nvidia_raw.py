import os
import json
import urllib.request
import urllib.error
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

api_key = os.getenv("NVIDIA_API_KEY")

if not api_key:
    print("❌ NVIDIA_API_KEY not found")
    raise SystemExit(1)

url = "https://integrate.api.nvidia.com/v1/chat/completions"

payload = {
    "model": "deepseek-ai/deepseek-v4.1-flash",
    "messages": [
        {
            "role": "user",
            "content": "Reply with exactly OK"
        }
    ],
    "max_tokens": 4,
    "temperature": 0,
    "stream": True
}

request = urllib.request.Request(
    url,
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    },
    method="POST",
)

print("Connecting to NVIDIA...")
print("Model: deepseek-v4.1-flash")

try:
    with urllib.request.urlopen(request, timeout=60) as response:
        print("HTTP:", response.status)
        print("Receiving response...\n")

        while True:
            line = response.readline()

            if not line:
                break

            text = line.decode("utf-8", errors="ignore").strip()

            if text:
                print(text)

    print("\n✅ NVIDIA request completed")

except urllib.error.HTTPError as e:
    print(f"\n❌ HTTP {e.code}")
    print(e.read().decode("utf-8", errors="replace"))

except Exception as e:
    print(f"\n❌ {type(e).__name__}: {e}")