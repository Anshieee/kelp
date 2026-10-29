DEMO_STATE ?= var/demo

.PHONY: demo test clean-demo

demo:
	python -m demo --state "$(DEMO_STATE)"

test:
	python -m pytest

clean-demo:
	rm -rf "$(DEMO_STATE)"
