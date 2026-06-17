#!/usr/bin/env python3
import http.cookiejar
import urllib.request
import json
import os
import sys
import re

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

    # Gather Yandex domains present in cookies
    yandex_tlds = set()
    for cookie in cookie_jar:
        m = re.search(r'\byandex\.(ru|by|kz|uz|com)\b', cookie.domain)
        if m:
            yandex_tlds.add(m.group(1))

    tlds_to_try = list(yandex_tlds) if yandex_tlds else ['ru', 'by', 'kz', 'uz', 'com']
    print(f"ℹ️ Domains present in cookies to check: {', '.join(tlds_to_try)}")

    # Build opener with cookies
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        ("Referer", "https://music.yandex.ru/"),
        ("X-Requested-With", "XMLHttpRequest")
    ]

    successful_tld = None

    for tld in tlds_to_try:
        print(f"\nChecking domain: music.yandex.{tld}...")
        try:
            req = urllib.request.Request(f"https://music.yandex.{tld}/handlers/library.jsx")
            with opener.open(req, timeout=10) as response:
                data = json.loads(response.read().decode('utf-8'))
                owner = data.get("owner", {})
                uid = owner.get("uid")
                login = owner.get("login")
                name = owner.get("name")
                print(f"✅ Status: Logged In on music.yandex.{tld}!")
                print(f"👤 Account: {name or 'Unknown'} (Login: {login or 'Unknown'}, UID: {uid or 'Unknown'})")
                
                # Check track access on this domain
                print(f"Checking track '{test_track}' on music.yandex.{tld}...")
                track_url = f"https://music.yandex.{tld}/handlers/track.jsx?track={test_track}"
                track_req = urllib.request.Request(track_url)
                with opener.open(track_req, timeout=10) as tr_response:
                    tr_data = json.loads(tr_response.read().decode('utf-8'))
                    if "track" in tr_data:
                        track = tr_data["track"]
                        title = track.get("title")
                        artists = ", ".join([a.get("name") for a in track.get("artists", [])])
                        print(f"✅ Status: Track is Accessible on music.yandex.{tld}!")
                        print(f"🎵 Track: {title} - {artists}")
                        
                        has_plus = track.get("hasPlus", False) or tr_data.get("hasPlus", False)
                        if has_plus:
                            print(f"⭐ Yandex Plus subscription: ACTIVE on music.yandex.{tld} (explicitly indicated)")
                        else:
                            print(f"ℹ️ Yandex Plus subscription: Likely ACTIVE on music.yandex.{tld} (successfully bypassed block)")
                        
                        successful_tld = tld
                        break
                    else:
                        print(f"⚠️ Session is logged in on music.yandex.{tld} but track info is missing in response.")
        except urllib.error.HTTPError as e:
            # Check if the error body contains Yandex CAPTCHA block
            try:
                body = e.read().decode('utf-8', errors='replace')
                if "запросы" in body and "автоматические" in body or "captcha" in body.lower():
                    print(f"⚠️ Yandex is blocking your IP address on domain .{tld} with a CAPTCHA challenge.")
            except Exception:
                pass

            if e.code == 404:
                print(f"❌ Not Logged In on music.yandex.{tld} (Yandex returned 404)")
            else:
                print(f"❌ HTTP Error {e.code} on music.yandex.{tld}: {e.reason}")
        except Exception as e:
            print(f"❌ Error checking music.yandex.{tld}: {e}")

    if successful_tld:
        print(f"\n🎉 SUCCESS! Valid authenticated session found on domain: music.yandex.{successful_tld}")
        print(f"👉 In your bot, Yandex Music links will be automatically redirected to use this domain.")
        return True
    else:
        print("\n❌ FAILURE: No active session found. Your cookies are either expired, invalid, or do not have a Yandex Plus subscription.")
        return False

if __name__ == "__main__":
    path = "data/cookies.txt"
    if len(sys.argv) > 1:
        path = sys.argv[1]
    
    check_cookies(path)
