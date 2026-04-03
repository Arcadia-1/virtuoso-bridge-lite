; read_connectivity.il
;
; Procedures:
;   ReadSchematic(libName cellName) - read full connectivity from a schematic

procedure(ReadSchematic(libName cellName)
  let((cv out)
    cv = dbOpenCellViewByType(libName cellName "schematic" "schematic" "r")
    out = ""

    unless(cv
      return(sprintf(nil "ERROR: cannot open %s/%s/schematic" libName cellName)))

    ; ── Header ────────────────────────────────────────────────────────────
    out = strcat(out sprintf(nil "Schematic : %s/%s/%s\n" cv~>libName cv~>cellName cv~>viewName))
    out = strcat(out sprintf(nil "Instances : %d   Nets : %d   Pins : %d\n"
                   length(cv~>instances) length(cv~>nets) length(cv~>terminals)))
    printf("%s" out)

    ; ── Instances ─────────────────────────────────────────────────────────
    printf("\nINSTANCE             LIB/CELL\n")
    printf("----------------------------------------------------\n")
    out = strcat(out "\nINSTANCE             LIB/CELL\n")
    out = strcat(out "----------------------------------------------------\n")
    foreach(inst cv~>instances
      let((line)
        line = sprintf(nil "%-20s  %s/%s\n" inst~>name inst~>libName inst~>cellName)
        printf("%s" line)
        out = strcat(out line)))

    ; ── Net connections ────────────────────────────────────────────────────
    printf("\nNET                  CONNECTIONS (inst.terminal)\n")
    printf("----------------------------------------------------\n")
    out = strcat(out "\nNET                  CONNECTIONS (inst.terminal)\n")
    out = strcat(out "----------------------------------------------------\n")
    foreach(net cv~>nets
      let((conns sep line)
        conns = ""
        sep = ""
        foreach(it net~>instTerms
          conns = strcat(conns sep sprintf(nil "%s.%s" it~>inst~>name it~>name))
          sep = "  ")
        line = sprintf(nil "%-20s  %s\n" net~>name conns)
        printf("%s" line)
        out = strcat(out line)))

    ; ── Pins ──────────────────────────────────────────────────────────────
    printf("\nPIN                  DIRECTION\n")
    printf("------------------------------------\n")
    out = strcat(out "\nPIN                  DIRECTION\n")
    out = strcat(out "------------------------------------\n")
    foreach(term cv~>terminals
      let((line)
        line = sprintf(nil "%-20s  %s\n" term~>name term~>direction)
        printf("%s" line)
        out = strcat(out line)))

    out))
