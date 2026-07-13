# SMIC180 Live Discovery

A confirmed technology profile can only be produced from live Virtuoso and PDK
evidence. Discovery must resolve the active PDK root and `cds.lib`, enumerate
candidate masters and views, read master terminals, inspect instance CDF
parameters and callbacks, and perform disposable create/save/close/reopen
round trips.

Every confirmed device adapter cites separate master, terminal, and CDF evidence.
The profile also records model include files, sections, corner mapping, legal or
observed parameter limits, bulk policy, and direct-versus-`si` netlist
normalization. Missing evidence leaves the adapter unresolved and blocks live
materialization.

The repository currently contains two possible PDK roots:
`/home/IC/Tech/smic18ee_2` and `/home/IC/Tech/smic18ee_2P6M_20100810`.
Discovery must report and resolve that conflict instead of choosing silently.
