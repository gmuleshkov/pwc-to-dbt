SAMPLE := examples/FLOWLINE_DEMO_JAFFLESHOP.xml
UV ?= uv

.PHONY: install test e2e convert repair golden lint inspect demo clean

install:  ## create .venv with the converter, dbt-duckdb and test tools
	$(UV) sync

test:  ## unit + sample tests (fast, offline)
	$(UV) run pytest

e2e:  ## convert -> dbt build on DuckDB -> compare with dbt-labs/jaffle-shop; writes verify/REPORT.md
	$(UV) run pytest -m e2e -s

convert:  ## convert the sample into examples/jaffle_dbt
	$(UV) run pwc-to-dbt convert $(SAMPLE) -o examples/jaffle_dbt

repair:  ## write the sample with defects P1-P5 fixed (docs/DEFECTS.md) and convert it
	$(UV) run python verify/repair_mapping.py $(SAMPLE) examples/FLOWLINE_DEMO_JAFFLESHOP_REPAIRED.xml > examples/REPAIR_CHANGES.txt
	$(UV) run pwc-to-dbt convert examples/FLOWLINE_DEMO_JAFFLESHOP_REPAIRED.xml -o examples/jaffle_dbt_repaired

inspect:  ## print each mapping's flow per target
	$(UV) run pwc-to-dbt inspect $(SAMPLE)

golden:  ## accept the current converter output as the golden files
	UPDATE_GOLDEN=1 $(UV) run pytest tests/test_sample.py -q

demo:  ## interactive presentation of this project on http://127.0.0.1:8765 (demo/README.md)
	$(UV) run python demo/server.py

lint:
	$(UV) run ruff check src tests demo verify/*.py

clean:
	rm -rf verify/work verify/reference out
