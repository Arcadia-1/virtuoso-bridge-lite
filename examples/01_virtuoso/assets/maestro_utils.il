; maestro_utils.il
;
; Utility procedures for managing Maestro sessions via bridge.
;
; Procedures:
;   MaestroGetLibPath(libName)         - resolve lib path (writePath->readPath->libPath)
;   MaestroFindSessionViews(lib cell)  - find session views (maestro/adexl/maestro2/...)
;   MaestroOpen(lib cell [view])       - open session, ensure editable, return session
;   MaestroClose(lib cell [view])      - save and close session cleanly
;   MaestroCloseAll()                  - close all maestro windows and sessions
;   MaestroClearLocks(lib cell)        - remove stale .cdslck files (all session views)
;   MaestroDismissDialog()             - dismiss any blocking dialog

; --- Dismiss any current dialog (Overwrite, Save, etc.) ---
procedure(MaestroDismissDialog()
  let((form)
    form = hiGetCurrentForm()
    when(form
      hiFormDone(form)
      printf("[MaestroDismissDialog] Dismissed dialog\n")
      t
    )
  )
)

; --- Resolve library path with fallback: writePath -> readPath -> libPath ---
procedure(MaestroGetLibPath(libName)
  let((obj path)
    obj = ddGetObj(libName)
    when(obj
      path = obj~>writePath
      when(or(null(path) equal(path nil))
        path = obj~>readPath)
      when(or(null(path) equal(path nil))
        path = obj~>libPath))
    path))

; --- Find session-like views for a cell (maestro, adexl, maestro2, ...) ---
procedure(MaestroFindSessionViews(libName cellName)
  let((cellObj views result)
    cellObj = ddGetObj(libName cellName)
    result = '()
    when(cellObj
      foreach(v cellObj~>views
        let((vn)
          vn = v~>name
          when(or(rexMatchp("^maestro" vn)
                  rexMatchp("^adexl" vn)
                  rexMatchp("^normVim" vn)
                  rexMatchp("^topsim" vn))
            result = cons(vn result)))))
    reverse(result)))

; --- Clear stale lock files for a maestro/adexl cellview ---
; Probes all session-like views, not just "maestro".
procedure(MaestroClearLocks(libName cellName)
  let((libPath views lockPath)
    libPath = MaestroGetLibPath(libName)
    when(libPath
      views = MaestroFindSessionViews(libName cellName)
      ; Fall back to '("maestro") if no views found (cell may not exist yet)
      unless(views views = '("maestro"))
      foreach(viewName views
        lockPath = sprintf(nil "%s/%s/%s/%s.sdb.cdslck" libPath cellName viewName viewName)
        when(isFile(lockPath)
          deleteFile(lockPath)
          printf("[MaestroClearLocks] Removed %s\n" lockPath))))
    t))

; --- Open maestro: check existing, make editable, return session ---
; viewName defaults to "maestro" but accepts "adexl", "maestro2", etc.
procedure(MaestroOpen(libName cellName @optional (viewName "maestro"))
  let((existingWin session result)

    ; 1. Check if a window already open for this cell and view
    existingWin = nil
    foreach(win hiGetWindowList()
      let((name)
        name = hiGetWindowName(win)
        when(and(name
                 rexMatchp(cellName name)
                 rexMatchp(viewName name))
          existingWin = win
        )
      )
    )

    ; 2. If not open, clear stale locks then open
    unless(existingWin
      MaestroClearLocks(libName cellName)
      existingWin = deOpenCellView(libName cellName viewName viewName nil "r")
      printf("[MaestroOpen] Opened %s/%s/%s\n" libName cellName viewName)
    )

    ; 3. Try make editable; dismiss dialog if it pops up
    result = maeMakeEditable()
    MaestroDismissDialog()  ; handle "Change Mode Confirmation" / "Overwrite" dialog
    unless(result
      ; Retry after dismissing dialog
      result = maeMakeEditable()
      MaestroDismissDialog()
    )

    ; 4. Get first available session
    session = nil
    foreach(s maeGetSessions()
      unless(session session = s)
    )

    printf("[MaestroOpen] Session: %s  Editable: %L\n" session result)
    session
  )
)

; --- Close maestro for a specific cell: save then close ---
; viewName defaults to "maestro" but accepts "adexl", "maestro2", etc.
procedure(MaestroClose(libName cellName @optional (viewName "maestro"))
  let((found)
    ; Save first (prevents "save changes?" dialog on close)
    foreach(s maeGetSessions()
      maeSaveSetup(?lib libName ?cell cellName ?view viewName ?session s)
    )

    ; Find and close only this cell's windows for the given view
    found = nil
    foreach(win hiGetWindowList()
      let((name)
        name = hiGetWindowName(win)
        when(and(name
                 rexMatchp(cellName name)
                 rexMatchp(viewName name))
          hiCloseWindow(win)
          MaestroDismissDialog()  ; handle any save dialog
          found = t
        )
      )
    )

    printf("[MaestroClose] %s/%s/%s closed\n" libName cellName viewName)
    found
  )
)

; --- Close all maestro windows and sessions ---
procedure(MaestroCloseAll()
  let((count)
    count = 0

    ; Save all then close
    foreach(win hiGetWindowList()
      let((name)
        name = hiGetWindowName(win)
        when(and(name rexMatchp("maestro" name))
          hiCloseWindow(win)
          MaestroDismissDialog()
          count = count + 1
        )
      )
    )

    ; Close all sessions
    foreach(s maeGetSessions()
      maeCloseSession(?session s ?forceClose t)
    )

    printf("[MaestroCloseAll] Closed %d maestro windows\n" count)
    count
  )
)
