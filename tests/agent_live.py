"""Agent-desktop end-to-end test over stdio. Everything happens on a fresh agent desktop (its own
seat), so it is safe while the user works: their cursor, focus and camera must stay untouched."""

import asyncio, base64, json, os, re, subprocess, sys, time
from mcp import Client
from mcp.client.stdio import StdioServerParameters

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("OUT", "/tmp")
LOG = os.path.join(OUT, "agent-testapp.jsonl")
results = []


def host(cmd):
    return json.loads(subprocess.run(["hyprctl", "-j"] + cmd.split(), capture_output=True, text=True).stdout or "{}")


def events(kind=None, since=0.0):
    try:
        evs = [json.loads(l) for l in open(LOG)]
    except FileNotFoundError:
        return []
    return [e for e in evs if e["t"] >= since and (kind is None or e["ev"] == kind)]


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


async def main():
    if os.path.exists(LOG):
        os.unlink(LOG)
    host_focus = host("activewindow").get("address")
    params = StdioServerParameters(command=f"{HERE}/.venv/bin/python", args=["-m", "hypr_computer"],
                                   env={**os.environ, "HYPR_COMPUTER_TARGET": "agent"})
    async with Client(params) as c:
        async def call(name, **args):
            r = await c.call_tool(name, args)
            t = "\n".join(b.text for b in r.content if b.type == "text")
            for b in r.content:
                if b.type == "image":
                    open(f"{OUT}/agent-{name}-{int(time.time()*1000)%100000}.{b.mime_type.split('/')[1]}", "wb").write(base64.b64decode(b.data))
            print(f"  > {name} {str(args)[:140]}\n    {t[:500]}")
            return t

        t = await call("agent_desktop", action="start", place=int(sys.argv[1]) if len(sys.argv) > 1 else None)
        check("agent desktop started on a place", "place" in t, t)
        await call("agent_desktop", action="status")
        await call("desktop")
        await call("launch", command=f"python3 {HERE}/tests/testapp.py {LOG}", wait_ms=800)
        for _ in range(40):
            if events("ready"):
                break
            time.sleep(0.25)
        check("test app runs inside the agent desktop", events("ready"))
        await call("screenshot")
        t0 = time.time()
        await call("click", text="Press me", wait_ms=300)
        check("click by text", events("button", t0))
        tree = await call("a11y_tree", window="hypr-computer test")
        m = re.search(r"(e\d+) \[(?:text|entry)\]", tree)
        t0 = time.time()
        if m:
            await call("click", ref=m.group(1))
        else:
            await call("click", text="type here")
        text = "zy ZY @#|{} é à ü € ~ 😀 ok"
        await call("type_text", text=text, wait_ms=300)
        got = (events("entry", t0) or [{"text": ""}])[-1]["text"]
        check("typing on the agent's own keyboard", got == text, repr(got))
        await call("key", keys="ctrl+a")
        await call("type_text", text="replaced", wait_ms=200)
        check("shortcut + typing", (events("entry", t0) or [{"text": ""}])[-1]["text"] == "replaced")
        hits = await call("ocr", query="EVENT PAD")
        mm = re.search(r"screen center \((\d+),(\d+)\)", hits)
        if mm:
            px, py = int(mm.group(1)) + 150, int(mm.group(2)) + 70
            t0 = time.time()
            await call("click", x=px, y=py, coords="screen", wait_ms=200)
            await call("click", x=px + 37, y=py + 23, coords="screen", wait_ms=200)
            pr = events("press", t0)
            if len(pr) >= 2:
                dx, dy = pr[1]["x"] - pr[0]["x"], pr[1]["y"] - pr[0]["y"]
                check("click precision (37,23)", abs(dx - 37) <= 1 and abs(dy - 23) <= 1, f"({dx},{dy})")
            t0 = time.time()
            await call("click", x=px, y=py, coords="screen", button="middle", wait_ms=200)
            check("middle click works on the agent desktop", any(e["button"] == 2 for e in events("press", t0)))
            t0 = time.time()
            await call("drag", from_x=px - 100, from_y=py - 40, to_x=px + 120, to_y=py + 50, coords="screen")
            de = events("drag-end", t0)
            check("drag (220,90)", de and abs(de[0]["dx"] - 220) <= 2 and abs(de[0]["dy"] - 90) <= 2, str(de))
            t0 = time.time()
            await call("scroll", direction="down", amount=2, x=px, y=py, coords="screen", wait_ms=200)
            check("scroll", sum(e["dy"] for e in events("scroll", t0)) > 0, str(events("scroll", t0)[:2]))
        await call("clipboard", action="set", text="agent clipboard")
        cb = await call("clipboard", action="get")
        hostcb = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, text=True).stdout
        check("agent clipboard is separate from the user's", cb == "agent clipboard" and hostcb != "agent clipboard",
              f"host has {hostcb[:30]!r}")
        await call("screenshot", grid=False)
        await call("window", target="hypr-computer test", action="close")
    check("user's focus untouched", host("activewindow").get("address") == host_focus,
          f"{host_focus} -> {host('activewindow').get('address')}")
    print(f"\n{sum(results)}/{len(results)} passed")


asyncio.run(main())
