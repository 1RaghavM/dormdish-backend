from __future__ import annotations

import json
from pathlib import Path
from typing import List, Tuple

import pytest

from mealplanner import nutrition_engine
from mealplanner.schemas import MenuItem
from mealplanner.taxonomy import Taxonomy


@pytest.fixture(scope="module")
def menu_items() -> List[MenuItem]:
    data_path = Path(__file__).parent / "data" / "fixtures_menu.json"
    data = json.loads(data_path.read_text())
    return [MenuItem.model_validate(item) for item in data]


@pytest.fixture()
def canonical_menu(menu_items: List[MenuItem]) -> Tuple[List[MenuItem], Taxonomy]:
    taxonomy = Taxonomy.from_menu(menu_items)
    canonical: dict[str, MenuItem] = {}
    for item in menu_items:
        policy = taxonomy.resolve(item.name)
        canonical[policy.name] = item.model_copy(update={"name": policy.name})
    menu = list(canonical.values())
    nutrition_engine.configure_menu(menu)
    return menu, taxonomy
