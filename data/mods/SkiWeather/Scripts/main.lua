-- SkiWeather: live weather controls for Ski-E-O! (UE4SS Lua mod). F9 opens / closes the panel.
-- Snowfall, storms and temperature scale the map biome's monthly climate table (BiomeSettings.MonthlyClimate:
-- SnowfallRateMultiplier, StormModifier, BaseTemperatureC per month), which the weather simulation reads as it runs.
-- The stock values are remembered and every change is applied to them, so 1x / 0 degrees is the unmodded game.
-- Like SkiDevTools, everything runs on the game thread and the panel's controls are polled (UE4SS Lua can't bind
-- UMG delegates); the panel is plain UMG built inside the game's CardPlaneWidget.
local DIR = debug.getinfo(1, "S").source:gsub("^@", ""):gsub("\\", "/"):match("^(.*/)Scripts/[^/]*$") or "Mods/SkiWeather/"
local S = { snow = 1, storms = 1, temp = 0 }
local KEYS = { "snow", "storms", "temp" }

local function loadSettings()
  local f = io.open(DIR .. "settings.txt", "r"); if not f then return end
  for line in f:lines() do
    local k, v = line:match("^(%w+)%s*=%s*(.-)%s*$")
    if k and S[k] ~= nil then S[k] = tonumber(v) or S[k] end
  end
  f:close()
end
local function save()
  local f = io.open(DIR .. "settings.txt", "w"); if not f then return end
  for _, k in ipairs(KEYS) do f:write(k .. " = " .. tostring(S[k]) .. "\n") end
  f:close()
end
loadSettings()

local function find(name)
  local o = FindFirstOf(name)
  if o and o:IsValid() then return o end
end

---------------------------------------------------------------------------------------------------------------- climate
local stock = {}                                  -- [biome asset path] = { {snowfall, storm, temperature} per month }
local function apply()
  local w = find("WeatherSubsystem"); if not w then return end
  local b = w.CachedBiomeSettings; if not (b and b:IsValid()) then return end
  local key = b:GetFullName()
  local mc = b.MonthlyClimate
  local o = stock[key]
  if not o then
    o = {}
    for i = 1, #mc do o[i] = { mc[i].SnowfallRateMultiplier, mc[i].StormModifier, mc[i].BaseTemperatureC } end
    stock[key] = o
  end
  for i = 1, #mc do
    mc[i].SnowfallRateMultiplier = o[i][1] * S.snow
    mc[i].StormModifier = o[i][2] * S.storms
    mc[i].BaseTemperatureC = o[i][3] + S.temp
  end
end

local function weather(n)
  local w = find("WeatherSubsystem"); if not w then return end
  if n then w:ForceWeather(n) else w:ReleaseForcedWeather() end
  pcall(function() w:SnapWeatherVisualsToCurrent() end)
end

------------------------------------------------------------------------------------------------------------------ panel
local C = {
  panel = { R = 0.006, G = 0.012, B = 0.022, A = 0.94 }, section = { R = 0.25, G = 0.62, B = 0.95, A = 1 },
  text = { R = 0.86, G = 0.89, B = 0.93, A = 1 }, dim = { R = 0.45, G = 0.50, B = 0.56, A = 1 },
  btn = { R = 0.09, G = 0.12, B = 0.16, A = 1 }, red = { R = 0.55, G = 0.07, B = 0.07, A = 1 },
  amber = { R = 0.55, G = 0.33, B = 0.03, A = 1 },
}
local P = { widget = nil, open = false, controls = {}, labels = {} }
local tree
local function new(cn) return StaticConstructObject(StaticFindObject("/Script/UMG." .. cn), tree) end
local function text(s, size, color, bold)
  local t = new("TextBlock")
  t:SetText(FText(s))
  local f = t.Font                                -- edit the default font in place: a partial struct table crashes
  f.Size = size or 10
  f.TypefaceFontName = FName(bold and "Bold" or "Regular")
  t:SetFont(f)
  t:SetColorAndOpacity({ SpecifiedColor = color or C.text, ColorUseRule = 0 })
  return t
end
local function vadd(box, w, top, bottom)
  box:AddChildToVerticalBox(w):SetPadding({ Left = 0, Top = top or 2, Right = 0, Bottom = bottom or 2 })
end
local function hrow(box) local h = new("HorizontalBox"); vadd(box, h); return h end
local function hadd(row, w, fill)
  local s = row:AddChildToHorizontalBox(w)
  s:SetPadding({ Left = 2, Top = 0, Right = 2, Bottom = 0 })
  s:SetVerticalAlignment(2)
  if fill ~= false then s:SetSize({ Value = fill or 1, SizeRule = 1 }) end
end
local function button(row, label, color, fn)
  local b = new("Button")
  b:SetBackgroundColor(color or C.btn)
  local t = text(label, 9, C.text, true); t:SetJustification(1); b:SetContent(t)
  hadd(row, b)
  P.controls[#P.controls + 1] = { kind = "button", w = b, fn = fn, was = false }
end
local function slider(box, title, min, max, step, value, fmt, fn)
  local row = hrow(box)
  hadd(row, text(title, 9, C.text), 0.9)
  local sl = new("Slider")
  sl:SetMinValue(min); sl:SetMaxValue(max); sl:SetStepSize(step); sl:SetValue(value)
  sl:SetSliderBarColor({ R = 0.25, G = 0.30, B = 0.36, A = 1 })
  sl:SetSliderHandleColor({ R = 0.30, G = 0.65, B = 0.95, A = 1 })
  hadd(row, sl, 1.6)
  local val = text(fmt(value), 9, C.text, true); val:SetJustification(2); hadd(row, val, 0.55)
  P.controls[#P.controls + 1] = { kind = "slider", w = sl, fn = fn, last = value, fmt = fmt, val = val }
end
local function label(box, key, s)
  local t = text(s, 9, C.dim); vadd(box, t, 1, 1); P.labels[key] = t
end
local function section(box, title) vadd(box, text(title, 9, C.section, true), 8, 1) end

local function build(pc)
  P.controls, P.labels = {}, {}
  local w = StaticFindObject("/Script/UMG.Default__WidgetBlueprintLibrary"):Create(pc,
    StaticFindObject("/Script/SkiResortSimulator.CardPlaneWidget"), pc)
  tree = w.WidgetTree
  local canvas = new("CanvasPanel")
  canvas:SetVisibility(4)                         -- self hit-test invisible: the rest of the screen stays clickable
  tree.RootWidget = canvas
  local border = new("Border")
  border:SetBrushColor(C.panel)
  border:SetPadding({ Left = 12, Top = 8, Right = 12, Bottom = 10 })
  local cs = canvas:AddChildToCanvas(border)
  cs:SetPosition({ X = 440, Y = 64 })             -- right of the F7 dev tools panel
  cs:SetAutoSize(true)
  local size = new("SizeBox"); size:SetWidthOverride(380); border:SetContent(size)
  local box = new("VerticalBox"); size:SetContent(box)

  local head = hrow(box)
  hadd(head, text("WEATHER CONTROLS", 13, { R = 0.95, G = 0.80, B = 0.25, A = 1 }, true), 1)
  local hint = text("F9", 9, C.dim); hint:SetJustification(2); hadd(head, hint, 0.2)
  label(box, "now", "Load a resort to use these controls.")

  section(box, "WEATHER NOW")
  local r = hrow(box)
  for _, wv in ipairs({ { "Sunny", 0 }, { "Cloudy", 1 }, { "Snowing", 2 }, { "Blizzard", 3 } }) do
    button(r, wv[1], C.btn, function() weather(wv[2]) end)
  end
  button(r, "Natural", C.amber, function() weather(nil) end)

  section(box, "CLIMATE  (1x / 0 = the game's own)")
  slider(box, "Snowfall", 0, 3, 0.05, S.snow, function(v) return string.format("%.2fx", v) end,
    function(v) S.snow = v; apply() end)
  slider(box, "Storms", 0, 2, 0.05, S.storms, function(v) return string.format("%.2fx", v) end,
    function(v) S.storms = v; apply() end)
  slider(box, "Temperature", -10, 10, 0.5, S.temp, function(v) return string.format("%+.1f C", v) end,
    function(v) S.temp = v; apply() end)
  label(box, "help", "Buried in snow? Lower Snowfall, or warm it up so more melts.")

  vadd(box, new("Spacer"), 6, 0)
  r = hrow(box)
  button(r, "Reset to the game's own", C.btn, function()
    S.snow, S.storms, S.temp = 1, 1, 0; apply()
    P.rebuild = true
  end)
  button(r, "Close  (F9)", C.red, function() P.open = false; P.widget:SetVisibility(1) end)

  w:SetVisibility(4)
  w:AddToViewport(901)
  P.widget, P.open = w, true
end

local NAMES = { [0] = "Sunny", "Cloudy", "Snowing", "Blizzard" }
local MONTHS = { "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec" }
local function refresh()
  local t = P.labels.now; if not (t and t:IsValid()) then return end
  local w, ts = find("WeatherSubsystem"), find("TimeSubsystem")
  if not w then t:SetText(FText("Load a resort to use these controls.")); return end
  local m = ts and ts:GetCurrentMonth() or 0
  local mc = w.CachedBiomeSettings.MonthlyClimate
  local row = mc[m + 1]
  t:SetText(FText(string.format("%s, %s%s   snowfall rate %.2f, %.0f C", MONTHS[(m + 6) % 12 + 1] or "?",
    NAMES[w:GetWeatherType()] or "?", w:IsWeatherPinned() and " (forced)" or "",
    row and row.SnowfallRateMultiplier or 0, row and row.BaseTemperatureC or 0)))
end

local function poll()
  for _, c in ipairs(P.controls) do
    if c.w:IsValid() then
      if c.kind == "button" then
        local p = c.w:IsPressed()
        if c.was and not p and c.w:IsHovered() then
          local ok, err = pcall(c.fn); if not ok then print("[SkiWeather] " .. tostring(err) .. "\n") end
          save()
        end
        c.was = p
      else
        local v = c.w:GetValue()
        if v ~= c.last then
          c.last = v; c.val:SetText(FText(c.fmt(v))); c.pending = os.clock()
        elseif c.pending and os.clock() - c.pending > 0.15 then    -- act once the handle settles
          c.pending = nil
          local ok, err = pcall(c.fn, v); if not ok then print("[SkiWeather] " .. tostring(err) .. "\n") end
          save()
        end
      end
    end
  end
  if P.rebuild then                               -- "Reset" moved the values: rebuild so the sliders show them
    P.rebuild = false
    pcall(function() P.widget:RemoveFromParent() end)
    build(FindFirstOf("PlayerController"))
  end
end

local function toggle(pc)
  if not (P.widget and P.widget:IsValid() and P.widget:IsInViewport()) then build(pc); return end
  P.open = not P.open
  P.widget:SetVisibility(P.open and 4 or 1)
end

------------------------------------------------------------------------------------------------------------------ loops
local function guarded(name, fn)
  return function()
    local ok, err = pcall(fn)
    if not ok then print("[SkiWeather] " .. name .. ": " .. tostring(err) .. "\n") end
  end
end
local pc, F9 = nil, { KeyName = FName("F9") }
LoopInGameThreadAfterFrames(2, guarded("input", function()      -- ~30 Hz: F9 + panel input
  if not (pc and pc:IsValid()) then pc = FindFirstOf("PlayerController"); if not (pc and pc:IsValid()) then return end end
  if pc:WasInputKeyJustPressed(F9) then toggle(pc) end
  if P.open and P.widget and P.widget:IsValid() then poll() end
end))
local tick = 0
LoopInGameThreadAfterFrames(30, guarded("climate", function()   -- ~2 Hz: keep the climate set (a loaded map
  tick = tick + 1                                                -- brings its own biome asset), refresh the label
  apply()
  if P.open then refresh() end
end))

print("[SkiWeather] loaded - F9 opens the weather controls\n")

return { toggle = toggle, apply = apply, S = S, P = P }   -- for scripted tests; UE4SS ignores it
