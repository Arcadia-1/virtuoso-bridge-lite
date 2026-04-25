; harvest_library.il
;
; Procedures for harvesting library metadata with robust view detection,
; nil normalization, and reliable library-path resolution.
;
; Fixes three problems from issue #7:
;   1. Hardcoded "maestro" view name — now probes all session-like views
;   2. Literal "nil" from SKILL treated as real data — now normalized
;   3. ddGetObj(lib)->libPath unreliable — falls back to writePath/readPath
;
; Procedures:
;   HarvestGetLibRoot(libName)          - resolve library root path
;   HarvestGetSessionViews(cellViewList) - filter session-like views from a list
;   HarvestGetSchematicViews(cellViewList) - filter schematic-like views
;   HarvestNormNil(val)                 - normalize SKILL nil to empty string
;   HarvestNormNilList(val)             - normalize SKILL nil to empty list
;   HarvestCellInfo(libName cellName)   - harvest one cell's metadata
;   HarvestLibrary(libName)             - harvest entire library metadata

; --- Known session-like view prefixes ---
; Views that contain Maestro/ADE session state and simulation results.
; Matched as prefixes so "maestro2", "spectre_post" etc. are caught.
procedure(HarvestIsSessionView(viewName)
  let((prefixes found)
    prefixes = '("maestro" "adexl" "normVim" "topsim" "spectre")
    found = nil
    foreach(pfx prefixes
      when(rexMatchp(strcat("^" pfx) viewName)
        found = t))
    found))

; --- Known schematic-like view prefixes ---
procedure(HarvestIsSchematicView(viewName)
  rexMatchp("^schematic" viewName))

; --- Known config-like view prefixes ---
procedure(HarvestIsConfigView(viewName)
  rexMatchp("^config" viewName))

; --- Normalize a SKILL value: nil or literal string "nil" -> "" ---
procedure(HarvestNormNil(val)
  if(or(null(val) equal(val "nil") equal(val nil))
    then ""
    else val))

; --- Normalize a SKILL list value: nil -> '() ---
procedure(HarvestNormNilList(val)
  if(or(null(val) equal(val "nil") equal(val nil))
    then '()
    else val))

; --- Resolve library root path with fallback chain ---
; libPath -> writePath -> readPath
procedure(HarvestGetLibRoot(libName)
  let((libObj root)
    libObj = ddGetObj(libName)
    unless(libObj
      printf("[harvest] ERROR: library '%s' not found\n" libName)
      return(""))
    root = libObj~>libPath
    when(or(null(root) equal(root nil))
      root = libObj~>writePath)
    when(or(null(root) equal(root nil))
      root = libObj~>readPath)
    when(or(null(root) equal(root nil))
      printf("[harvest] WARNING: no path resolved for '%s'\n" libName)
      root = "")
    root))

; --- Filter session-like views from a view name list ---
procedure(HarvestGetSessionViews(viewNames)
  let((result)
    result = '()
    foreach(vn viewNames
      when(HarvestIsSessionView(vn)
        result = cons(vn result)))
    reverse(result)))

; --- Filter schematic-like views from a view name list ---
procedure(HarvestGetSchematicViews(viewNames)
  let((result)
    result = '()
    foreach(vn viewNames
      when(HarvestIsSchematicView(vn)
        result = cons(vn result)))
    reverse(result)))

; --- Probe Maestro setups for a given cell and session view ---
; Returns a list of setup info strings, or '() if none.
procedure(HarvestProbeSetups(libName cellName viewName)
  let((rawSetups setups result)
    rawSetups = errset(maeGetSetupNames(libName cellName viewName))
    ; errset returns list(value) on success, nil on error
    when(rawSetups
      rawSetups = car(rawSetups))
    setups = HarvestNormNilList(rawSetups)
    result = '()
    foreach(setup setups
      ; Skip literal "nil" setup names
      unless(or(null(setup) equal(setup "nil"))
        let((analyses)
          analyses = HarvestNormNilList(
            errset(maeGetEnabledAnalysis(libName cellName viewName setup))
            )
          when(analyses analyses = car(analyses))
          analyses = HarvestNormNilList(analyses)
          result = cons(
            sprintf(nil "setup=%s analyses=%L" setup analyses)
            result))))
    reverse(result)))

; --- Check if result directories exist for a cell/view ---
procedure(HarvestProbeResults(libRoot cellName viewName)
  let((resultsPath hasResults)
    resultsPath = sprintf(nil "%s/%s/%s/results" libRoot cellName viewName)
    hasResults = isDir(resultsPath)
    when(hasResults
      printf("[harvest]   results: %s\n" resultsPath))
    hasResults))

; --- Harvest metadata for a single cell ---
; Returns a formatted string with cell info.
procedure(HarvestCellInfo(libName cellName)
  let((cellObj viewNames sessionViews schematicViews configViews
       libRoot out)
    cellObj = ddGetObj(libName cellName)
    unless(cellObj
      printf("[harvest] WARNING: cell '%s/%s' not found\n" libName cellName)
      return(""))

    ; Enumerate actual views
    viewNames = '()
    foreach(v cellObj~>views
      viewNames = cons(v~>name viewNames))
    viewNames = reverse(viewNames)

    ; Classify views
    sessionViews = HarvestGetSessionViews(viewNames)
    schematicViews = HarvestGetSchematicViews(viewNames)
    configViews = '()
    foreach(vn viewNames
      when(HarvestIsConfigView(vn)
        configViews = cons(vn configViews)))
    configViews = reverse(configViews)

    libRoot = HarvestGetLibRoot(libName)

    ; Format output
    out = sprintf(nil "cell=%s\n" cellName)
    out = strcat(out sprintf(nil "  all_views=%L\n" viewNames))
    out = strcat(out sprintf(nil "  session_views=%L\n" sessionViews))
    out = strcat(out sprintf(nil "  schematic_views=%L\n" schematicViews))
    out = strcat(out sprintf(nil "  config_views=%L\n" configViews))

    ; Probe each session view
    foreach(sv sessionViews
      let((setups hasResults)
        out = strcat(out sprintf(nil "  [%s]\n" sv))

        ; Probe setups (only for maestro/adexl views)
        setups = HarvestProbeSetups(libName cellName sv)
        if(setups
          then foreach(s setups
                 out = strcat(out sprintf(nil "    %s\n" s)))
          else out = strcat(out "    (no setups)\n"))

        ; Probe result directories
        when(nequal(libRoot "")
          hasResults = HarvestProbeResults(libRoot cellName sv)
          out = strcat(out sprintf(nil "    has_results=%L\n" hasResults)))))

    out))

; --- Harvest entire library ---
; Returns a newline-separated summary string suitable for Python consumption.
procedure(HarvestLibrary(libName)
  let((libObj libRoot cells out)
    libObj = ddGetObj(libName)
    unless(libObj
      printf("[harvest] ERROR: library '%s' not found\n" libName)
      return(""))

    libRoot = HarvestGetLibRoot(libName)

    out = ""
    out = strcat(out "========================================\n")
    out = strcat(out sprintf(nil "[harvest] %s\n" libName))
    out = strcat(out sprintf(nil "[harvest] root=%s\n" libRoot))
    out = strcat(out "========================================\n")

    foreach(cell libObj~>cells
      let((info)
        info = HarvestCellInfo(libName cell~>name)
        printf("%s" info)
        out = strcat(out info)))

    out = strcat(out "========================================\n")
    out))
