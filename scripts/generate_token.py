#!/usr/bin/env python3
"""Generate a Garmin Connect auth token for CI/CD use.

Run once locally:
    pip install garminconnect garth
    python scripts/generate_token.py

Then store the printed value as GitHub Secret: GARMIN_TOKEN_BASE64
The token auto-refreshes — you only need to regenerate it if it expires (~1 year).
"""

import base64
import getpass

from garminconnect import Garmin, GarminConnectTwoFactorAuthenticationError

email = input("Garmin Email: ").strip()
password = getpass.getpass("Garmin Passwort: ")

print("\nVerbinde mit Garmin Connect...")
api = Garmin(email=email, password=password)

try:
    api.login()
except GarminConnectTwoFactorAuthenticationError:
    mfa_code = input("MFA-Code (aus Authenticator-App): ").strip()
    api.login(mfa_code)

token_str = api.garth.dumps()
token_b64 = base64.b64encode(token_str.encode()).decode()

print("\n" + "=" * 64)
print("GARMIN_TOKEN_BASE64 (als GitHub Secret speichern):")
print("=" * 64)
print(token_b64)
print("=" * 64)
print("\nGehe zu: GitHub Repo → Settings → Secrets → Actions → New secret")
print("Name:  GARMIN_TOKEN_BASE64")
print("Value: (den obigen Wert einfügen)")
