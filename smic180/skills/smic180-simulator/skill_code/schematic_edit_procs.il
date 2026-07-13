/*============================================================
 *  Schematic Edit Procedures — 可复用的参数化 SKILL 函数
 *  Date: 2026-04-30
 *  Verified on: LLM_Layout_Design_Lab / Virtuoso via bridge
 *============================================================*/

/*------------------------------------------------------------
 * 打开 cellview (edit 模式)
 *------------------------------------------------------------*/
procedure( schEditOpen(libName cellName)
    let((cv)
        cv = dbOpenCellViewByType(libName cellName "schematic" "schematic" "a")
        if(cv then
            printf("Opened: %s/%s/schematic (%d instances)\n" libName cellName length(cv~>instances))
        else
            printf("ERROR: Failed to open %s/%s/schematic\n" libName cellName)
        )
        cv
    )
)

/*------------------------------------------------------------
 * 放置 instance
 *   schEditPlaceInst("lib" "cell" "masterLib" "masterCell" "instName" x y "R0")
 *------------------------------------------------------------*/
procedure( schEditPlaceInst(libName cellName masterLib masterCell instName xx yy orient)
    let((cv master inst)
        cv = dbOpenCellViewByType(libName cellName "schematic" "schematic" "a")
        master = dbOpenCellViewByType(masterLib masterCell "symbol")
        unless(master
            printf("ERROR: Cannot open master %s/%s/symbol\n" masterLib masterCell)
            return(nil)
        )
        inst = dbCreateInst(cv master instName list(xx yy) orient)
        if(inst then
            printf("Placed: %s (%s/%s) at (%.3f %.3f)\n" instName masterLib masterCell xx yy)
        else
            printf("ERROR: Failed to place %s\n" instName)
        )
        inst
    )
)

/*------------------------------------------------------------
 * 设置 instance CDF 参数
 *   schEditSetParam("lib" "cell" "instName" "paramName" "paramValue")
 *------------------------------------------------------------*/
procedure( schEditSetParam(libName cellName instName paramName paramValue)
    let((cv inst param found)
        cv = dbOpenCellViewByType(libName cellName "schematic" "schematic" "a")
        inst = car(setof(x cv~>instances x~>name==instName))
        unless(inst
            printf("ERROR: Instance %s not found\n" instName)
            return(nil)
        )
        param = car(setof(p cdfGetInstCDF(inst)~>parameters p~>name==paramName))
        if(param then
            param~>value = paramValue
            printf("Set: %s/%s %s.%s = %s\n" libName cellName instName paramName paramValue)
        else
            printf("ERROR: Param %s not found on %s (available: %L)\n"
                paramName instName cdfGetInstCDF(inst)~>parameters~>name)
        )
        param
    )
)

/*------------------------------------------------------------
 * 创建 net
 *   schEditCreateNet("lib" "cell" "netName")
 *------------------------------------------------------------*/
procedure( schEditCreateNet(libName cellName netName)
    let((cv net)
        cv = dbOpenCellViewByType(libName cellName "schematic" "schematic" "a")
        net = dbCreateNet(cv netName)
        if(net then
            printf("Created net: %s\n" netName)
        else
            printf("ERROR: Failed to create net %s\n" netName)
        )
        net
    )
)

/*------------------------------------------------------------
 * 创建 wire
 *   schEditCreateWire("lib" "cell" list(list(x1 y1) list(x2 y2)))
 *------------------------------------------------------------*/
procedure( schEditCreateWire(libName cellName points)
    let((cv wire)
        cv = dbOpenCellViewByType(libName cellName "schematic" "schematic" "a")
        wire = schCreateWire(cv "route" "full" points 0 0 0 nil nil)
        if(wire then
            printf("Created wire with %d points\n" length(points))
        else
            printf("ERROR: Failed to create wire\n")
        )
        wire
    )
)

/*------------------------------------------------------------
 * 创建 wire label
 *   schEditCreateLabel("lib" "cell" x y "labelText" "align")
 *------------------------------------------------------------*/
procedure( schEditCreateLabel(libName cellName xx yy labelText align)
    let((cv label)
        cv = dbOpenCellViewByType(libName cellName "schematic" "schematic" "a")
        label = schCreateWireLabel(cv nil list(xx yy) labelText align "0" "stick" 0.0625 nil)
        if(label then
            printf("Created label: %s at (%.3f %.3f)\n" labelText xx yy)
        else
            printf("ERROR: Failed to create label %s\n" labelText)
        )
        label
    )
)

/*------------------------------------------------------------
 * 创建 pin
 *   schEditCreatePin("lib" "cell" "pinName" "direction" x y "orientation")
 *   direction: "input" / "output" / "inputOutput"
 *   orientation: "left" / "right" / "top" / "bottom"
 *------------------------------------------------------------*/
procedure( schEditCreatePin(libName cellName pinName direction xx yy orient)
    let((cv pin)
        cv = dbOpenCellViewByType(libName cellName "schematic" "schematic" "a")
        pin = schCreatePin(cv nil pinName direction nil list(xx yy) orient)
        if(pin then
            printf("Created pin: %s (%s) at (%.3f %.3f) %s\n" pinName direction xx yy orient)
        else
            printf("ERROR: Failed to create pin %s\n" pinName)
        )
        pin
    )
)

/*------------------------------------------------------------
 * 保存 cellview
 *   schEditSave("lib" "cell")
 *------------------------------------------------------------*/
procedure( schEditSave(libName cellName)
    let((cv)
        cv = dbOpenCellViewByType(libName cellName "schematic" "schematic" "a")
        dbSave(cv)
        printf("Saved: %s/%s/schematic\n" libName cellName)
        t
    )
)
