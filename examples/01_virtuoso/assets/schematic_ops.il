; schematic_ops.il
;
; Internal helpers:
;   _SchGetEditCV()                    - find the open schematic window, return editable cv
;   _SchLabelTerm(cv inst term net)    - place net label at instance terminal position
;
; Atomic procedures (parameterized, no auto-save):
;   SchOpenNew(libName cellName)                - create + open a fresh schematic in the GUI
;   SchCreateAnalogInst(cell name x y orient)   - place one instance from _SchAnalogLib
;   SchSetInstParam(instName paramName value)   - set one CDF parameter on an instance
;   SchNetLabel(instName termName netName)      - place net label at a terminal
;   SchRenameInst(oldName newName)              - rename one instance
;   SchDeleteInst(instName)                     - delete one instance by name
;   SchListInsts()                              - return newline-separated instance names
;   SchSave()                                   - schCheck + dbSave on the open schematic
;   SchDeleteCell(libName cellName)             - close window and delete entire cell
;

;;; ── Internal helpers ──────────────────────────────────────────────────────

procedure(_SchGetEditCV()
  ; Prefer _SchCurrentCV when set by SchOpenNew; fall back to window scan.
  let((cv)
    cv = when(boundp('_SchCurrentCV) && _SchCurrentCV
           dbOpenCellViewByType(
             _SchCurrentCV~>libName _SchCurrentCV~>cellName
             "schematic" "schematic" "a"))
    unless(cv
      foreach(win hiGetWindowList()
        when(!cv && win~>cellView && win~>cellView~>viewName == "schematic"
          cv = dbOpenCellViewByType(
                 win~>cellView~>libName win~>cellView~>cellName
                 "schematic" "schematic" "a"))))
    cv))

procedure(_SchTermCenter(inst termName)
  ; Return schematic-coordinate center of a terminal pin figure.
  ; instTerm~>point is nil for many cell types; use dbTransformBBox instead.
  let((term pin fig bbox)
    term = car(setof(x inst~>master~>terminals x~>name == termName))
    unless(term return(nil))
    pin  = car(term~>pins)
    fig  = when(pin car(pin~>figs))
    bbox = when(fig dbTransformBBox(fig~>bBox inst~>transform))
    when(bbox
      list((xCoord(car(bbox)) + xCoord(cadr(bbox))) / 2.0
           (yCoord(car(bbox)) + yCoord(cadr(bbox))) / 2.0))))

procedure(_SchLabelTerm(cv inst termName netName)
  let((ctr)
    ctr = _SchTermCenter(inst termName)
    when(ctr
      schCreateWireLabel(cv nil ctr netName
                         "lowerLeft" "R0" "stick" 0.0625 nil))))

;;; ── Atomic procedures ─────────────────────────────────────────────────────

procedure(SchOpenNew(libName cellName)
  let((cv)
    cv = dbOpenCellViewByType(libName cellName "schematic" "schematic" "a")
    unless(cv return(sprintf(nil "ERROR: cannot open %s/%s/schematic" libName cellName)))
    _SchCurrentCV = cv  ; pin for _SchGetEditCV
    geOpen(?lib libName ?cell cellName ?view "schematic" ?viewType "schematic" ?mode "a")
    sprintf(nil "opened: %s/%s/schematic" libName cellName)))

procedure(SchCreateAnalogInst(analogCell instName x y orient)
  let((cv master inst)
    cv = _SchGetEditCV()
    unless(cv return("ERROR: no schematic window open"))
    master = dbOpenCellView(_SchAnalogLib analogCell "symbol")
    unless(master return(sprintf(nil "ERROR: %s/%s not found" _SchAnalogLib analogCell)))
    inst = dbCreateInst(cv master instName list(x y) orient)
    sprintf(nil "inst: %s (%s/%s)" instName _SchAnalogLib analogCell)))

procedure(SchSetInstParam(instName paramName value)
  let((cv inst cdfId param)
    cv = _SchGetEditCV()
    unless(cv return("ERROR: no schematic window open"))
    inst = car(setof(x cv~>instances x~>name == instName))
    unless(inst return(sprintf(nil "ERROR: instance '%s' not found" instName)))
    cdfId = cdfGetInstCDF(inst)
    unless(cdfId return(sprintf(nil "ERROR: no CDF for '%s'" instName)))
    param = cdfFindParamByName(cdfId paramName)
    unless(param return(sprintf(nil "ERROR: param '%s' not found on '%s'" paramName instName)))
    param~>value = value
    sprintf(nil "param: %s.%s = %s" instName paramName value)))

procedure(SchNetLabel(instName termName netName)
  let((cv inst)
    cv = _SchGetEditCV()
    unless(cv return("ERROR: no schematic window open"))
    inst = car(setof(x cv~>instances x~>name == instName))
    unless(inst return(sprintf(nil "ERROR: instance '%s' not found" instName)))
    _SchLabelTerm(cv inst termName netName)
    sprintf(nil "net: %s.%s -> %s" instName termName netName)))

procedure(SchRenameInst(oldName newName)
  let((cv inst)
    cv = _SchGetEditCV()
    unless(cv return("ERROR: no schematic window open"))
    inst = car(setof(x cv~>instances x~>name == oldName))
    unless(inst return(sprintf(nil "ERROR: instance '%s' not found" oldName)))
    inst~>name = newName
    sprintf(nil "renamed: %s -> %s  [%s/%s]" oldName newName cv~>libName cv~>cellName)))

procedure(SchDeleteInst(instName)
  let((cv inst)
    cv = _SchGetEditCV()
    unless(cv return("ERROR: no schematic window open"))
    inst = car(setof(x cv~>instances x~>name == instName))
    unless(inst return(sprintf(nil "ERROR: instance '%s' not found" instName)))
    dbDeleteObject(inst)
    sprintf(nil "deleted: %s  [%s/%s]" instName cv~>libName cv~>cellName)))

procedure(SchListInsts()
  let((cv out)
    cv = _SchGetEditCV()
    unless(cv return("ERROR: no schematic window open"))
    out = ""
    foreach(inst cv~>instances
      out = strcat(out inst~>name "\n"))
    out))

procedure(SchSave()
  let((cv)
    cv = _SchGetEditCV()
    unless(cv return("ERROR: no schematic window open"))
    schCheck(cv)
    dbSave(cv)
    sprintf(nil "saved: %s/%s" cv~>libName cv~>cellName)))

procedure(SchDeleteCell(libName cellName)
  let((ddcell)
    foreach(win hiGetWindowList()
      when(win~>cellView
        && win~>cellView~>libName == libName
        && win~>cellView~>cellName == cellName
        hiCloseWindow(win)))
    ddcell = ddGetObj(libName cellName)
    if(ddcell
      then ddDeleteObj(ddcell)
           sprintf(nil "deleted: %s/%s" libName cellName)
      else sprintf(nil "ERROR: cell not found: %s/%s" libName cellName))))

procedure(_SchPinAtTerm(cv inst termName pinName direction)
  let((ctr)
    ctr = _SchTermCenter(inst termName)
    when(ctr
      schCreatePin(cv nil pinName direction nil ctr "R0"))))

;;; ── PDK library configuration ─────────────────────────────────────────────
;;; Edit these names to match the PDK installed in your Virtuoso session.

_SchPdkLib    = "tsmcN28"   ; process library containing NMOS/PMOS symbols
_SchAnalogLib = "analogLib" ; Cadence built-in analog primitives

