-- Actor preparation, not New Game/load detection. Only explicit pending labels
-- on baked, recipe-owned actors are eligible. Canonical names disable future work.
local Init={}
function Init.new(config,env)
    local states={}
    local function near(a,b,t) return math.abs(a-b)<=t end
    return {tick=function()
        if not env.allowed() then return end
        for _,actor in ipairs(env.actors(config.target_package)) do
            local definition=config.lifts[env.id(actor)]
            if definition and env.name(actor)==definition.pending_name then
                local key=env.key(actor)
                local state=states[key] or {first=env.now(),stable=0}
                states[key]=state
                if not state.finished then
                    local ok,message=pcall(function()
                        assert(env.now()-state.first<120,'Terrain preparation timed out')
                        assert(env.matches(actor,definition),'Actor class or terminal trace differs from recipe')
                        local ready=true
                        for _,p in ipairs(definition.probes_cm) do
                            local z=env.ground(p[1],p[2])
                            if type(z)~='number' or z~=z or not near(z,p[3],200) then ready=false end
                        end
                        state.stable=ready and state.stable+1 or 0
                        if state.stable<6 then return end
                        -- No imported actors, IDs, hubs or save files. Use native construction.
                        state.finished=true -- one attempt; never loop over partially rebuilt geometry
                        env.rebuild(actor,definition)
                        assert(env.verify(actor),'Native support bases or cable collision failed validation')
                        env.rename(actor,definition.name)
                        env.log('Prepared '..definition.name)
                    end)
                    if not ok then
                        state.finished=true
                        env.log('FAILED: '..tostring(message))
                        env.rename(actor,'Support setup failed')
                    end
                end
            end
        end
    end}
end
return Init
