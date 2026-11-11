SAMPLE := examples/FLOWLINE_DEMO_JAFFLESHOP.xml
UV ?= uv

.PHONY: install test e2e convert golden lint inspect clean

install:  ## create .venv with the converter, dbt-duckdb and test tools
	$(UV) sync

test:  ## unit + sample tests (fast, offline)
	$(UV) run pytest

e2e:  ## convert -> dbt build on DuckDB -> compare with dbt-labs/jaffle-shop; writes verify/REPORT.md
	$(UV) run pytest -m e2e -s

convert:  ## convert the sample into examples/jaffle_dbt
	$(UV) run pwc-to-dbt convert $(SAMPLE) -o examples/jaffle_dbt

inspect:  ## print each mapping's flow per target
	$(UV) run pwc-to-dbt inspect $(SAMPLE)

golden:  ## accept the current converter output as the golden files
	UPDATE_GOLDEN=1 $(UV) run pytest tests/test_sample.py -q

lint:
	$(UV) run ruff check src tests verify/*.py

clean:
	rm -rf verify/work verify/reference out
