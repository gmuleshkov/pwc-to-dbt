# Prompts given to the coding agent

Claude Code, in a terminal session started in `~/development/repos`. Prompts are verbatim (typos included).
The agent's own questions are shown where my answer was a selection.

## 1. Initial prompt (model: Sonnet 5.5)

The assignment text, pasted in full, followed by:

> please check it invesitgate all the details tell me wha tthe task is about what we should do how we shoud it.
> in case of questions or decisions needed ask me in interactive way but make sure to include details because
> I am not really familiar with the domain. Probably I will have questions in genenral for you too. so
> investigate ask me questions and about decisions prepapre solid dev plan and also check what tools clis we
> will need and plan for instaling those locally. regarding repo we can create a new one you already have gh
> suathenitcated

## 2. Answers to the agent's questions

The agent asked four multiple-choice questions after profiling the XML and the reference projects:

| Question (shortened) | My answer |
|---|---|
| The task links the old `jaffle_shop_duckdb`, but the XML mirrors the new `dbt-labs/jaffle-shop`. Which reference? | **Both** (mention both, verify against the new one) |
| The XML has 3 mappings / 16 targets, not one; the enriched marts have no reference. Scope? | **All 3 mappings** |
| Language and how to translate Informatica expressions? | **Python + real parser** |
| Repo name and visibility? | **Public: pwc-to-dbt** |

## 3. Go-ahead

> yes go ahead. feel free to continue with next stages too. just make sure to have everything you need locally
> and make sure to thest the work you are doing e2e

## 4. After switching the model to Opus 5.5

> yes go ahead. feel free to continue with next stages too. just make sure to have everything you need locally
> and make sure to thest the work you are doing e2e. also note the model was switched to opus so review the
> requirements and the plan and do changes if needed that will improve it
