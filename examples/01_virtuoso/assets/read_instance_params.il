;; read_instance_params.il
;;
;; PDK library configuration — shared with schematic_ops.il.
;; Set _SchPdkLib before loading this file to override.
unless(boundp('_SchPdkLib) _SchPdkLib = "tsmcN28")
;;
;; Procedures:
;;   CdfDumpInstParams(?lib ?cell ?view ?filter)
;;
;; ?filter - list of CDF param names to include; nil = show all non-empty params
;;
;; Usage:
;;   CdfDumpInstParams()
;;   CdfDumpInstParams(?filter list("w" "l" "nf" "m" "model"))
;;   CdfDumpInstParams(?lib "mylib" ?cell "inv" ?view "schematic")

procedure(CdfDumpInstParams(@key (lib nil) (cell nil) (view "schematic") (filter nil))
  prog((cv buf cdfId)
    when(lib == nil || cell == nil
      cv = geGetWindowCellView()
      when(cv == nil
        return("ERROR: no open schematic. Pass ?lib ?cell or open a cell view."))
      lib  = cv~>libName
      cell = cv~>cellName
      view = cv~>viewName)
    unless(cv
      cv = dbOpenCellViewByType(lib cell view "schematic"))
    when(cv == nil
      return(sprintf(nil "ERROR: cannot open %s/%s/%s" lib cell view)))

    buf = sprintf(nil "Schematic: %s/%s/%s  (%d instances)\n" lib cell view length(cv~>instances))

    ; ── Schematic-level pins ──────────────────────────────────────────────
    let((pinLine)
      pinLine = ""
      foreach(term cv~>terminals
        pinLine = strcat(pinLine sprintf(nil " %s(%s)" term~>name term~>direction)))
      when(pinLine != ""
        buf = strcat(buf "Pins:" pinLine "\n")))

    foreach(inst cv~>instances
      let((connLine paramLine)
        buf = strcat(buf sprintf(nil "\n%s  [%s/%s]\n"
                     inst~>name inst~>libName inst~>cellName))

        ; ── Connections on one line ────────────────────────────────────────
        connLine = ""
        foreach(it inst~>instTerms
          let((netName)
            netName = if(it~>net then it~>net~>name else "?")
            connLine = strcat(connLine sprintf(nil " %s:%s" it~>name netName))))
        when(connLine != ""
          buf = strcat(buf " " connLine "\n"))

        ; ── CDF parameters on one line ────────────────────────────────────
        ; For tsmcN28 transistors, only show key electrical params.
        ; For everything else, show all non-empty params (length-filtered).
        cdfId = cdfGetInstCDF(inst)
        if(cdfId
          then
            let((mosfetFilter activeFilter)
              mosfetFilter = when(inst~>libName == _SchPdkLib
                               list("l" "w" "nf" "fingers" "m" "totalM" "simM" "description"))
              activeFilter = if(mosfetFilter then mosfetFilter else filter)
              paramLine = ""
              foreach(param cdfId~>parameters
                let((valStr)
                  valStr = sprintf(nil "%L" param~>value)
                  when(param~>value != nil && param~>value != ""
                    && strlen(valStr) <= 120
                    && (null(activeFilter) || member(param~>name activeFilter))
                    paramLine = strcat(paramLine sprintf(nil " %s=%s" param~>name valStr)))))
              when(paramLine != ""
                buf = strcat(buf " " paramLine "\n")))
          else
            buf = strcat(buf "  (no CDF)\n"))))
    return(buf)))
