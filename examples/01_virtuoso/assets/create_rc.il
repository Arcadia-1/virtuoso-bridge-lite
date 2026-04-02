; create_rc.il
;
; Procedures:
;   SchCreateRC(libName cellName)  - create RC circuit (VDC + R + C), open window
;
; Depends on: schematic_ops.il (for _SchLabelTerm)

procedure(SchCreateRC(libName cellName)
  let((cv vdcM resM capM V1 R1 C1)
    cv = dbOpenCellViewByType(libName cellName "schematic" "schematic" "a")
    unless(cv return(sprintf(nil "ERROR: cannot open %s/%s/schematic" libName cellName)))

    vdcM = dbOpenCellView(_SchAnalogLib "vdc" "symbol")
    resM = dbOpenCellView(_SchAnalogLib "res" "symbol")
    capM = dbOpenCellView(_SchAnalogLib "cap" "symbol")

    V1 = dbCreateInst(cv vdcM "V1" '(0.000 0.000) "R0")
    R1 = dbCreateInst(cv resM "R1" '(1.000 0.500) "R0")
    C1 = dbCreateInst(cv capM "C1" '(2.000 0.000) "R0")

    _SchLabelTerm(cv V1 "PLUS"  "VIN")
    _SchLabelTerm(cv V1 "MINUS" "GND")
    _SchLabelTerm(cv R1 "PLUS"  "VIN")
    _SchLabelTerm(cv R1 "MINUS" "VOUT")
    _SchLabelTerm(cv C1 "PLUS"  "VOUT")
    _SchLabelTerm(cv C1 "MINUS" "GND")

    schCheck(cv)
    dbSave(cv)
    geOpen(?lib libName ?cell cellName ?view "schematic" ?viewType "schematic" ?mode "a")
    sprintf(nil "created: %s/%s/schematic  (V1:vdc  R1:res  C1:cap  nets: VIN VOUT GND)"
            libName cellName)))
