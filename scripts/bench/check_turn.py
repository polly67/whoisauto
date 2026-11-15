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
    for candidate_port, candidate_transport, tls in ((port, transport, parsed.scheme == "turns"), (80, "udp", False), (80, "tcp", False)):
        connection = None
        try:
            connection, protocol = await asyncio.wait_for(create_turn_endpoint(
                Receiver, server_addr=(parsed.hostname, candidate_port),
                username=os.environ["WHOISAUTO_TURN_USERNAME"], password=os.environ["WHOISAUTO_TURN_CREDENTIAL"],
                transport=candidate_transport, ssl=tls), timeout=12)
            print(json.dumps({"turn_allocation": "ok", "transport": candidate_transport,
                              "port": candidate_port, "tls": tls, "server": parsed.hostname}))
            return
        except Exception as error:
            print(json.dumps({"turn_allocation": "failed", "transport": candidate_transport,
                              "port": candidate_port, "tls": tls, "error_type": type(error).__name__}))
        finally:
            if connection: connection.close()
    raise SystemExit(1)



asyncio.run(main())
