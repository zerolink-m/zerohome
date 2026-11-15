#!/usr/bin/env python3
"""Fail closed before publication/promotion; never change package visibility."""
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
PACKAGE = "/users/zerolink-m/packages/container/zerohome"


def get(path):
    request = urllib.request.Request(API + path, headers={
        "Authorization": "Bearer " + os.environ["GH_TOKEN"],
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def verify(allow_new=False):
    try:
        package = get(PACKAGE)
    except urllib.error.HTTPError as exc:
        if exc.code != 404 or not allow_new:
            raise SystemExit("Cannot verify private package visibility; refusing publication.") from None
        # GitHub Container Registry creates new packages PRIVATE, independently
        # of repository visibility (see deployment documentation). Only bootstrap
        # from our private source repository; never infer privacy from a 403.
        repository = get("/repos/zerolink-m/zerohome")
        if repository.get("private") is not True:
            raise SystemExit("New-package bootstrap requires the private source repository.")
        print("Package absent: bootstrap SHA tag under GHCR's documented private-by-default policy; promotion requires a private visibility readback.")
        return
    if package.get("visibility") != "private":
        raise SystemExit("Package is not private; refusing publication. No visibility changes are automatic.")
    print("Verified: GHCR package visibility is private.")


if __name__ == "__main__":
    verify(allow_new=sys.argv[1:] == ["--allow-new"])
