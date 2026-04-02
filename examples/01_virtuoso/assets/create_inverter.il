; create_inverter.il
;
; Procedures:
;   SchCreateInverter(libName cellName)  - create CMOS inverter (NMOS + PMOS from _SchPdkLib), open window
;
; Depends on: schematic_ops.il (for _SchLabelTerm, _SchPdkLib, _SchAnalogLib)

procedure(SchCreateInverter(libName cellName)
  ; CMOS inverter: NMOS + PMOS from _SchPdkLib, VDC supply from _SchAnalogLib.
  ; Net labels placed at terminal pin bBox centers (via _SchTermCenter).
  let((cv nchM pchM vdcM MN0 MP0 V0)
    cv = dbOpenCellViewByType(libName cellName "schematic" "schematic" "a")
    unless(cv return(sprintf(nil "ERROR: cannot open %s/%s/schematic" libName cellName)))

    nchM = dbOpenCellView(_SchPdkLib "nch_ulvt_mac" "symbol")
    pchM = dbOpenCellView(_SchPdkLib "pch_ulvt_mac" "symbol")
    vdcM = dbOpenCellView(_SchAnalogLib "vdc" "symbol")

    unless(nchM && pchM
      return(sprintf(nil "ERROR: PDK library '%s' not found" _SchPdkLib)))

    MN0 = dbCreateInst(cv nchM "MN0" '(0.000 0.000) "R0")
    MP0 = dbCreateInst(cv pchM "MP0" '(0.000 1.500) "R0")
    V0  = dbCreateInst(cv vdcM "V0"  '(2.000 0.750) "R0")

    _SchLabelTerm(cv MN0 "G" "IN")
    _SchLabelTerm(cv MN0 "D" "OUT")
    _SchLabelTerm(cv MN0 "S" "VSS")
    _SchLabelTerm(cv MN0 "B" "VSS")

    _SchLabelTerm(cv MP0 "G" "IN")
    _SchLabelTerm(cv MP0 "D" "OUT")
    _SchLabelTerm(cv MP0 "S" "VDD")
    _SchLabelTerm(cv MP0 "B" "VDD")

    _SchLabelTerm(cv V0 "PLUS"  "VDD")
    _SchLabelTerm(cv V0 "MINUS" "VSS")

    schCreatePin(cv nil "IN"  "input"  nil '(-1.000 0.750) "R0")
    schCreatePin(cv nil "OUT" "output" nil '(-1.000 0.250) "R0")

    schCheck(cv)
    dbSave(cv)
    geOpen(?lib libName ?cell cellName ?view "schematic" ?viewType "schematic" ?mode "a")
    sprintf(nil "created: %s/%s/schematic  (MN0:nch_ulvt_mac  MP0:pch_ulvt_mac  nets: IN OUT VDD VSS)"
            libName cellName)))
