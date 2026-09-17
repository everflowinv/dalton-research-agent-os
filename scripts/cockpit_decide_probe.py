#!/usr/bin/env python3
"""Submit one approval decision to the local cockpit exactly as the page does, and print the raw answer.

The approvals page maps every failure it cannot classify to "保存决定暂时未完成"
and shows nothing else, so when a button fails there is no way to see why from
the browser.  This does what the button does -- one session, one CSRF token,
one POST to /v1/cockpit/decide -- and prints the HTTP status, the elapsed time
and the exact JSON body the control server returned.

    .venv/bin/python scripts/cockpit_decide_probe.py --card 0001467373 --decision return_for_more_work
    .venv/bin/python scripts/cockpit_decide_probe.py --list

``--dry-run`` stops after printing the card and the body it would send.  A
decision that succeeds is a real decision: the same one the button makes.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request
import uuid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://127.0.0.1:8793")
    parser.add_argument("--login", required=True, help="the Tailscale login the cockpit allows")
    parser.add_argument("--kind", default="deep_insight_gate")
    parser.add_argument("--card", help="substring of the card ref to act on")
    parser.add_argument("--decision")
    parser.add_argument("--rationale", help="defaults to the card's rationale_default")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    headers = {"Tailscale-User-Login": args.login, "Content-Type": "application/json"}

    def get(path: str) -> dict:
        with opener.open(urllib.request.Request(args.base + path, headers=headers),
                         timeout=args.timeout) as response:
            return json.load(response)

    overview = get("/v1/cockpit/overview")
    csrf = overview.get("csrf_token") or ""
    approvals = get("/v1/cockpit/approvals")
    cards = [item for item in approvals.get("items", []) if item.get("kind") == args.kind]
    if args.list or not args.card:
        for item in cards:
            print(item["ref"], [action.get("label") for action in item.get("actions", [])])
        return 0
    matches = [item for item in cards if args.card in item["ref"]]
    if len(matches) != 1:
        print(f"expected one card matching {args.card!r}, found {len(matches)}", file=sys.stderr)
        return 2
    card = matches[0]
    if not args.decision:
        print("--decision is required; the card offers:",
              [action.get("decision") for action in card.get("actions", [])], file=sys.stderr)
        return 2
    rationale = args.rationale if args.rationale is not None else (card.get("rationale_default") or "")
    body = {
        "kind": card["kind"], "ref": card["ref"], "hash": card["hash"],
        "decision": args.decision, "rationale": rationale, "request_id": uuid.uuid4().hex,
        **({"evaluation_id": card["evaluation_id"]} if card.get("evaluation_id") else {}),
    }
    print("card:", card["ref"])
    print("decision:", args.decision, "| rationale chars:", len(rationale), "| csrf:", bool(csrf))
    if args.dry_run:
        print(json.dumps(body, ensure_ascii=False, indent=1)[:1200])
        return 0
    request = urllib.request.Request(
        args.base + "/v1/cockpit/decide", data=json.dumps(body).encode("utf-8"),
        headers={**headers, "X-Dalton-CSRF": csrf}, method="POST")
    started = time.monotonic()
    try:
        with opener.open(request, timeout=args.timeout) as response:
            print(f"HTTP {response.status} after {time.monotonic() - started:.1f}s")
            print(response.read().decode("utf-8")[:2000])
            return 0
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code} after {time.monotonic() - started:.1f}s")
        print(exc.read().decode("utf-8", "replace")[:2000])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
