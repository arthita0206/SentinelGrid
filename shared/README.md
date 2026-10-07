# Shared

Common code shared across the simulator, fault injection, and (eventually)
cloud integration layers — e.g. data models, constants, config loading
helpers. Keep this free of AWS SDK calls so it can be imported by purely
local code without pulling in cloud dependencies.

Not yet implemented — this is a placeholder for a future development step.
