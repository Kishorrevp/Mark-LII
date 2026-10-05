import os
import sys
from pathlib import Path

from openai import OpenAI
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

api_key = os.getenv("NVIDIA_API_KEY")

if not api_key:
    print("❌ NVIDIA_API_KEY not found in .env")
    sys.exit(1)

client = OpenAI(
    api_key=api_key,
    base_url="https://integrate.api.nvidia.com/v1",
    timeout=30.0,
)

MODEL = "poolside/laguna-xs-2.1"

print("Connecting to NVIDIA...")
print(f"Model: {MODEL}")

try:
    stream = client.chat.completions.create(
        model = "poolside/laguna-xs-2.1",
        messages=[
            {
                "role": "user",
                "content": "Reply with exactly: OK"
            }
        ],
        temperature=0,
        max_tokens=5,
        stream=True,
    )

    print("\n✅ NVIDIA RESPONSE:\n")

    for chunk in stream:
        if not chunk.choices:
            continue

        delta = chunk.choices[0].delta
        if delta.content:
            print(delta.content, end="", flush=True)

    print("\n\n✅ NVIDIA TEST PASSED")

except Exception as e:
    print("\n❌ NVIDIA TEST FAILED")
    print(type(e).__name__, str(e))