-- Optional Hyprland helper for hypr-computer (https://github.com/zinoos/hypr-computer).
-- Load it from hyprland.lua:  dofile(os.getenv("HOME") .. "/.config/hypr/agent-places.lua")
--
-- Agent desktops: places (workspaces) 4…0 belong to AI agents, 1…3 stay yours. Each agent works
-- in its own nested Hyprland (own cursor, keyboard focus, clipboard, apps) that renders headless; a capture-only viewer (class dev.hypr_computer.viewer)
-- sits full-size in the agent's place, so Super+4 shows what agent 4 is doing right now. Your
-- mouse and keyboard can't reach an agent desktop, and agents can't use yours.
--
-- hypr-computer calls AgentPlaces.claim(pid, place, monitor) right after it starts the nested
-- Hyprland. When that pid's window opens, it goes straight into its place: it never splits
-- the place you are on and never takes your keyboard focus.
-- Safe to dofile() again (hyprctl eval) without a reload: handlers are registered once.

AgentPlaces = AgentPlaces or {}
local A = AgentPlaces
A.pending = A.pending or {} -- pid -> { place, monitor }: windows to put in an agent place
A.viewers = A.viewers or {} -- viewer address -> place
local GAP_OUT = 18 -- outer gap around a placed viewer; match your layout's gaps

-- The nested Hyprland's own boot window (closed right after start: it would forward your input) and
-- the capture-only viewer: neither takes your focus.
hl.window_rule({
    name = "agent-viewer",
    match = { class = "^(aquamarine|dev\\.hypr_computer\\.viewer)$" },
    no_initial_focus = true,
})

-- The tiling area of a place on a screen, in canvas coordinates ({x, y, w, h}), or nil when the
-- Phantomat canvas isn't running. monitor defaults to the anchor screen (the one at 0,0).
function A.area(place, monitor)
    local so = hl.plugin.spatialoverview
    local ok, p = pcall(function() return so.places() end)
    if not (ok and p and p.screens and #p.screens > 0) then return nil end
    local s
    for _, m in ipairs(p.screens) do
        if (monitor and m.name == monitor) or (not monitor and math.abs(m.x) < 1 and math.abs(m.y) < 1) then s = m end
    end
    s = s or p.screens[1]
    local r = {}
    for _, m in ipairs(hl.get_monitors()) do
        if m.name == s.name then r = m.reserved or {} end
    end
    return {
        x = s.x + (place - 1) * p.step + GAP_OUT + (r.left or 0),
        y = s.y + GAP_OUT + (r.top or 0),
        w = s.w - 2 * GAP_OUT - (r.left or 0) - (r.right or 0),
        h = s.h - 2 * GAP_OUT - (r.top or 0) - (r.bottom or 0),
        monitor = s.name,
    }
end

function A.claim(pid, place, monitor)
    A.pending[pid] = { place = place, monitor = monitor }
    return A.area(place, monitor)
end

local function claimed(win)
    return win and win.pid and A.pending[win.pid]
end

-- Before the tiler sees it: keep it out of the place you're looking at.
function A.on_open_early(win)
    local c = claimed(win)
    if c and CanvasTiling and CanvasTiling.free then CanvasTiling.free[win.address] = true end
end

-- After the tiler's own handler: put it in its place and let the tiler fill the place with it.
function A.on_open(win)
    local c = claimed(win)
    if not c then return end
    A.pending[win.pid] = nil
    A.viewers[win.address] = c.place
    if CanvasTiling and CanvasTiling.free then CanvasTiling.free[win.address] = true end
    local a = A.area(c.place, c.monitor)
    local target = "address:" .. win.address
    if a then
        -- Phantomat puts a new window at the view centre when it takes it in. A plugin build with a
        -- "canvas tile" dispatcher puts it straight in its place; otherwise it's resized and moved there.
        local so = hl.plugin.spatialoverview
        local ok = false
        if so and so._dispatch then
            ok = pcall(so._dispatch, "canvas",
                string.format("tile %s %d %d %d %d open", win.address, a.x, a.y, a.w, a.h))
        end
        if not ok then
            hl.dispatch(hl.dsp.window.resize({ x = a.w, y = a.h, relative = false, window = target }))
            hl.dispatch(hl.dsp.window.move({ x = a.x, y = a.y, window = target }))
        end
        hl.timer(function()
            if CanvasTiling and CanvasTiling.free then CanvasTiling.free[win.address] = nil end
            if CanvasTiling and CanvasTiling.retile then pcall(CanvasTiling.retile) end
        end, { timeout = 400, type = "oneshot" })
    else
        -- No canvas: plain Hyprland workspaces 4…10.
        hl.dispatch(hl.dsp.window.move({ workspace = tostring(c.place), follow = false, window = target }))
    end
end

function A.on_close(win)
    if win and win.address then A.viewers[win.address] = nil end
end

-- Registered once; they call through A so re-running this file updates the behaviour.
if not A.hooked2 then
    A.hooked2 = true
    hl.on("window.open_early", function(win) A.on_open_early(win) end)
    hl.on("window.open", function(win) A.on_open(win) end)
    hl.on("window.close", function(win) A.on_close(win) end)
end

-- Put an existing window (an agent viewer) into a place, filling it. Returns the area or nil.
function A.put(address, place, monitor)
    local a = A.area(place, monitor)
    if not a then return nil end
    A.viewers[address] = place
    local so = hl.plugin.spatialoverview
    if CanvasTiling and CanvasTiling.free then CanvasTiling.free[address] = true end
    pcall(so._dispatch, "canvas", string.format("tile %s %d %d %d %d", address, a.x, a.y, a.w, a.h))
    hl.timer(function()
        if CanvasTiling and CanvasTiling.free then CanvasTiling.free[address] = nil end
        if CanvasTiling and CanvasTiling.retile then pcall(CanvasTiling.retile) end
    end, { timeout = 300, type = "oneshot" })
    return a
end
