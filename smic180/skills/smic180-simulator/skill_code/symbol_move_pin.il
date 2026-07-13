/*============================================================
 *  symbol_move_pin.il
 *  Move a single symbol pin — rect + wire + label — to a
 *  new (x, y) position.  Handles the three shape types TSG
 *  generates per pin:
 *    rect  (pin/drawing)   →  bBox
 *    line  (device/drawing) →  points
 *    label (pin/drawing)    →  xy
 *
 *  Usage:
 *    symbolMovePin("lib" "cell" "PIN_NAME" newX newY "side")
 *    side is one of: "left" "right" "top" "bottom"
 *
 *  Returns: t on success, raises error on failure.
 *============================================================*/

procedure( symbolMovePin(libName cellName pinName newX newY side)
    let((cv term pin fig bb oldCx oldCy dx dy shapes s stype pts xy wireLen)
        wireLen = 0.375
        cv = dbOpenCellViewByType(libName cellName "symbol" "schematicSymbol" "a")
        unless(cv
            error("symbolMovePin: cannot open %s/%s/symbol\n" libName cellName)
        )

        ; Locate the terminal by name
        term = car(setof(_t cv~>terminals _t~>name == pinName))
        unless(term
            error("symbolMovePin: pin %s not found in %s/%s\n" pinName libName cellName)
        )

        ; Current pin center from the terminal's pin figure
        pin = car(term~>pins)
        fig = car(pin~>figs)
        bb = fig~>bBox
        oldCx = (car(car(bb)) + car(cadr(bb))) / 2.0
        oldCy = (cadr(car(bb)) + cadr(cadr(bb))) / 2.0

        dx = newX - oldCx
        dy = newY - oldCy

        ; Collect all shapes whose center is near the pin's current position.
        ; TSG places the rect, line stub, and label within ~0.25 um of the
        ; pin center, so a search window of ±0.5 in x and ±0.1 in y safely
        ; captures all three without grabbing neighboring pins.
        shapes = setof(s cv~>shapes
            let((sbb scx scy)
                sbb = s~>bBox
                scx = (car(car(sbb)) + car(cadr(sbb))) / 2.0
                scy = (cadr(car(sbb)) + cadr(cadr(sbb))) / 2.0
                abs(scx - oldCx) < 0.5 && abs(scy - oldCy) < 0.1
            )
        )

        ; Move each shape according to its type
        foreach(s shapes
            stype = s~>objType
            if(stype == "rect" then
                bb = s~>bBox
                s~>bBox = list(
                    list(car(car(bb)) + dx,  cadr(car(bb)) + dy)
                    list(car(cadr(bb)) + dx, cadr(cadr(bb)) + dy))
            )
            if(stype == "line" then
                ; Re-create wire to extend outward from body on this side
                case(side
                    ("left"   s~>points = list(list(newX newY) list(newX - wireLen newY)))
                    ("right"  s~>points = list(list(newX newY) list(newX + wireLen newY)))
                    ("top"    s~>points = list(list(newX newY) list(newX newY + wireLen)))
                    ("bottom" s~>points = list(list(newX newY) list(newX newY - wireLen)))
                )
            )
            if(stype == "label" then
                xy = s~>xy
                s~>xy = list(car(xy) + dx, cadr(xy) + dy)
            )
        )

        dbSave(cv)
        printf("MOVE-PIN: %s  (%f %f) -> (%f %f)  side=%s  shapes=%d\n"
               pinName oldCx oldCy newX newY side length(shapes))
        t
    )
)
