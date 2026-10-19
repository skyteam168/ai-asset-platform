# 薄封装：实际逻辑在 tools/dev.py（Windows 直接用 python tools/dev.py <task>）
PY ?= .venv/bin/python
.PHONY: up down migrate lint docs test ci serve
up down migrate lint docs test ci serve:
	$(PY) tools/dev.py $@
