"""Smoke test over real stdio: list tools and call the read-only ones. Saves returned images to $OUT."""
import asyncio, base64, os, sys, time
from mcp import Client
from mcp.client.stdio import StdioServerParameters

OUT = os.environ.get("OUT", "/tmp")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

async def main(calls):
    params = StdioServerParameters(command=f"{HERE}/.venv/bin/python", args=["-m", "hypr_computer"])
    async with Client(params) as c:
        tools = await c.list_tools()
        print(len(tools.tools), "tools:", ", ".join(t.name for t in tools.tools))
        for i, (name, args) in enumerate(calls):
            t = time.time()
            r = await c.call_tool(name, args)
            print(f"\n=== {name} {args} ({time.time()-t:.2f}s) error={r.is_error}")
            for j, block in enumerate(r.content):
                if block.type == "text":
                    print(block.text[:2500])
                elif block.type == "image":
                    p = f"{OUT}/{i}-{name}-{j}.{block.mime_type.split('/')[1]}"
                    open(p, "wb").write(base64.b64decode(block.data))
                    print("[image]", p)

calls = [eval(a) for a in sys.argv[1:]] if len(sys.argv) > 1 else [
    ("desktop", {}), ("windows", {}), ("screenshot", {}),
    ("ocr", {"query": "File"}), ("apps", {"query": "files"}),
    ("hyprland", {"lua": "return hl.get_active_window().title"}), ("clipboard", {"action": "types"}),
]
asyncio.run(main(calls))
