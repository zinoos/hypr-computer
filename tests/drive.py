"""Drive hypr-computer by hand from a shell, with one long-lived server session (frames persist).

    python tests/drive.py serve &                     # start (socket in $XDG_RUNTIME_DIR)
    python tests/drive.py call screenshot '{}'        # prints text, saves images to $OUT, prints paths
    python tests/drive.py stop
"""

import asyncio
import base64
import json
import os
import sys
import time

from mcp import Client
from mcp.client.stdio import StdioServerParameters

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "hypr-computer-drive.sock")
OUT = os.environ.get("OUT", "/tmp")


async def serve():
    params = StdioServerParameters(command=f"{HERE}/.venv/bin/python", args=["-m", "hypr_computer"])
    async with Client(params) as c:
        stop = asyncio.Event()

        async def handle(reader, writer):
            req = json.loads(await reader.readline())
            if req["tool"] == "__stop__":
                stop.set()
                writer.write(b'{"text": "bye"}\n')
                await writer.drain()
                writer.close()
                return
            try:
                r = await c.call_tool(req["tool"], req["args"])
                texts, images = [], []
                for b in r.content:
                    if b.type == "text":
                        texts.append(b.text)
                    elif b.type == "image":
                        p = f"{OUT}/drive-{int(time.time() * 1000) % 10_000_000}.{b.mime_type.split('/')[1]}"
                        with open(p, "wb") as f:
                            f.write(base64.b64decode(b.data))
                        images.append(p)
                out = {"text": "\n".join(texts), "images": images}
            except Exception as ex:
                out = {"text": f"client error: {ex}", "images": []}
            writer.write((json.dumps(out) + "\n").encode())
            await writer.drain()
            writer.close()

        if os.path.exists(SOCK):
            os.unlink(SOCK)
        server = await asyncio.start_unix_server(handle, SOCK, limit=2**24)
        async with server:
            await stop.wait()


async def call(tool, args):
    reader, writer = await asyncio.open_unix_connection(SOCK, limit=2**24)
    writer.write((json.dumps({"tool": tool, "args": args}) + "\n").encode())
    await writer.drain()
    res = json.loads(await reader.readline())
    print(res["text"])
    for p in res.get("images", []):
        print("[image]", p)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "serve":
        asyncio.run(serve())
    elif cmd == "stop":
        asyncio.run(call("__stop__", {}))
    else:
        asyncio.run(call(sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}))
