<p align="center">
  <img src="assets/banner.svg" alt="virtuoso-bridge-lite" width="100%"/>
</p>

<p align="center">
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/stargazers"><img src="https://img.shields.io/github/stars/Arcadia-1/virtuoso-bridge-lite?style=flat-square&color=f5c542&logo=github&v=20260523" alt="GitHub stars"/></a>
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/network/members"><img src="https://img.shields.io/github/forks/Arcadia-1/virtuoso-bridge-lite?style=flat-square&color=f5c542" alt="GitHub forks"/></a>
  <a href="stats/README.md"><img src="https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FArcadia-1%2Fvirtuoso-bridge-lite%2Fmain%2Fstats%2Fclones-badge.json&style=flat-square&v=2" alt="Clones"/></a>
  <a href="stats/README.md"><img src="https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FArcadia-1%2Fvirtuoso-bridge-lite%2Fmain%2Fstats%2Fviews-badge.json&style=flat-square&v=2" alt="Views"/></a>
</p>

<p align="center">
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/issues"><img src="https://img.shields.io/github/issues/Arcadia-1/virtuoso-bridge-lite?style=flat-square&color=3fb950" alt="Open Issues"/></a>
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/commits/main"><img src="https://img.shields.io/github/last-commit/Arcadia-1/virtuoso-bridge-lite?style=flat-square&color=3fb950" alt="Last Commit"/></a>
  <a href="https://virtuoso-bridge.tokenzhang.com"><img src="https://img.shields.io/badge/docs-website-blue" alt="Website"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT"/></a>
  <a href="https://github.com/Arcadia-1/virtuoso-bridge-lite/pulls"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs Welcome"/></a>
</p>

<p align="center">
  <b><a href="https://virtuoso-bridge.tokenzhang.com">virtuoso-bridge.tokenzhang.com</a></b>
  &nbsp;·&nbsp; <a href="https://virtuoso-bridge.tokenzhang.com/guide/">Guide</a>
  &nbsp;·&nbsp; <a href="https://virtuoso-bridge.tokenzhang.com/guide/?lang=zh">中文文档</a>
</p>

Let AI agents drive Cadence Virtuoso and Spectre — locally or on a remote
server — from Python, SKILL or the command line.

```bash
git clone https://github.com/Arcadia-1/virtuoso-bridge-lite.git
cd virtuoso-bridge-lite
uv venv .venv && source .venv/bin/activate
uv pip install -e .

virtuoso-bridge init user@host   # write ~/.virtuoso-bridge/.env
virtuoso-bridge start            # prepare fixed-port setup and the SSH tunnel
# Paste the printed load("...") into the intended idle CIW.
virtuoso-bridge status
```

Setup, configuration, CLI reference, architecture and citation are in the
**[guide](https://virtuoso-bridge.tokenzhang.com/guide/)**. Coding agents start with [AGENTS.md](AGENTS.md).
Automatic sessions are opt-in: set `VB_AUTO_SESSIONS=1` and optionally
`VB_REMOTE_WORK_DIR=/projects/example` to discover a registered daemon using an
OS-assigned port. A working directory alone keeps the legacy fixed-port mode.
