; list_library_cells.il
;
; Procedures:
;   ListLibraries()            - list all library names
;   ListLibraryCells(libName)  - list cells and views in a library
; Lists all cells and views in a Virtuoso library.
; Prints to CIW and returns a newline-separated string for Python.
;
; Usage: ListLibraryCells("MY_LIB")

procedure(ListLibraries()
  let((out)
    out = ""
    foreach(lib ddGetLibList()
      printf("%s\n" lib~>name)
      out = strcat(out lib~>name "\n"))
    out))

procedure(ListLibraryCells(libName)
  let((lib out views)
    lib = ddGetObj(libName)
    out = ""
    printf("========================================\n")
    printf("[library cells] %s\n" libName)
    printf("========================================\n")
    when(lib
      foreach(cell lib~>cells
        views = ""
        foreach(view cell~>views
          views = strcat(views view~>name " "))
        printf("cell=%-30s views=%s\n" cell~>name views)
        out = strcat(out sprintf(nil "%s|views=%s\n" cell~>name views))))
    printf("========================================\n")
    out))
