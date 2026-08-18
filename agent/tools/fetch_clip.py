"""Fetch a recording by timestamp from a laptop running OngoingRec.

    python tools/fetch_clip.py 2026-08-17T19:06:00 --employee-id PW33744

The laptop being switched off is the ordinary case, not an error: a counsellor
closes the lid at 6pm and someone asks for 6:30pm audio the next morning. That
arrives here as a refused connection, which on its own reads like a bug in the
client, so it is reported as what it actually means -- the laptop is not
running -- and given its own exit code so a caller can tell it apart from "the
laptop is up but that moment was never recorded".
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

# Exit codes, so a shell script can branch without parsing the message.
EXIT_OK = 0
EXIT_LAPTOP_DOWN = 3
EXIT_NO_RECORDING = 4
EXIT_BAD_REQUEST = 5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("timestamp", help="ISO-8601; naive values are laptop local time")
    parser.add_argument("--employee-id")
    parser.add_argument("--email-id")
    parser.add_argument("--window-seconds", type=int, default=60)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default="", help="only if local_api_token is set")
    parser.add_argument("--output", default="clip.mp3")
    args = parser.parse_args(argv)

    if not (args.employee_id or args.email_id):
        print("need --employee-id or --email-id", file=sys.stderr)
        return EXIT_BAD_REQUEST

    payload = {"timestamp": args.timestamp, "window_seconds": args.window_seconds}
    if args.employee_id:
        payload["employee_id"] = args.employee_id
    if args.email_id:
        payload["email_id"] = args.email_id
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}

    url = f"http://{args.host}:{args.port}/recordings/fetch"
    try:
        # Long enough for ffmpeg to cut and re-encode a clip spanning several
        # segments, which a wide window on a fragmented day really can take.
        response = httpx.post(url, json=payload, headers=headers, timeout=120.0)
    except httpx.ConnectError:
        print(f"Laptop shut down hai (ya OngoingRec service band hai) - {args.host}:{args.port}")
        return EXIT_LAPTOP_DOWN
    except httpx.TimeoutException:
        print(f"Laptop ne jawab nahi diya (timeout) - {args.host}:{args.port}")
        return EXIT_LAPTOP_DOWN

    if response.status_code == 404:
        print(f"Recording nahi mili: {_detail(response)}")
        return EXIT_NO_RECORDING
    if response.status_code >= 400:
        print(f"HTTP {response.status_code}: {_detail(response)}", file=sys.stderr)
        return EXIT_BAD_REQUEST

    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(response.content)

    print(f"Clip saved: {destination}  ({len(response.content)} bytes)")
    print(f"  covers   : {response.headers.get('x-ongoingrec-clip-start')}"
          f" -> {response.headers.get('x-ongoingrec-clip-end')}")
    gaps = json.loads(response.headers.get("x-ongoingrec-gaps", "[]"))
    if gaps:
        # A gap means the laptop was asleep or the mic was gone for that
        # stretch. The audio is padded with silence to keep later offsets
        # honest, so without this line the silence looks like a quiet room.
        print(f"  gaps     : {len(gaps)} ({sum(g['seconds'] for g in gaps):.0f}s not recorded)")
    return EXIT_OK


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    return body.get("detail") or body.get("error") or response.text[:200]


if __name__ == "__main__":
    raise SystemExit(main())
