# Keep Lite defaults and make advanced configuration opt-in

The project's core is Spectre execution plus PDK/CDF callback refresh, targeting
GUI-equivalent electrical simulation with the same netlist, models, and options.
Lightweight schematic/symbol manipulation and GDS operations cover common work;
the SKILL execution interface remains the escape hatch for uncommon tasks.

Multi-server, multi-account, and process/revision/variant configurations are
optional extensions, not the default model. Existing one-host configuration and
Python calls remain sufficient: no new required metadata, matrix file, profile
selection, or automatic matrix/PDK identity gate. Keep advanced auditing behind
explicit commands. Configuration-declared process versions are not evidence of
the version loaded by an existing CIW; any future runtime identity mechanism
needs an explicit, trustworthy source and opt-in policy.

This deliberately favors a small default interface over a general EDA workflow
orchestration framework. New common-operation wrappers should simplify recurring
work while preserving the SKILL escape hatch, not create a second workflow DSL.
