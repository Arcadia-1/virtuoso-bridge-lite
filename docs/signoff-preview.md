# Standalone signoff preview

Run `python tools/signoff.py --out preview/signoff --title "Circuit review"`.
The stdlib-only helper builds an HTML review page from locally supplied files
under `preview/shots` and `preview/data`. Use `--shots a.png,b.png` to limit
the gallery and `--wave path/to/wave.json` to select the waveform statistics.
The selected wave file scopes the statistics table; simulation summaries and
notes still come from `preview/data`. Use a dedicated checkout per circuit.
An optional `anatomy.html` can be supplied in the output folder.

`--shots` limits the current gallery, not files copied by earlier builds.
Use a fresh output directory before sharing a scoped report; inspect its files
and all supplied notes/logs for private data. Treat a supplied `anatomy.html`
as executable HTML and include it only from a trusted source. Text files use UTF-8.

Generated HTML and revision JSON are local review artifacts, not electrical
signoff evidence. The helper does not run Cadence, validate a PDK or declare
measurements correct. No captured lab images or PDK data are included here.
MCP integration can follow separately; this CLI runs without the MCP adapter.
