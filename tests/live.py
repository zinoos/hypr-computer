"""Live end-to-end test: drives tests/testapp.py through the MCP server over stdio and checks what
the app actually received. Injects real input: only run when nobody is using the machine."""

import asyncio
import base64
import json
import os
import re
import sys
import time

from mcp import Client
from mcp.client.stdio import StdioServerParameters

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("OUT", "/tmp")
LOG = os.path.join(OUT, "testapp-events.jsonl")
GESTURES = "--gestures" in sys.argv
results = []


def events(kind=None, since=0.0):
    out = []
    try:
        for line in open(LOG):
            e = json.loads(line)
            if e["t"] >= since and (kind is None or e["ev"] == kind):
                out.append(e)
    except FileNotFoundError:
        pass
    return out


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


async def main():
    if os.path.exists(LOG):
        os.unlink(LOG)
    params = StdioServerParameters(command=f"{HERE}/.venv/bin/python", args=["-m", "hypr_computer"])
    async with Client(params) as c:
        async def call(name, **args):
            r = await c.call_tool(name, args)
            texts = [b.text for b in r.content if b.type == "text"]
            for i, b in enumerate(r.content):
                if b.type == "image":
                    with open(f"{OUT}/live-{name}-{int(time.time() * 1000) % 100000}.{b.mime_type.split('/')[1]}", "wb") as f:
                        f.write(base64.b64decode(b.data))
            t = "\n".join(texts)
            print(f"  > {name} {args if len(str(args)) < 160 else str(args)[:160]}\n    {t[:600]}".rstrip())
            return t

        await call("launch", command=f"python3 {HERE}/tests/testapp.py {LOG}", wait_for_window=True, wait_ms=800)
        for _ in range(40):
            if events("ready"):
                break
            time.sleep(0.25)
        check("test app started", events("ready"))
        await call("window", target="hypr-computer test", action="focus", wait_ms=400)
        await call("screenshot", window="hypr-computer test")

        # 1. click by OCR text
        t0 = time.time()
        await call("click", text="Press me", within="hypr-computer test", wait_ms=300)
        check("click(text=) pressed the button", events("button", t0))

        # 2. accessibility: toggle checkbox, set slider
        tree = await call("a11y_tree", window="hypr-computer test")
        m = re.search(r"(e\d+) \[check box\] 'Check me'", tree)
        if m:
            t0 = time.time()
            await call("a11y_act", ref=m.group(1), action="click", wait_ms=200)
            check("a11y_act toggled the checkbox", any(e["active"] for e in events("check", t0)))
        else:
            check("a11y tree lists the checkbox", False, tree[:300])
        m = re.search(r"(e\d+) \[slider\]", tree)
        if m:
            t0 = time.time()
            await call("a11y_act", ref=m.group(1), action="set_value", value="42")
            check("a11y set_value moved the slider", any(abs(e["value"] - 42) < 0.5 for e in events("scale", t0)))
        m = re.search(r"(e\d+) \[(?:text|entry)\]", tree)
        entry_ref = m.group(1) if m else None

        # 3. click the entry (a11y ref -> coordinates) and type layout-specific + exotic characters
        t0 = time.time()
        if entry_ref:
            await call("click", ref=entry_ref, wait_ms=150)
        else:
            await call("click", text="type here", within="hypr-computer test", wait_ms=150)
        text = "zy ZY @#|{} é à ü ö ä € ~ ` 😀 ok"
        await call("type_text", text=text, delay_ms=8, wait_ms=300)
        got = (events("entry", t0) or [{"text": ""}])[-1]["text"]
        check("type_text typed the exact string", got == text, f"got {got!r}")

        # 4. shortcuts: select all + type replaces
        t0 = time.time()
        await call("key", keys="ctrl+a", wait_ms=100)
        await call("type_text", text="replaced", wait_ms=200)
        got = (events("entry", t0) or [{"text": ""}])[-1]["text"]
        check("ctrl+a then typing replaced the text", got == "replaced", f"got {got!r}")
        t0 = time.time()
        await call("key", keys="BackSpace Left Home End shift+Tab F5 XF86Forward", wait_ms=200)
        names = [e["name"] for e in events("key", t0)]
        check("named keys arrive", {"BackSpace", "Left", "Home", "End", "F5"} <= set(names), str(names))

        # 5. pointer on the event pad: find it with OCR, then click/double/right/ctrl-click, hover, drag, scroll
        hits = await call("ocr", query="EVENT PAD", window="hypr-computer test")
        m = re.search(r"screen center \((\d+),(\d+)\)", hits)
        if not m:
            check("OCR found the pad", False, hits)
            return
        px, py = int(m.group(1)) + 150, int(m.group(2)) + 70
        t0 = time.time()
        await call("move_mouse", x=px, y=py, coords="screen", wait_ms=150)
        check("hover sends enter", events("enter", t0))
        t0 = time.time()
        await call("click", x=px, y=py, coords="screen", wait_ms=200)
        pr = events("press", t0)
        check("left click lands", pr and pr[0]["button"] == 1, str(pr[:1]))
        t0 = time.time()
        await call("click", x=px, y=py, coords="screen", count=2, wait_ms=300)
        check("double click", any(e["n"] == 2 for e in events("press", t0)))
        t0 = time.time()
        await call("click", x=px, y=py, coords="screen", button="right", wait_ms=200)
        check("right click", any(e["button"] == 3 for e in events("press", t0)))
        t0 = time.time()
        await call("click", x=px, y=py, coords="screen", modifiers=["ctrl"], wait_ms=200)
        check("ctrl+click carries the modifier", any(e["mods"] & 4 for e in events("press", t0)))
        t0 = time.time()
        await call("click", x=px, y=py, coords="screen", button="middle", wait_ms=200)
        if any(e["button"] == 2 for e in events("press", t0)):
            check("middle click", True)
        else:
            # Phantomat keeps BTN_MIDDLE for canvas panning and never forwards it to windows.
            print("SKIP  middle click did not reach the app (expected on the Phantomat canvas)")

        # Precision: a click at a known offset from the first press must land at that offset.
        base = events("press")[0]
        t0 = time.time()
        await call("click", x=px + 37, y=py + 23, coords="screen", wait_ms=200)
        p2 = events("press", t0)
        if p2:
            dx, dy = p2[0]["x"] - base["x"], p2[0]["y"] - base["y"]
            check("click precision (37,23) offset", abs(dx - 37) <= 1 and abs(dy - 23) <= 1, f"got ({dx},{dy})")

        t0 = time.time()
        await call("drag", from_x=px - 100, from_y=py - 40, to_x=px + 120, to_y=py + 50, coords="screen",
                   duration_ms=400, wait_ms=200)
        de = events("drag-end", t0)
        check("drag distance (220,90)", de and abs(de[0]["dx"] - 220) <= 2 and abs(de[0]["dy"] - 90) <= 2, str(de))

        t0 = time.time()
        await call("scroll", direction="down", amount=2, x=px, y=py, coords="screen", wait_ms=200)
        sc = events("scroll", t0)
        tot = sum(e["dy"] for e in sc)
        check("scroll down on pad", sc and tot > 0, f"dy total {tot} ({len(sc)} events)")
        t0 = time.time()
        await call("scroll", direction="right", amount=1, wait_ms=200)
        sc = events("scroll", t0)
        check("horizontal scroll", sc and sum(e["dx"] for e in sc) > 0, str(sc[:2]))

        # 6. scroll a list found by OCR
        t0 = time.time()
        await call("scroll", direction="down", amount=4, text="row 3", within="hypr-computer test", wait_ms=300)
        ls = events("list-scroll", t0)
        check("scroll list via text target", ls and ls[-1]["value"] > 0, str(ls[-1:]))

        # 7. batch + wait + screenshot_after
        t0 = time.time()
        await call("actions", steps=[{"do": "click", "text": "Press me", "within": "hypr-computer test"},
                                     {"do": "wait", "seconds": 0.2},
                                     {"do": "click", "text": "Press me", "within": "hypr-computer test"}],
                   screenshot_after=True)
        check("actions batch ran both clicks", len(events("button", t0)) == 2)
        r = await call("wait", until_text="presses: 3", window="hypr-computer test", seconds=5)
        check("wait(until_text)", "visible" in r, r)

        if GESTURES:
            await call("move_mouse", x=px, y=py, coords="screen")
            t0 = time.time()
            await call("gesture", kind="pinch", fingers=2, scale=1.8, wait_ms=300)
            check("pinch reaches the app", events("pinch", t0), str(events("pinch", t0)[-1:]))

        await call("release_all")
        await call("window", target="hypr-computer test", action="close", wait_ms=300)

    passed = sum(ok for _, ok in results)
    print(f"\n{passed}/{len(results)} checks passed")


asyncio.run(main())
