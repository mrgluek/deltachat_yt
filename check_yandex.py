#!/usr/bin/env python3
import http.cookiejar
import urllib.request
import json
import os
import sys

def check_cookies(cookies_path, test_track="150402031:41648883"):
    if not os.path.exists(cookies_path):
        print(f"❌ Cookies file not found at: {cookies_path}")
        return False

    # Load Netscape cookies
    cookie_jar = http.cookiejar.MozillaCookieJar(cookies_path)
    try:
        cookie_jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as e:
        print(f"❌ Failed to parse cookies file: {e}")
        return False

    # Check if we have Yandex cookies in the jar
    yandex_cookies = [cookie for cookie in cookie_jar if "yandex" in cookie.domain]
    if not yandex_cookies:
        print("❌ No Yandex cookies found in the cookie jar. Make sure you exported cookies for music.yandex.ru.")
        return False
        
    print(f"ℹ️ Loaded {len(yandex_cookies)} Yandex cookies from {cookies_path}")

    # Build opener with cookies
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        ("Referer", "https://music.yandex.ru/"),
        ("X-Requested-With", "XMLHttpRequest")
    ]

    # Step 1: library.jsx (check if session is active)
    print("\n1. Checking library endpoint (login status)...")
    logged_in = False
    try:
        req = urllib.request.Request("https://music.yandex.ru/handlers/library.jsx")
        with opener.open(req, timeout=15) as response:
            data = json.loads(response.read().decode('utf-8'))
            owner = data.get("owner", {})
            uid = owner.get("uid")
            login = owner.get("login")
            name = owner.get("name")
            print("✅ Status: Logged In!")
            print(f"👤 Account: {name or 'Unknown'} (Login: {login or 'Unknown'}, UID: {uid or 'Unknown'})")
            logged_in = True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("❌ Status: Not Logged In (or cookies expired/invalid). Yandex returned 404.")
        else:
            print(f"❌ HTTP Error {e.code}: {e.reason}")
    except Exception as e:
        print(f"❌ Error during request: {e}")

    # Step 2: track.jsx (check if test track is accessible)
    print(f"\n2. Checking track API for '{test_track}'...")
    try:
        url = f"https://music.yandex.ru/handlers/track.jsx?track={test_track}"
        req = urllib.request.Request(url)
        with opener.open(req, timeout=15) as response:
            data = json.loads(response.read().decode('utf-8'))
            
            # If we get track data, then the session has access
            track = data.get("track", {})
            title = track.get("title")
            artists = ", ".join([a.get("name") for a in track.get("artists", [])])
            
            print("✅ Status: Track is Accessible!")
            print(f"🎵 Track: {title} - {artists}")
            
            # Look for Yandex Plus indicator in track info
            # Usually premium tracks will have special flags if accessible via Plus
            has_plus = track.get("hasPlus", False) or data.get("hasPlus", False)
            if has_plus:
                print("⭐ Yandex Plus subscription: ACTIVE (explicitly indicated)")
            else:
                print("ℹ️ Yandex Plus subscription: Likely ACTIVE (successfully bypassed 404 block)")
            return True
            
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("❌ Status: Track is INACCESSIBLE. Yandex returned 404.")
            print("👉 This means the cookies do not have a Yandex Plus subscription, or this track is not available in the IP's region.")
        else:
            print(f"❌ HTTP Error {e.code}: {e.reason}")
    except Exception as e:
        print(f"❌ Error during request: {e}")

    return False

if __name__ == "__main__":
    path = "data/cookies.txt"
    if len(sys.argv) > 1:
        path = sys.argv[1]
    
    # Default test track: https://music.yandex.ru/album/41648883/track/150402031
    check_cookies(path)
