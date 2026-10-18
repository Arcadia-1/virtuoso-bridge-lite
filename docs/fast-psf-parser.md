# Optional PSF ASCII parser

`virtuoso_bridge.spectre.parsers_fast` supplies `parse_file_fast`,
`parse_dir_fast`, `signal_stats` and `measure_period`. It is opt-in; the
existing parser and simulation defaults are unchanged. `parse_file_fast`
accepts `want` to select signals while retaining the sweep vector.

The parser targets swept PSF ASCII data and does not claim support for every
PSF representation. Validate representative files before replacing an existing
reader. RMS means square root of mean square, including the DC component.
No performance benchmark or live Cadence compatibility claim is made here.
