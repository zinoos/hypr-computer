-- Agent desktop: a nested Hyprland with its own seat, cursor, focus and clipboard.
hl.config({
    general = { gaps_in = 4, gaps_out = 8, border_size = 3 },
    decoration = { rounding = 8 },
    animations = { enabled = true },
    cursor = { no_hardware_cursors = true },
    misc = { disable_hyprland_logo = true, disable_splash_rendering = true },
    input = { kb_layout = "ch", kb_variant = "de" },
})
