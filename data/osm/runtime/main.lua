local root=assert(debug.getinfo(1,'S').source:sub(2):match('^(.*)[/\\]Scripts[/\\]main%.lua$'))
package.path=root..'/Scripts/?.lua;'..package.path
local Json=require('json')
local file=assert(io.open(root..'/Scripts/config.json','rb'))
local config=Json.decode(file:read('*a'));file:close()
local UEHelpers=require('UEHelpers')
local Compat=require('compatibility')
local Init=require('support_init')
local function valid(o) return o and o:IsValid() end
local function unwrap(o) local ok,v=pcall(function() return o:get() end);return ok and v or o end
local function log(s) print('[Creator supports] '..s..'\n') end
local function signature(path,kinds)
    local fn=StaticFindObject(path);assert(valid(fn),'Missing native method '..path)
    local actual={}
    fn:ForEachProperty(function(p) actual[#actual+1]=p:GetClass():GetFName():ToString() end)
    assert(#actual==#kinds,'Changed native parameter count '..path)
    for i,v in ipairs(kinds) do assert(actual[i]==v,'Changed native parameter type '..path) end
end
local checked=false
local build_warning=false
local function contracts()
    if checked then return end
    local cls='/Script/SkiResortSimulator.LiftActor:'
    signature(cls..'Initialize',{'StructProperty','StructProperty'})
    signature(cls..'GenerateTowers',{})
    signature(cls..'Finalize',{})
    signature(cls..'HasCableTerrainCollision',{'BoolProperty'})
    signature('/Script/SkiResortSimulator.PlaceableActor:GetPlaceableId',{'StructProperty'})
    signature('/Script/SkiResortSimulator.PlaceableActor:GetInstanceName',{'TextProperty'})
    signature('/Script/SkiResortSimulator.PlaceableActor:SetInstanceName',{'TextProperty'})
    Compat.require('creator-supports.ground',{functions={
        ['/Script/SkiResortSimulator.SkiResortFunctionLibrary:GetGroundHeightAtLocation']={
            WorldContextObject='ObjectProperty',X='FloatProperty',Y='FloatProperty',ReturnValue='FloatProperty'}},
        function_order={['/Script/SkiResortSimulator.SkiResortFunctionLibrary:GetGroundHeightAtLocation']={
            'WorldContextObject','X','Y','ReturnValue'}}})
    Compat.require('creator-supports.fields',{classes={
        ['/Script/SkiResortSimulator.LiftActor']={Waypoints='ArrayProperty',Towers='ArrayProperty'}}})
    checked=true
end
local library
local function ground(x,y)
    if not valid(library) then library=StaticFindObject('/Script/SkiResortSimulator.Default__SkiResortFunctionLibrary') end
    assert(valid(library),'Ground library unavailable')
    return library:GetGroundHeightAtLocation(UEHelpers.GetWorld(),x,y)
end
local function points(actor)
    local r={};actor.Waypoints:ForEach(function(_,v) r[#r+1]=unwrap(v).position end);return r
end
local function vec(p) return {X=p[1],Y=p[2],Z=ground(p[1],p[2])} end
local controller=Init.new(config,{
    now=os.time,log=log,
    allowed=function()
        local f=io.open(root..'/'..config.game_manifest_relative,'rb')
        if not f then
            if not build_warning then log('DISABLED: Steam build manifest unavailable');build_warning=true end
            return false
        end
        local data=f:read('*a');f:close()
        local ok=data:match('"buildid"%s*"(%d+)"')==config.game_build
        if not ok and not build_warning then log('DISABLED: unsupported Steam build');build_warning=true end
        return ok
    end,
    actors=function(target)
        local world=UEHelpers.GetWorld();if not valid(world) or not world:GetFullName():find(target..'.',1,true) then return {} end
        contracts()
        local result={}
        for _,actor in ipairs(FindAllOf('LiftActor') or {}) do
            if valid(actor) and valid(actor:GetWorld()) and actor:GetWorld():GetAddress()==world:GetAddress() then result[#result+1]=actor end
        end
        return result
    end,
    id=function(actor)
        local g=actor:GetPlaceableId()
        return string.format('%08x%08x%08x%08x',g.A&0xffffffff,g.B&0xffffffff,g.C&0xffffffff,g.D&0xffffffff)
    end,
    key=function(actor) return tostring(actor:GetWorld():GetAddress())..':'..tostring(actor:GetAddress()) end,
    name=function(actor) return actor:GetInstanceName():ToString() end,
    matches=function(actor,d)
        if not actor:GetClass():GetFullName():find(d.class,1,true) then return false end
        local p=points(actor);if #p~=2 then return false end
        for i=1,2 do
            if math.abs(p[i].X-d.terminals_cm[i][1])>1 or math.abs(p[i].Y-d.terminals_cm[i][2])>1 then return false end
        end
        return true
    end,
    ground=ground,
    rebuild=function(actor,d)
        actor:Initialize(vec(d.terminals_cm[1]),vec(d.terminals_cm[2]))
        actor:GenerateTowers()
        actor:Finalize()
    end,
    verify=function(actor)
        local ok=true
        actor.Towers:ForEach(function(_,v)
            local tower=unwrap(v);local p=tower.BasePosition
            local z=ground(p.X,p.Y)
            if type(z)~='number' or z~=z or math.abs(p.Z-z)>200 then ok=false end
        end)
        return ok and not actor:HasCableTerrainCollision()
    end,
    rename=function(actor,name) actor:SetInstanceName(FText(name)) end,
})
local failed=false
local queued=false
LoopAsync(500,function()
    if failed then return true end
    if queued then return false end
    queued=true
    ExecuteInGameThread(function()
        queued=false
        if failed then return end
        local ok,message=pcall(controller.tick)
        if not ok then failed=true;log('DISABLED: '..tostring(message)) end
    end)
    return false
end)
log('Pending authored lifts will initialize only after matching terrain heights are stable')
