---
title: 'Python tooling: use uv everywhere'
slug: python-uv-policy
profile: amber
host: any
importance: 3
superseded_by: null
tags:
- python
- tooling
- policy
grounding: ok
description: 'New Python projects use uv: `uv venv --python 3.13 .venv`, dependencies
  in pyproject.toml, `uv pip install -e ''.[dev]''`'
volatility: durable
---
New Python projects use uv: `uv venv --python 3.13 .venv`, dependencies in pyproject.toml, `uv pip install -e '.[dev]'`. No global pip installs, no conda. CLI tools go in with `uv tool install`.
