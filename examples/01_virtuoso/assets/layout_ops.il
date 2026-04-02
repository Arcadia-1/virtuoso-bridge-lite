; layout_ops.il
;
; Internal helpers:
;   _LayoutGetEditCV()                           - find open layout window, return editable cv
;
; Procedures:
;   LayoutReadLayout(libName cellName)           - read shapes/instances summary (read-only)
;   LayoutListShapes()                           - list shape type+layer from open layout
;   LayoutSave()                                 - save open layout
;   LayoutDeleteShapesOnLayer(layer purpose)     - delete all shapes on a layer/purpose
;   LayoutClearRouting()                         - delete all shapes (keep instances), then save
;   LayoutDeleteCell(libName cellName)           - close window and delete entire cell

;;; ── Internal helpers ──────────────────────────────────────────────────────

procedure(_LayoutGetEditCV()
  let((cv)
    cv = nil
    foreach(win hiGetWindowList()
      when(!cv && win~>cellView && win~>cellView~>viewName == "layout"
        cv = dbOpenCellViewByType(
               win~>cellView~>libName win~>cellView~>cellName
               "layout" "maskLayout" "a")))
    cv))

;;; ── Procedures ────────────────────────────────────────────────────────────

procedure(LayoutReadLayout(libName cellName)
  prog((cv buf)
    cv = dbOpenCellViewByType(libName cellName "layout" "maskLayout" "r")
    unless(cv return(sprintf(nil "ERROR: cannot open %s/%s/layout" libName cellName)))
    buf = sprintf(nil "Layout: %s/%s/layout  (%d shapes  %d instances)\n"
                  libName cellName length(cv~>shapes) length(cv~>instances))
    foreach(shape cv~>shapes
      buf = strcat(buf sprintf(nil "  %s [%s %s]"
                   shape~>objType car(shape~>lpp) cadr(shape~>lpp)))
      when(shape~>objType == "rect"
        let((bb)
          bb = shape~>bBox
          buf = strcat(buf sprintf(nil "  (%.3f %.3f)-(%.3f %.3f)"
                       xCoord(car(bb)) yCoord(car(bb))
                       xCoord(cadr(bb)) yCoord(cadr(bb))))))
      buf = strcat(buf "\n"))
    foreach(inst cv~>instances
      buf = strcat(buf sprintf(nil "  inst: %s  [%s/%s]  @ (%.3f %.3f)\n"
                   inst~>name inst~>libName inst~>cellName
                   xCoord(inst~>xy) yCoord(inst~>xy))))
    return(buf)))

procedure(LayoutListShapes()
  prog((cv out)
    cv = _LayoutGetEditCV()
    unless(cv return("ERROR: no layout window open"))
    out = ""
    foreach(shape cv~>shapes
      out = strcat(out sprintf(nil "%s [%s %s]\n"
                   shape~>objType car(shape~>lpp) cadr(shape~>lpp))))
    return(out)))

procedure(LayoutSave()
  prog((cv)
    cv = _LayoutGetEditCV()
    unless(cv return("ERROR: no layout window open"))
    dbSave(cv)
    return(sprintf(nil "saved: %s/%s" cv~>libName cv~>cellName))))

procedure(LayoutDeleteShapesOnLayer(layer purpose)
  prog((cv shapes count)
    cv = _LayoutGetEditCV()
    unless(cv return("ERROR: no layout window open"))
    shapes = cv~>shapes
    count = 0
    foreach(shape shapes
      when(car(shape~>lpp) == layer && cadr(shape~>lpp) == purpose
        dbDeleteObject(shape)
        count = count + 1))
    return(sprintf(nil "deleted %d shape(s) on %s/%s  [%s/%s]"
            count layer purpose cv~>libName cv~>cellName))))

procedure(LayoutClearRouting()
  prog((cv count)
    cv = _LayoutGetEditCV()
    unless(cv return("ERROR: no layout window open"))
    count = length(cv~>shapes)
    foreach(shape cv~>shapes dbDeleteObject(shape))
    dbSave(cv)
    return(sprintf(nil "deleted %d shape(s) from %s/%s (instances preserved)"
            count cv~>libName cv~>cellName))))

procedure(LayoutDeleteCell(libName cellName)
  let((ddcell)
    foreach(win hiGetWindowList()
      when(win~>cellView
        && win~>cellView~>libName == libName
        && win~>cellView~>cellName == cellName
        dbSave(win~>cellView)
        hiCloseWindow(win)))
    ddcell = ddGetObj(libName cellName)
    if(ddcell
      then ddDeleteObj(ddcell)
           sprintf(nil "deleted: %s/%s" libName cellName)
      else sprintf(nil "ERROR: cell not found: %s/%s" libName cellName))))
