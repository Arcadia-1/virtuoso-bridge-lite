; file_ops.il
;
; Procedures:
;   VbWriteFile(path content)  - write string content to a file on the remote host
;   VbReadFile(path)           - read file contents; returns "ERROR: ..." if not found

procedure(VbWriteFile(path content)
  prog((port)
    port = outfile(path "w")
    unless(port return(sprintf(nil "ERROR: cannot write %s" path)))
    fprintf(port "%s" content)
    close(port)
    return("ok")))

procedure(VbReadFile(path)
  prog((port line lines)
    port = infile(path)
    unless(port return(sprintf(nil "ERROR: cannot read %s" path)))
    lines = ""
    while(gets(line port)
      lines = strcat(lines line))
    close(port)
    return(lines)))
