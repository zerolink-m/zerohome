#!/usr/bin/env python3
"""Fail closed unless the GHCR package is public; never change visibility."""
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
PACKAGE = "/users/zerolink-m/packages/container/zerohome"


def get(path, token):
    request = urllib.request.Request(API + path, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def verify():
    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        raise SystemExit("GH_TOKEN is required to verify GHCR package visibility; refusing release promotion.")

    try:
        package = get(PACKAGE, token)
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            message = "GitHub denied package visibility access. Grant the workflow token package read access; refusing release promotion."
        elif exc.code == 404:
            message = "GHCR package was not found or is not readable by this token; refusing release promotion."
        else:
            message = f"GitHub package visibility API returned HTTP {exc.code}; refusing release promotion."
        raise SystemExit(message) from None
    except urllib.error.URLError as exc:
        raise SystemExit(f"Cannot reach GitHub package visibility API ({exc.reason}); refusing release promotion.") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise SystemExit("GitHub returned invalid package visibility data; refusing release promotion.") from None

    if not isinstance(package, dict):
        raise SystemExit("GitHub returned an unexpected package record; refusing release promotion.")

    visibility = package.get("visibility")
    if visibility != "public":
        raise SystemExit(f"Expected public GHCR package, API reported {visibility!r}; refusing release promotion.")
    print("Verified: GHCR package visibility is public.")


if __name__ == "__main__":
    if len(sys.argv) != 1:
        raise SystemExit("Usage: check-package-visibility.py")
    verify()
