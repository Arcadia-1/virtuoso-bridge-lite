/*============================================================
 *  set_inst_params.il
 *  Set multiple CDF parameters on a named instance in one call.
 *
 *  Usage:
 *    setInstParams("lib" "cell" "I0" list("vdc" "1.8" "acm" "1"))
 *    setInstParams("lib" "cell" "C0" list("c" "10p"))
 *
 *  Opens the schematic in append mode, finds the instance,
 *  sets each param, and saves.  Headless-safe — no GUI deps.
 *============================================================*/
procedure( setInstParams(libName cellName instName paramPairs)
    let((cv inst param i n key val)
        cv = dbOpenCellViewByType(libName cellName "schematic" "schematic" "a")
        unless(cv
            error("setInstParams: cannot open %s/%s/schematic\n" libName cellName)
        )
        inst = car(setof(x cv~>instances x~>name == instName))
        unless(inst
            error("setInstParams: instance %s not found in %s/%s\n" instName libName cellName)
        )
        n = length(paramPairs)
        for(i 0 truncate((n - 1) / 2.0)
            key = nthelem(i*2 + 1 paramPairs)
            val = nthelem(i*2 + 2 paramPairs)
            param = car(setof(p cdfGetInstCDF(inst)~>parameters p~>name == key))
            if(param then
                param~>value = val
                printf("SET-PARAM: %s/%s/%s  %s.%s = %s\n"
                       libName cellName instName instName key val)
            else
                printf("WARNING: param %s not found on %s (available: %L)\n"
                       key instName cdfGetInstCDF(inst)~>parameters~>name)
            )
        )
        dbSave(cv)
        t
    )
)
