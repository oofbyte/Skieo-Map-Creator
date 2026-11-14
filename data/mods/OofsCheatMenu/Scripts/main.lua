-- Oof's Simple Cheat Menu for Ski-E-O! Ski Resort Tycoon (UE4SS Lua mod). F6 opens / closes the menu.
-- Money, time warp, weather and climate, reputation, guests and "god mode" toggles, on the game's own systems
-- (EconomySubsystem, TimeSubsystem, CalendarSubsystem, WeatherSubsystem, ReputationSubsystem, GuestSimulationSubsystem,
-- EventSubsystem, StaffSettingsDataAsset, the placement validation of the building being placed).
-- Everything runs on the game thread; the menu is plain UMG built inside the game's CardPlaneWidget and its controls are
-- polled (UE4SS Lua can't bind UMG delegates). The toggles keep working while the menu is closed.
local DIR = debug.getinfo(1, "S").source:gsub("^@", ""):gsub("\\", "/"):match("^(.*/)Scripts/[^/]*$")
  or "Mods/OofsCheatMenu/"
local TAG = "[OofsCheatMenu] "
local M = rawget(_G, "OOF_CHEATS") or {}        -- the dev REPL can run this file again: same table, no second loops
_G.OOF_CHEATS = M

local function log(s) print(TAG .. s .. "\n") end
local function guarded(name, fn)
  return function(...)
    local ok, err = pcall(fn, ...)
    if not ok then log(name .. ": " .. tostring(err)) end
  end
end

------------------------------------------------------------------------------------------------------------ settings
local S = M.S or { tab = 1, snow = 1, storms = 1, temp = 0, cashMult = 1, infinite = false, noAccidents = false,
                   noBreakdowns = false, noFatigue = false, anarchy = false }
M.S = S
local KEYS = { "tab", "snow", "storms", "temp", "cashMult", "infinite", "noAccidents", "noBreakdowns", "noFatigue",
               "anarchy" }
local function loadSettings()
  local f = io.open(DIR .. "settings.txt", "r"); if not f then return end
  for line in f:lines() do
    local k, v = line:match("^(%w+)%s*=%s*(.-)%s*$")
    if k and S[k] ~= nil then
      if type(S[k]) == "boolean" then S[k] = (v == "true") else S[k] = tonumber(v) or S[k] end
    end
  end
  f:close()
end
local function save()
  local f = io.open(DIR .. "settings.txt", "w"); if not f then return end
  for _, k in ipairs(KEYS) do f:write(k .. " = " .. tostring(S[k]) .. "\n") end
  f:close()
end
if not M.loaded then loadSettings() end
M.loaded = true

local function valid(o) return o ~= nil and o:IsValid() end
local function find(name)
  local o = FindFirstOf(name)
  if valid(o) then return o end
end
local function commas(n)
  local s = string.format("%d", math.floor(n + 0.5))
  local neg = s:sub(1, 1) == "-"
  if neg then s = s:sub(2) end
  s = s:reverse():gsub("(%d%d%d)", "%1,"):reverse():gsub("^,", "")
  return (neg and "-" or "") .. s
end

--------------------------------------------------------------------------------------------------------------- money
local INFINITE_FLOOR = 100000000                 -- "infinite money" keeps the balance at least this high
local MAX_BALANCE = 2000000000                   -- the balance is an int32 (2,147,483,647): stay clear of wrapping
local function econ() return find("EconomySubsystem") end
local function addCash(n)
  local e = econ(); if not e then return end
  n = math.min(n, MAX_BALANCE - e:GetBalance())
  if n > 0 then e:AddFunds(n) end
  S.lastBalance = e:GetBalance()                 -- our own gifts are not income for the cash multiplier
end
local function payOffLoans()                     -- the money to repay them is a gift too
  local e = econ(); if not e then return end
  addCash(e:GetTotalLoanBalance())
  local loans = e:GetActiveLoans()
  for i = #loans, 1, -1 do e:RepayLoan(i - 1) end
  S.lastBalance = e:GetBalance()
end

-- cash multiplier: every rise in the balance (income) is topped up by (multiplier - 1); loans taken are left alone
local lastLoan
local function moneyTick()
  local e = econ(); if not e then S.lastBalance = nil; return end
  local bal, loan = e:GetBalance(), e:GetTotalLoanBalance()
  if S.lastBalance and S.cashMult > 1 and bal > S.lastBalance and loan == lastLoan then
    local extra = math.min(math.floor((bal - S.lastBalance) * (S.cashMult - 1)), MAX_BALANCE - bal)
    if extra > 0 then e:AddFunds(extra); bal = e:GetBalance() end
  end
  if S.infinite and bal < INFINITE_FLOOR then
    e:AddFunds(INFINITE_FLOOR - bal); bal = e:GetBalance()
  end
  S.lastBalance, lastLoan = bal, loan
end

---------------------------------------------------------------------------------------------------------------- time
local SPEEDS = { 1, 2, 5, 10, 25, 50, 100 }
local function timeSys() return find("TimeSubsystem") end
local function setSpeed(rate)
  local ts = timeSys(); if not ts then return end
  if rate > 10 then                              -- the game's own buttons stop at 10x; the world caps time dilation
    for _, w in ipairs(FindAllOf("WorldSettings") or {}) do pcall(function() w.MaxGlobalTimeDilation = 1000 end) end
  end
  ts:ResumeTime()
  ts:SetTimeRate(rate)
end

-- the calendar runs one month over DaysPerMonth in-game days (Advanced options, 1 by default)
local function daysPerMonth()
  local n = 1
  pcall(function() n = timeSys():GetDaysPerMonth() end)
  return (n and n > 0) and n or 1
end
local function skipHours(h)                      -- forward only, at most to the end of this month
  local cal = find("CalendarSubsystem"); if not cal then return end
  cal:SetMonthProgress(math.min(cal:GetMonthProgress() + h / (24 * daysPerMonth()), 0.9995))
end
local function setHour(h)                        -- time of day, today (DayNightPhase 0..1 = 00:00..24:00)
  local ts = timeSys(); if ts then ts:SetDayNightPhase(h / 24) end
end
local function sunset()
  local h = 17
  pcall(function() h = timeSys():GetSunsetHour() end)
  return h
end

------------------------------------------------------------------------------------------------------------- weather
local function weatherSys() return find("WeatherSubsystem") end
local function weather(n)
  local w = weatherSys(); if not w then return end
  if n then w:ForceWeather(n) else w:ReleaseForcedWeather() end
  pcall(function() w:SnapWeatherVisualsToCurrent() end)
end

-- climate: snowfall, storms and temperature scale the map biome's monthly climate table, which the weather simulation
-- reads as it runs; the stock values are remembered, so 1x / 0 degrees is the unmodded game
local stock = {}
local function applyClimate()
  local w = weatherSys(); if not w then return end
  local b = w.CachedBiomeSettings; if not valid(b) then return end
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

-- the weather's own year-change handler: a new "year personality" (the kind of winter) is rolled
local function rerollWinter()
  local w, cal = weatherSys(), find("CalendarSubsystem"); if not (w and cal) then return end
  w:OnYearChanged(cal:GetYearNumber())
  pcall(function() w:SnapWeatherVisualsToCurrent() end)
end

---------------------------------------------------------------------------------------------------------- reputation
local function rep() return find("ReputationSubsystem") end
local function setLevel(lv)
  local r = rep(); if not r then return end
  r:CheatSetLevel(math.max(0, math.min(lv, r:GetMaxReachableLevel())))
end

-------------------------------------------------------------------------------------------------------------- guests
-- the game's own spawner (its arrival point); if it spawns nobody (arrivals closed), at the bottom of the first lift
local function spawnGuests(n)
  local sim = find("GuestSimulationSubsystem"); if not sim then return end
  if not sim:IsSimulationEnabled() then return end       -- not open yet (no ticket office): guests would stand still
  local before = sim:GetGuestCount()
  local sp = sim:GetSpawner()
  if valid(sp) then
    local cfg = sp.Config
    local old = { MaxGuests = cfg.MaxGuests, SpawnBatchSize = cfg.SpawnBatchSize, SpawnInterval = cfg.SpawnInterval,
                  SnowboarderFraction = cfg.SnowboarderFraction }
    sp:SetConfig({ MaxGuests = math.max(old.MaxGuests, before + n), SpawnBatchSize = n, SpawnInterval = old.SpawnInterval,
                   SnowboarderFraction = old.SnowboarderFraction })
    sp:SpawnBatch()
    sp:SetConfig(old)
  end
  if sim:GetGuestCount() == before then
    local lifts = FindAllOf("LiftActor") or {}
    if lifts[1] then
      local l = lifts[1]:GetBottomTerminalLocation()
      sim:SpawnGuests({ X = l.X, Y = l.Y, Z = l.Z }, n)
    end
  end
end

------------------------------------------------------------------------------------------------------------ god mode
-- accidents (skier injuries) and breakdowns (lifts, buildings, groomers) are cancelled as they start
local INJURY, BREAKDOWNS = 0, { [3] = true, [6] = true, [7] = true, [17] = true, [18] = true }
local seen = {}
local function eventsTick()
  if not (S.noAccidents or S.noBreakdowns) then return end
  local ev = find("EventSubsystem"); if not ev then return end
  local list = ev:GetActiveEvents()
  local now = {}
  for i = 1, #list do
    local e = list[i]:get()
    local id = e.EventId
    local key = string.format("%x-%x-%x-%x", id.A, id.B, id.C, id.D)
    now[key] = true
    if not seen[key] then
      seen[key] = true
      local t = e.EventType
      if (t == INJURY and S.noAccidents) or (BREAKDOWNS[t] and S.noBreakdowns) then
        pcall(function() ev:TryCancelEvent(id) end)
      end
    end
  end
  for k in pairs(seen) do if not now[k] then seen[k] = nil end end
end

local FATIGUE = { "BaseStaminaDecayPerHour", "EventHandlingExtraDecayPerHour", "HurryingExtraDecayPerHour" }
local staffOrig
local function staffTick()
  local st = find("StaffSettingsDataAsset"); if not st then return end
  if not staffOrig then staffOrig = {}; for _, k in ipairs(FATIGUE) do staffOrig[k] = st[k] end end
  for _, k in ipairs(FATIGUE) do
    local want = S.noFatigue and 0 or staffOrig[k]
    if st[k] ~= want then st[k] = want end
  end
end

-- build anywhere: the placement checks of the building being placed or moved are relaxed (and put back when off)
local RELAXED = { MaxSlopeAngle = 89.0, ConformingMaxSlopeAngle = 89.0, AllowedSurfaceTypes = 0x7FFFFFFF,
                  bCheckObstacles = false, bCheckOtherPlaceables = false }
local relaxed, pm, mv, searchIn = {}, nil, nil, 0
local function relax(actor)
  if not valid(actor) then return end
  local ok, v = pcall(function() return actor.ValidationComponent end)
  if not (ok and valid(v)) then return end
  local key = v:GetFullName()
  if not relaxed[key] then
    local o = {}
    for k in pairs(RELAXED) do o[k] = v[k] end
    relaxed[key] = { comp = v, values = o }
  end
  for k, want in pairs(RELAXED) do if v[k] ~= want then v[k] = want end end
end
local function anarchyTick()
  if not S.anarchy then
    for key, e in pairs(relaxed) do
      if valid(e.comp) then for k, val in pairs(e.values) do pcall(function() e.comp[k] = val end) end end
      relaxed[key] = nil
    end
    return
  end
  if not (valid(pm) and valid(mv)) then          -- FindFirstOf walks every object: look at most once a second
    searchIn = searchIn - 1
    if searchIn <= 0 then
      searchIn = 30
      if not valid(pm) then pm = FindFirstOf("PlacementManagerComponent") end
      if not valid(mv) then mv = FindFirstOf("MovePlaceableComponent") end
    end
  end
  if valid(pm) then relax(pm.BuildingPreviewActor) end
  if valid(mv) then relax(mv.HeldActor) end
  for key, e in pairs(relaxed) do if not valid(e.comp) then relaxed[key] = nil end end
end

----------------------------------------------------------------------------------------------------------------- menu
local C = {
  panel = { R = 0.006, G = 0.012, B = 0.022, A = 0.94 }, section = { R = 0.25, G = 0.62, B = 0.95, A = 1 },
  text = { R = 0.86, G = 0.89, B = 0.93, A = 1 }, dim = { R = 0.45, G = 0.50, B = 0.56, A = 1 },
  title = { R = 0.95, G = 0.80, B = 0.25, A = 1 },
  btn = { R = 0.09, G = 0.12, B = 0.16, A = 1 }, on = { R = 0.05, G = 0.38, B = 0.14, A = 1 },
  red = { R = 0.55, G = 0.07, B = 0.07, A = 1 }, amber = { R = 0.55, G = 0.33, B = 0.03, A = 1 },
  blue = { R = 0.05, G = 0.22, B = 0.50, A = 1 },
}
local TABS = { "Money", "Time", "Weather", "Resort" }
local P = M.P or { widget = nil, open = false }
M.P = P
P.controls, P.labels = P.controls or {}, P.labels or {}

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
local function button(row, label, color, fn, fill)
  local b = new("Button")
  b:SetBackgroundColor(color or C.btn)
  local t = text(label, 9, C.text, true); t:SetJustification(1); b:SetContent(t)
  hadd(row, b, fill)
  P.controls[#P.controls + 1] = { kind = "button", w = b, fn = fn, was = false, name = label }
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
  P.controls[#P.controls + 1] = { kind = "slider", w = sl, fn = fn, last = value, fmt = fmt, val = val, name = title }
end
local function check(box, title, value, fn)
  local row = hrow(box)
  local cb = new("CheckBox")
  cb:SetIsChecked(value and true or false)
  hadd(row, cb, false)
  hadd(row, text(title, 9, C.text), 1)
  P.controls[#P.controls + 1] = { kind = "check", w = cb, fn = fn, last = value and true or false, name = title }
end
local function label(box, key, s, color)
  local t = text(s, 9, color or C.dim); t:SetAutoWrapText(true); vadd(box, t, 1, 1); P.labels[key] = t
end
local function section(box, title) vadd(box, text(title, 9, C.section, true), 8, 1) end

local BUILD = {}
BUILD[1] = function(box)                          -- Money
  label(box, "cash", "Cash: -", C.text)
  section(box, "ADD CASH")
  local r = hrow(box)
  button(r, "+$100K", C.btn, function() addCash(100000) end)
  button(r, "+$1M", C.btn, function() addCash(1000000) end)
  button(r, "+$10M", C.btn, function() addCash(10000000) end)
  button(r, "+$100M", C.on, function() addCash(100000000) end)
  r = hrow(box)
  button(r, "Pay off all loans (free)", C.btn, payOffLoans)
  section(box, "CHEATS")
  check(box, "Infinite money (never below $100M)", S.infinite, function(v) S.infinite = v; moneyTick() end)
  slider(box, "Income multiplier", 1, 10, 0.5, S.cashMult, function(v) return string.format("%gx", v) end,
    function(v) S.cashMult = v end)
  label(box, "help", "Income multiplier: every dollar the resort earns counts that many times.")
end
BUILD[2] = function(box)                          -- Time
  label(box, "time", "Date: -", C.text)
  section(box, "TIME WARP")
  local r = hrow(box)
  for _, v in ipairs(SPEEDS) do button(r, v .. "x", v > 10 and C.amber or C.btn, function() setSpeed(v) end) end
  r = hrow(box)
  button(r, "Pause", C.btn, function() local ts = timeSys(); if ts then ts:PauseTime() end end)
  button(r, "Resume", C.btn, function() local ts = timeSys(); if ts then ts:ResumeTime() end end)
  section(box, "SKIP AHEAD")
  r = hrow(box)
  button(r, "+1 hour", C.btn, function() skipHours(1) end)
  button(r, "+6 hours", C.btn, function() skipHours(6) end)
  button(r, "Next day", C.btn, function() skipHours(24) end)
  button(r, "Next month", C.btn, function() skipHours(24 * daysPerMonth()) end)
  section(box, "TIME OF DAY")
  r = hrow(box)
  button(r, "Morning", C.btn, function() setHour(8) end)
  button(r, "Noon", C.btn, function() setHour(12) end)
  button(r, "Sunset", C.btn, function() setHour(sunset()) end)
  button(r, "Night", C.btn, function() setHour(22) end)
  local ts, held = timeSys(), false
  if ts then pcall(function() held = ts:IsTimeOfDayHeld() end) end
  check(box, "Freeze the clock (the time of day stays put)", held,
    function(v) local t = timeSys(); if t then t:SetTimeOfDayHeld(v) end end)
  label(box, "help", "Orange speeds go past the game's own 10x; guests and lifts may glitch there.")
end
BUILD[3] = function(box)                          -- Weather
  label(box, "weather", "Weather: -", C.text)
  section(box, "WEATHER NOW")
  local r = hrow(box)
  for _, wv in ipairs({ { "Sunny", 0 }, { "Cloudy", 1 }, { "Snowing", 2 }, { "Blizzard", 3 } }) do
    button(r, wv[1], C.btn, function() weather(wv[2]) end)
  end
  button(r, "Natural", C.amber, function() weather(nil) end)
  section(box, "CLIMATE  (1x / 0 = the game's own)")
  slider(box, "Snowfall", 0, 3, 0.05, S.snow, function(v) return string.format("%.2fx", v) end,
    function(v) S.snow = v; applyClimate() end)
  slider(box, "Storms", 0, 2, 0.05, S.storms, function(v) return string.format("%.2fx", v) end,
    function(v) S.storms = v; applyClimate() end)
  slider(box, "Temperature", -10, 10, 0.5, S.temp, function(v) return string.format("%+.1f C", v) end,
    function(v) S.temp = v; applyClimate() end)
  r = hrow(box)
  button(r, "Reset climate", C.btn, function() S.snow, S.storms, S.temp = 1, 1, 0; applyClimate(); P.rebuild = true end)
  button(r, "Re-roll this winter", C.btn, rerollWinter)
  label(box, "help", "Buried in snow? Lower Snowfall, or warm it up so more melts.")
end
BUILD[4] = function(box)                          -- Resort
  label(box, "rep", "Reputation: -", C.text)
  local r = hrow(box)
  button(r, "Level up", C.blue, function() local x = rep(); if x then setLevel(x:GetCurrentLevel() + 1) end end)
  button(r, "Max level (unlock all)", C.blue, function() local x = rep(); if x then setLevel(x:GetMaxReachableLevel()) end end)
  button(r, "+1,000 XP", C.btn, function() local x = rep(); if x then x:AddXP(1000, 0) end end)
  section(box, "GUESTS")
  label(box, "guests", "Guests: -")
  r = hrow(box)
  button(r, "+100 guests", C.btn, function() spawnGuests(100) end)
  button(r, "+1,000 guests", C.btn, function() spawnGuests(1000) end)
  section(box, "GOD MODE")
  check(box, "No accidents (skier injuries)", S.noAccidents, function(v) S.noAccidents = v end)
  check(box, "No breakdowns (lifts, buildings, groomers)", S.noBreakdowns, function(v) S.noBreakdowns = v end)
  check(box, "Staff never get tired", S.noFatigue, function(v) S.noFatigue = v; staffTick() end)
  check(box, "Build anywhere (slopes, trees, overlapping)", S.anarchy, function(v) S.anarchy = v end)
end

local function setLabel(k, s) local t = P.labels[k]; if valid(t) then t:SetText(FText(s)) end end
local MONTHS = { "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec" }
local WEATHERS = { [0] = "Sunny", "Cloudy", "Snowing", "Blizzard" }
local function refresh()
  local e = econ()
  setLabel("status", e and "" or "Load a resort to use the cheats.")
  if valid(P.labels.status) then P.labels.status:SetVisibility(e and 1 or 4) end     -- 1 = collapsed
  if e then
    setLabel("cash", string.format("Cash: $%s%s", commas(e:GetBalance()),
      e:GetTotalLoanBalance() > 0 and ("    loans: $" .. commas(e:GetTotalLoanBalance())) or ""))
  end
  local ts = timeSys()
  if ts then
    local h = ts:GetTimeOfDayHours()
    setLabel("time", string.format("%s  %02d:%02d    speed %gx%s", MONTHS[(ts:GetCurrentMonth() + 6) % 12 + 1] or "?",
      math.floor(h), math.floor((h % 1) * 60), ts:GetTimeRate(),
      (ts:IsTimePaused() and "   PAUSED" or "") .. (ts:IsTimeOfDayHeld() and "   CLOCK FROZEN" or "")))
  end
  local w = weatherSys()
  if w then
    local pers = ""
    pcall(function() pers = w:GetPersonalityDisplayName():ToString() end)
    setLabel("weather", string.format("%s%s    this winter: %s", WEATHERS[w:GetWeatherType()] or "?",
      w:IsWeatherPinned() and " (forced)" or "", pers ~= "" and pers or "normal"))
  end
  local r = rep()
  if r then
    local name = ""
    pcall(function() name = r:GetLevelDisplayName():ToString() end)
    setLabel("rep", string.format("%s (level %d of %d)    XP %s", name, r:GetCurrentLevel() + 1,
      r:GetMaxReachableLevel() + 1, commas(r:GetCurrentXP())))
  end
  local sim = find("GuestSimulationSubsystem")
  if sim then
    setLabel("guests", string.format("Guests on the hill: %s%s", commas(sim:GetGuestCount()),
      sim:IsSimulationEnabled() and "" or "    (guests come once the resort has a ticket office and a lift)"))
  end
end
M.refresh = refresh

function M.build(pc)
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
  cs:SetPosition({ X = 440, Y = 64 })
  cs:SetAutoSize(true)
  local size = new("SizeBox"); size:SetWidthOverride(400); border:SetContent(size)
  local box = new("VerticalBox"); size:SetContent(box)

  local head = hrow(box)
  hadd(head, text("OOF'S SIMPLE CHEAT MENU", 13, C.title, true), 1)
  local hint = text("F6", 9, C.dim); hint:SetJustification(2); hadd(head, hint, 0.15)
  label(box, "status", "")
  local r = hrow(box)
  for i, name in ipairs(TABS) do
    button(r, name, i == S.tab and C.blue or C.btn, function() S.tab = i; P.rebuild = true end)
  end
  vadd(box, new("Spacer"), 2, 0)
  BUILD[S.tab](box)
  vadd(box, new("Spacer"), 6, 0)
  r = hrow(box)
  button(r, "Close  (F6)", C.red, function() P.open = false; P.widget:SetVisibility(1) end)

  w:SetVisibility(4)
  w:AddToViewport(901)
  P.widget, P.open = w, true
  refresh()
end

local function poll()
  for _, c in ipairs(P.controls) do
    if valid(c.w) then
      if c.kind == "button" then
        local p = c.w:IsPressed()
        if c.was and not p and c.w:IsHovered() then
          local ok, err = pcall(c.fn); if not ok then log(tostring(err)) end
          save()
        end
        c.was = p
      elseif c.kind == "slider" then
        local v = c.w:GetValue()
        if v ~= c.last then
          c.last = v; c.val:SetText(FText(c.fmt(v))); c.pending = os.clock()
        elseif c.pending and os.clock() - c.pending > 0.15 then    -- act once the handle settles
          c.pending = nil
          local ok, err = pcall(c.fn, v); if not ok then log(tostring(err)) end
          save()
        end
      else
        local v = c.w:IsChecked()
        if v ~= c.last then
          c.last = v
          local ok, err = pcall(c.fn, v); if not ok then log(tostring(err)) end
          save()
        end
      end
    end
  end
  if P.rebuild then                               -- tab switch / reset: build the menu again
    P.rebuild = false
    pcall(function() P.widget:RemoveFromParent() end)
    M.build(FindFirstOf("PlayerController"))
  end
end

function M.toggle(pc)
  if not (valid(P.widget) and P.widget:IsInViewport()) then M.build(pc); return end
  P.open = not P.open
  P.widget:SetVisibility(P.open and 4 or 1)
  if P.open then refresh() end
end

------------------------------------------------------------------------------------------------------------- loops
local pc, F6, frame = nil, { KeyName = FName("F6") }, 0
function M.input()                                -- every frame: "just pressed" lasts one frame
  if not valid(pc) then pc = FindFirstOf("PlayerController"); if not valid(pc) then return end end
  if pc:WasInputKeyJustPressed(F6) then M.toggle(pc) end
  frame = frame + 1
  if frame % 2 == 1 then return end               -- the rest every other frame is plenty (a click lasts ~0.1 s)
  anarchyTick()
  if P.open and valid(P.widget) then poll() end
end
local tick = 0
function M.background()                           -- 4 times a second: the toggles keep working with the menu closed
  tick = tick + 1
  pcall(moneyTick)
  pcall(eventsTick)
  if tick % 2 == 0 then pcall(applyClimate) end
  if tick % 8 == 0 then pcall(staffTick) end
  if tick % 2 == 1 and P.open and valid(P.widget) then pcall(refresh) end
end

if not M.started then
  M.started = true
  LoopInGameThreadAfterFrames(1, guarded("input", function() M.input() end))
  LoopInGameThreadAfterFrames(15, guarded("background", function() M.background() end))

  -- dev REPL, only with Mods/OofsCheatMenu/dev.txt: runs D:/SkiBuild/repl/in.lua when it changes, output to out.txt
  local dev = io.open(DIR .. "dev.txt", "r")
  if dev then
    dev:close()
    local REPL_IN, REPL_OUT, lastSrc = "D:/SkiBuild/repl/in.lua", "D:/SkiBuild/repl/out.txt", nil
    do local f0 = io.open(REPL_IN, "rb"); if f0 then lastSrc = f0:read("*a"); f0:close() end end
    LoopInGameThreadAfterFrames(20, guarded("repl", function()
      local f = io.open(REPL_IN, "rb"); if not f then return end
      local src = f:read("*a"); f:close()
      if src == lastSrc then return end
      lastSrc = src
      local lines = {}
      local env = setmetatable({ M = M, S = S, P = P, DIR = DIR, out = function(...)
        local t = {} for i = 1, select("#", ...) do t[#t + 1] = tostring(select(i, ...)) end
        lines[#lines + 1] = table.concat(t, " ") end }, { __index = _G })
      local fn, err = load(src, "repl", "t", env)
      local ok, res = false, err
      if fn then ok, res = pcall(fn) end
      if not ok then lines[#lines + 1] = "ERROR: " .. tostring(res) end
      local o = io.open(REPL_OUT, "w"); o:write(table.concat(lines, "\n") .. "\n--done--\n"); o:close()
    end))
    log("dev REPL on")
  end
  log("loaded - F6 opens the cheat menu")
end

return M
