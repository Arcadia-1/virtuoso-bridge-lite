; ade_bridge.il — ADE Explorer/Assembler bridge functions
; Load in CIW: load("/path/to/ade_bridge.il")
;
; Tested on IC618 (IC251 compatible)
; Works with: asiGetCurrentSession, asiGetDesignVarList,
;             asiSetDesignVarList, asiGetResultsDir, sevRun, sevSession

; --- Find ADE Explorer or Assembler window ---
procedure(adeBridgeFindAdeWindow()
  let((adeWin)
    adeWin = nil
    foreach(win hiGetWindowList()
      let((name)
        name = hiGetWindowName(win)
        when(and(name null(adeWin)
                 or(rexMatchp("ADE Explorer" name)
                    rexMatchp("ADE Assembler" name)))
          adeWin = win
        )
      )
    )
    adeWin
  )
)

; --- Ping ---
procedure(adeBridgePing()
  t
)

; --- List all design variables ---
procedure(adeBridgeListVars()
  asiGetDesignVarList(asiGetCurrentSession())
)

; --- Get a single design variable ---
procedure(adeBridgeGetVar(varName)
  let((vars result)
    vars = asiGetDesignVarList(asiGetCurrentSession())
    result = nil
    foreach(pair vars
      when(equal(car(pair) varName)
        result = cadr(pair)
      )
    )
    result
  )
)

; --- Set a single design variable ---
procedure(adeBridgeSetVar(varName varValue)
  let((session vars newVars)
    session = asiGetCurrentSession()
    vars = asiGetDesignVarList(session)
    newVars = nil
    foreach(pair vars
      if(equal(car(pair) varName) then
        newVars = cons(list(varName varValue) newVars)
      else
        newVars = cons(pair newVars)
      )
    )
    asiSetDesignVarList(session reverse(newVars))
    t
  )
)

; --- Run simulation (auto-detects Explorer vs Assembler) ---
procedure(adeBridgeRunSim()
  let((adeWin name session)
    adeWin = adeBridgeFindAdeWindow()
    if(adeWin then
      name = hiGetWindowName(adeWin)
      if(rexMatchp("Assembler" name) then
        maeRunSimulation()
      else
        session = sevSession(adeWin)
        if(session then
          sevRun(session)
        else
          printf("[adeBridge] sevSession returned nil for %s\n" name)
          nil
        )
      )
    else
      printf("[adeBridge] no ADE Explorer/Assembler window found\n")
      nil
    )
  )
)

; --- Get results directory ---
procedure(adeBridgeGetResultsDir()
  asiGetResultsDir(asiGetCurrentSession())
)
