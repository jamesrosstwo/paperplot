# paperplot and the plot designer

- `paperplot/`: a small matplotlib styling library for paper figures (themes, fonts, grouped bars, intervals).
- `designer/`: a web app for designing paperplot figures inside a LaTeX paper. You change controls, the figure redraws in place on the
  compiled page, you edit the surrounding TeX, recompile, and commit the figure to the paper's git repo
  ([designer/README.md](designer/README.md), build contract in [designer/CONTRACT.md](designer/CONTRACT.md)).

## Where things live

This repository is the source of truth for the code. The running service is the `paperplot-designer` Docker
container on borabora, built from a checkout at `~/paperplot`.

| What | Where | In this repo? |
|---|---|---|
| Code, tests, Docker files | this repo | yes |
| Seed specs (`designer/seeds/`) and test fixtures (`designer/tests/fixtures/`) | this repo | yes; **made-up example data** |
| Real figure specs, data registry, cache | the designer state dir (borabora: `~/designer_state`) | no |
| Data tables | read-only data dirs mounted into the container | no; linked by reference, never copied |
| The paper | its own git clone (borabora: `~/paper-rss`, remote: Overleaf) | no |
| Deployment settings, token, git credentials | `~/.config/plot-designer.env`, `~/.config/plot-designer.token`, `~/.git-credentials` | no |

Never commit real eval results here: seeds and fixtures must stay made-up.

## Develop, test, deploy (borabora)

```bash
cd ~/paperplot
designer/docker/test.sh                       # full suite inside the designer image (repo mounted read-only)
designer/docker/redeploy.sh                   # build, test, replace the container, wait until healthy
git push                                      # github.com/jamesrosstwo/paperplot
```

- Spec edits need no rebuild: specs are read from the state dir on every render. Rebuild only after code changes.
- `designer/docker/test.sh --update-references` re-renders the reference images in `designer/tests/fixtures/reference/`
  after an intended visual change; review the diff before committing.
- First-time setup, settings and troubleshooting: [designer/docker/README.md](designer/docker/README.md).
