#!/usr/bin/env python3
import sys

try:
    from yandex_music import Client
except ImportError:
    print("❌ Error: yandex-music library is not installed. Run 'pip install yandex-music' first.")
    sys.exit(1)

def main():
    print("🔑 Yandex Music OAuth Token Generator")
    print("=====================================")
    print("This script will help you authorize Yandex Music API and get an OAuth token.")
    print("Please follow the instructions below:\n")

    import os
    yandex_proxy = os.getenv("YANDEX_PROXY") or os.getenv("PROXY")
    old_http = os.environ.get("HTTP_PROXY")
    old_https = os.environ.get("HTTPS_PROXY")
    if yandex_proxy:
        print(f"ℹ️ Routing authorization request through proxy: {yandex_proxy}")
        os.environ["HTTP_PROXY"] = yandex_proxy
        os.environ["HTTPS_PROXY"] = yandex_proxy

    client = Client()
    
    def on_code(code):
        print(f"👉 STEP 1: Open this URL in your web browser (where you are logged in to Yandex):")
        print(f"   {code.verification_url}")
        print("\n👉 STEP 2: Enter this 6-digit code:")
        print(f"   ⭐  {code.user_code}  ⭐\n")
        print("Waiting for you to authorize the device...")

    try:
        token_info = client.device_auth(on_code=on_code)
        token = token_info.access_token
        print("\n🎉 SUCCESS! You have successfully authorized the device.")
        print("Here is your Yandex Music OAuth token:")
        print(f"\n🔑 {token}\n")
        print("Copy this token and add it to your .env file on the server:")
        print("YANDEX_TOKEN=your_token_here")
    except Exception as e:
        print(f"\n❌ Error during authorization: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
