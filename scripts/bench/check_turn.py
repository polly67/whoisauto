"""Check TURN allocation without logging server credentials."""
import asyncio
import json
import os
import socket
import urllib.parse

from aioice.turn import create_turn_endpoint
from aioice.ice import StunProtocol
from aioice import Candidate


async def main():
    raw = os.environ["WHOISAUTO_TURN_URL"]
    parsed = urllib.parse.urlsplit(raw.replace("turn:", "turn://", 1).replace("turns:", "turns://", 1))
    port = parsed.port or (5349 if parsed.scheme == "turns" else 3478)
    transport = urllib.parse.parse_qs(parsed.query).get("transport", ["udp"])[0]
    class Receiver(asyncio.DatagramProtocol):
        pass
    connection = None
    try:
        connection, protocol = await asyncio.wait_for(create_turn_endpoint(
            Receiver, server_addr=(parsed.hostname, port),
            username=os.environ["WHOISAUTO_TURN_USERNAME"], password=os.environ["WHOISAUTO_TURN_CREDENTIAL"],
            transport=transport, ssl=parsed.scheme == "turns"), timeout=20)
        print(json.dumps({"turn_allocation": "ok", "transport": transport, "server": parsed.hostname}))
    except Exception as error:
        print(json.dumps({"turn_allocation": "failed", "error_type": type(error).__name__}))
        raise SystemExit(1)
    finally:
        if connection: connection.close()


asyncio.run(main())
