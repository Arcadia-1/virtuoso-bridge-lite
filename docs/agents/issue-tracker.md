# Issue tracker: GitHub

Issues and specs live in `Arcadia-1/virtuoso-bridge-lite`. Use `gh` for GitHub
operations, with `--repo Arcadia-1/virtuoso-bridge-lite` when outside this clone.

- Read: `gh issue view NUMBER --comments`; fetch labels with `--json`.
- Discover: `gh issue list --state open --json number,title,body,labels,comments`.
- Publish: `gh issue create --title TITLE --body-file FILE` or
  `gh issue comment NUMBER --body-file FILE`.
- Change labels: `gh issue edit NUMBER --add-label LABEL --remove-label LABEL`.
- Close after verifying the outcome: `gh issue close NUMBER --comment MESSAGE`.

## Pull requests as a triage surface

**PRs as a request surface: no.** Automatic triage discovery lists issues only.
An explicitly named PR remains in scope for review: use `gh pr view NUMBER`,
`gh pr diff NUMBER`, `gh pr checks NUMBER`, and `gh pr review NUMBER`.
GitHub shares the issue/PR number space; resolve an ambiguous number with
`gh pr view`, falling back to `gh issue view`.

When a skill says to publish a ticket, create a GitHub issue. When it says to
fetch a ticket, read its body, comments, labels, and current state.
