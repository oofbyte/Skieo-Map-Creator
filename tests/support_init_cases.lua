-- Run with Lua 5.4 or Fengari. Mocks verify control flow, not game integration.
local Init=require('support_init')
local function case(options)
    options=options or {}
    local actor={}
    local calls={};local clock=0;local ready=false;local title='Preparing lift: Example'
    local config={target_package='/Game/Map',lifts={owned={name='Example',pending_name=title,probes_cm={{0,0,12000}}}}}
    local env={allowed=function()return true end,actors=function()return {actor} end,
      id=function()return options.foreign and 'foreign' or 'owned' end,
      name=function()return title end,key=function()return 'actor' end,now=function()return clock end,
      matches=function()return not options.mismatch end,
      ground=function()return ready and 12000 or 0 end,
      rebuild=function() calls[#calls+1]='Initialize';calls[#calls+1]='GenerateTowers';calls[#calls+1]='Finalize' end,
      verify=function()return not options.invalid end,rename=function(_,s)title=s end,log=function()end}
    if options.completed then title='Example' end
    local c=Init.new(config,env)
    for i=1,8 do c.tick() end
    assert(#calls==0,'Never construct on zero ground')
    ready=true
    if options.timeout then clock=121 end
    for i=1,5 do c.tick() end
    assert(#calls==0,'Requires six stable polls')
    c.tick()
    if options.foreign or options.completed then assert(#calls==0 and title~='Support setup failed')
    elseif options.mismatch or options.timeout then assert(#calls==0 and title=='Support setup failed')
    else
      assert(table.concat(calls,',')=='Initialize,GenerateTowers,Finalize')
      assert(title==(options.invalid and 'Support setup failed' or 'Example'))
    end
    local n=#calls
    for i=1,30 do c.tick() end
    assert(#calls==n,'Completed/failed actors must never regenerate repeatedly')
end
case();case{invalid=true};case{timeout=true};case{mismatch=true};case{foreign=true};case{completed=true}
print('6 initialization cases passed (mocks only)')
