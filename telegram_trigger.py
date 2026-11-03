import requests
import os

GITHUB_TOKEN = os.getenv("CGit_Token")

OWNER = "NiranjanVH"
REPO = "coinglass-telegram-bo"

def trigger_workflow():
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/dispatches"

    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json"
        },
        json={"event_type": "run_coinglass"}
    )

    print(response.status_code, response.text)
