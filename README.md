# webhook-relay

Короткое описание: что делает проект и зачем.

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # заполнить своими значениями
webhook-relay           # или: python -m webhook_relay
```

## Тесты и линтер

```bash
pytest
ruff check .
```

## Структура

```
src/webhook_relay/   код
tests/              тесты
```
