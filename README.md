# Deterministic Meal Planner

A production-ready meal-planning engine that replaces heuristic AI prompts with a deterministic OR-Tools solver, hard nutrition constraints, and LM Studio structured output support.

## Highlights

- **Deterministic nutrition math** – a canonical nutrition engine handles all conversions and rounding.
- **CP-SAT optimization** – discrete portion steps, meal share windows, per-item caps, and diet filters enforced at solve time.
- **Repair loop** – automatic local adjustments keep solutions inside ±5% calorie/protein tolerance without human intervention.
- **FastAPI service** – `/solve`, `/sum`, and `/schema` endpoints for backend or LM Studio workflows.
- **Tooling for LM Studio** – function-calling spec plus response schema with hard enums per menu item.

## Project layout

```
.
├─ pyproject.toml
├─ src/mealplanner
│  ├─ api.py
│  ├─ lmstudio_tools.py
│  ├─ nutrition_engine.py
│  ├─ planner.py
│  ├─ schemas.py
│  ├─ solver.py
│  └─ taxonomy.py
└─ tests
   ├─ data/fixtures_menu.json
   ├─ test_end_to_end.py
   ├─ test_nutrition_engine.py
   └─ test_solver_feasibility.py
```

## Requirements

- Python 3.11 (or newer 3.12) with `pip`
- OR-Tools native dependencies (installed automatically via `pip`)
- Optional: [uv](https://docs.astral.sh/uv) for faster dependency management

## Quickstart

```bash
# clone repository if needed
$ git clone <repo>
$ cd dormdish-backend

# create a virtual environment (python 3.11+ assumed)
$ python3.11 -m venv .venv
$ source .venv/bin/activate

# install dependencies in editable mode
$ pip install -e '.[tests]'

# run the test suite
$ pytest
```

> **Note**: The test run requires Python 3.11+. Attempting to install with Python 3.9/3.10 will fail with a version constraint error.

## Running the API

```bash
$ uvicorn mealplanner.api:app --reload
```

Endpoints:

| Endpoint | Method | Description |
| --- | --- | --- |
| `/solve` | POST | Accepts `MealRequest` payload and returns a solved `MealPlan`. |
| `/sum` | POST | Re-computes totals for an existing `MealPlan` when provided the canonical menu. |
| `/schema` | GET | Generic JSON schema for the `MealPlan` model (without per-item enums). |
| `/schema` | POST | Given a `MealRequest`, returns a strict schema suitable for LM Studio response_format. |

Example `curl` request:

```bash
curl -X POST http://localhost:8000/solve \
  -H 'Content-Type: application/json' \
  -d @tests/data/example_request.json
```

## LM Studio integration

```python
from mealplanner.lmstudio_tools import build_tool_spec, get_response_format_schema, invoke_tool
from mealplanner.planner import MealPlanner
from mealplanner.schemas import MealRequest

menu = [...]  # list of menu items matching schemas.MenuItem
request = MealRequest(...)
planner = MealPlanner()

# Register tool with LM Studio
spec = build_tool_spec()
response_schema = get_response_format_schema(menu)

# Handle tool call payload
def solve_meal_plan_tool(payload: dict):
    return invoke_tool(planner, payload)
```

- `build_tool_spec()` exposes the OpenAI-compatible function signature.
- `get_response_format_schema()` returns the strict JSON schema to pass as `response_format`.
- `invoke_tool()` validates payloads and delegates to the deterministic planner.

## Nutrition sums and verification

All calorie/protein math flows through `mealplanner.nutrition_engine`:

```python
from mealplanner import nutrition_engine
from mealplanner.schemas import MealPlan

nutrition_engine.configure_menu(menu)  # canonical menu items
plan = planner.solve(request)
totals = nutrition_engine.sum_plan(plan)
```

Totals are rounded deterministically (kcal to nearest integer, protein grams to nearest gram).

## Testing strategy

- **Unit tests** (`tests/test_nutrition_engine.py`) – conversion correctness, rounding, and nutrition totals.
- **Solver tests** (`tests/test_solver_feasibility.py`) – verifies feasibility across calorie/protein targets and share windows.
- **End-to-end tests** (`tests/test_end_to_end.py`) – validates planner output, per-item caps, and FastAPI contract.

Run with:

```bash
$ pytest
```

## Troubleshooting

- Ensure Python ≥3.11. Use `python3.11` from [`pyenv`](https://github.com/pyenv/pyenv) or system packages if not available by default.
- If OR-Tools wheels fail, update `pip` and `setuptools` before installation: `pip install --upgrade pip setuptools`.
- For strict schema generation, guarantee that menu item names match taxonomy aliases so the structured output enums remain synchronized with the solver.