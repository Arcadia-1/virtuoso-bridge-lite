/*============================================================
 *  extract_symbol_info.il
 *  Extract ALL symbol elements (rects, lines, labels, terminals)
 *  in pipe-delimited format for Python parsing.
 *
 *  Usage:
 *    extractSymbolInfo("lib" "cell")
 *
 *  Output format (one line per element):
 *    RECT|layer|purpose|L|B|R|T
 *    LINE|layer|purpose|x1|y1|x2|y2
 *    LABEL|layer|purpose|text|x|y
 *    TERM|index|pin_index|name|direction|cx|cy|layer|purpose
 *============================================================*/

procedure( extractSymbolInfo(libName cellName)
    let((cv out pts bb px py)

        cv = dbOpenCellViewByType(libName cellName "symbol" nil "r")
        unless(cv
            printf("ERROR: Cannot open %s/%s/symbol\n" libName cellName)
            return(nil)
        )

        out = ""

        /* ── Dump all shapes ── */
        foreach(s cv~>shapes
            /* Rectangles */
            if(s~>objType == "rect" then
                bb = s~>bBox
                out = strcat(out sprintf(nil "RECT|%s|%s|%g|%g|%g|%g\n"
                    s~>layerName s~>purpose
                    car(car(bb)) cadr(car(bb))
                    car(cadr(bb)) cadr(cadr(bb))))
            )

            /* Lines (wire stubs) — 2-point only */
            if(s~>objType == "line" then
                pts = s~>points
                when(length(pts) == 2
                    out = strcat(out sprintf(nil "LINE|%s|%s|%g|%g|%g|%g\n"
                        s~>layerName s~>purpose
                        car(car(pts))  cadr(car(pts))
                        car(cadr(pts)) cadr(cadr(pts))))
                )
            )

            /* Labels */
            if(s~>objType == "label" then
                out = strcat(out sprintf(nil "LABEL|%s|%s|%s|%g|%g\n"
                    s~>layerName s~>purpose
                    s~>theLabel
                    car(s~>xy) cadr(s~>xy)))
            )
        )

        /* ── Dump all terminals with ALL pin figures ── */
        ti = 0
        foreach(trm cv~>terminals
            let((pins)
                pins = trm~>pins
                pi = 0
                foreach(p pins
                    let((fig)
                        fig = car(p~>figs)
                        when(fig
                            bb = fig~>bBox
                            px = (car(car(bb)) + car(cadr(bb))) / 2.0
                            py = (cadr(car(bb)) + cadr(cadr(bb))) / 2.0
                            out = strcat(out sprintf(nil "TERM|%d|%d|%s|%s|%g|%g|%s|%s\n"
                                ti pi trm~>name trm~>direction px py
                                fig~>layerName fig~>purpose))
                        )
                        pi = pi + 1
                    )
                )
                ti = ti + 1
            )
        )

        printf("%s" out)
        out
    )
)
