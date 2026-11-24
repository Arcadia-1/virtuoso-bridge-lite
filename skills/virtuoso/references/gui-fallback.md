# GUI fallback: driving a form over X11

Last resort. Use it only when a setting has no SKILL/mae path that reaches the
netlist. Order of preference:

1. SKILL / `client.maestro.*` / `mae*` calls.
2. Netlist the setup, patch the netlist, run Spectre directly (the result is
   right, but Maestro's own result columns stay empty).
3. Click the form with `virtuoso-bridge gui` (this page).

Known cases on IC23.1 where only the form works (the API accepts the option,
stores it, and the netlister ignores it):

| Analysis | Setting | What the form adds to the netlist |
|---|---|---|
| `tran` | Transient Noise toggle | `noisefmax=... noisefmin=... noiseseed=...` |
| `pnoise` (sampled) | jitter event table (`measTableData`), via **Add** | `measurement=[pm0]` and `pm0 jitterevent ...` |
| `pss` | Run transient: Decide automatically | `autotstab=yes` |

After the form has been confirmed once in one test, `maeGetAnalysis` on that
test shows the field values the form wrote. Copying them to another test with
`maeSetAnalysis` is worth one try, but check its netlist: for the jitter
event it was not enough, and the other test's form had to be clicked too.

## Requirements

`xdotool`, `wmctrl`, `xwininfo` and ImageMagick (`import`, `convert`) on the
GUI host, and the X display Virtuoso runs on. The display is `VB_DISPLAY`, else
`--display`, else read from the running Virtuoso process. `gui windows` warns
about missing tools.

## Commands

```bash
virtuoso-bridge gui windows --window 'ADE Assembler'          # JSON: id, title, client x/y/w/h
virtuoso-bridge gui pin   --window 'strongarm maestro' --geometry 120,420,2105,800
virtuoso-bridge gui shot  --window 'Choosing Analyses' -o form.png [--scale 0.5] [--region X,Y,W,H]
virtuoso-bridge gui click --window 'Choosing Analyses' --at 84,58 [--count 2] --shot after.png
virtuoso-bridge gui type  --window 'Choosing Analyses' --at 346,338 --text '0.5/VAR("tclk")' --shot after.png
virtuoso-bridge gui key   --window 'strongarm maestro' --keys ctrl+s
```

Python: `X11Gui(runner, user)` in `virtuoso_bridge.virtuoso.x11_gui` provides
`windows`, `find`, `pin`, `screenshot`, `click`, `set_text` and `key`.

Rules the helper enforces:

* `--window` is a regex that must match exactly one window title. It never
  acts on "the focused window".
* `--at` is relative to that window's client area and must lie inside it.
  Pixel (px, py) of an unscaled `gui shot --window` image is `--at px,py`.
  With `--scale s`, divide by s.
* Commands run under `bash -c`, so a csh login shell does not break quoting.

## Procedure

1. `gui windows`, then `gui pin` the window so coordinates stay valid.
2. `gui shot` and read the coordinates off the image.
3. Act once, with `--shot`; look at the result before the next action. A
   click that misses can toggle something else. Undo it right away and say
   so.
4. Close error popups that the form raises. ADE form fields want design
   variables as `VAR("tclk")`; a bare `tclk` is rejected there, even though
   the netlister accepts it.
5. Press the form's **OK**, then save the setup (`maeSaveSetup`).
6. Verify the netlist, not the form: `client.maestro.create_netlist_for_corner`
   and `client.maestro.check_netlist_options(path, analysis, names)`, or grep
   for the expected statement (e.g. `jitterevent`).

Coordinates depend on window geometry, fonts and resolution. Do not store
them as a replay script; store the procedure and the field names.

## Etiquette

The display may be in front of the user. Say before you start that you are
going to drive their screen, keep to the named window, and leave the windows
as you found them (same tabs, no stray forms open).
