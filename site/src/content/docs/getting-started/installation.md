---
title: Installation
description: Install orlalabs-caliper with pip or uv, plus the extras you need.
---

Caliper is published on PyPI as [`orlalabs-caliper`](https://pypi.org/project/orlalabs-caliper/). It needs Python 3.12 or newer.

```bash
pip install orlalabs-caliper
# or, in a uv project
uv add orlalabs-caliper
# or, without installing anything
uvx --from orlalabs-caliper caliper --help
```

This gives you the `caliper` command (`eval`, `sample`, `kappa`, `label`, `example`) and `caliper-compile`. The core runs offline on the fake provider, with no API keys.

## Extras

Add extras for what you use, e.g. `pip install 'orlalabs-caliper[anthropic,label]'`.

| Extra | For |
| --- | --- |
| `anthropic` | Claude through the Anthropic API |
| `bedrock` | Claude on Amazon Bedrock |
| `bedrock-converse` | Any Bedrock model through the Converse API (Nova, Llama, ...) |
| `label` | The `caliper label` web UI |
| `server`, `postgres`, `e2b` | `caliper-server` and its store and sandbox |
| `all` | Everything above except `e2b` |

Import names are `caliper_eval`, `caliper_label`, `caliper_kappa`, `caliper_sdk` and so on.

## In CI

Add `orlalabs-caliper` with the provider extra you need to your project's dependencies, then run the eval as a step. The [CI gate](/eval/guide/#ci-gate) section has a complete GitHub Actions job that posts the verdict on the pull request.
