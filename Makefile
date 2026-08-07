.PHONY: install test lint run init demo
install:
	python -m pip install -e '.[dev]'
test:
	pytest -q
lint:
	ruff check .
run:
	uvicorn wb_review_assistant.web:app --reload
init:
	wb-reviews init-db
demo:
	wb-reviews seed-demo

