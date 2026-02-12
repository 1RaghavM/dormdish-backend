import requests
from bs4 import BeautifulSoup, NavigableString
import json
import re
import sys
from datetime import datetime
from urllib.parse import urljoin, urlencode, urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "DormDishBot/1.0 (+https://dormdish.com) Python-requests",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

BASE_PICKMENU = (
    "https://menuportal23.dining.rutgers.edu/foodpronet/pickmenu.aspx"
)


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def title_case_name(name: str | None) -> str:
    """Convert menu item name to proper title case.
    Handles common food name patterns and preserves special cases.
    """
    if not name:
        return ""
    name = _clean(name)
    if not name:
        return ""
    
    # If already mostly lowercase or mixed case, just capitalize first letter of each word
    # If all caps, use title() but handle special cases
    if name.isupper():
        # Use title() but fix common issues
        name = name.title()
        # Fix common food name patterns
        # Preserve "And" in "Mac And Cheese"
        name = re.sub(r'\bAnd\b', 'and', name, flags=re.IGNORECASE)
        # Preserve "Or" in "X Or Y"
        name = re.sub(r'\bOr\b', 'or', name, flags=re.IGNORECASE)
        # Common prefixes that should be lowercase
        name = re.sub(r'\bOf\b', 'of', name, flags=re.IGNORECASE)
        name = re.sub(r'\bThe\b', 'the', name, flags=re.IGNORECASE)
        name = re.sub(r'\bA\b', 'a', name, flags=re.IGNORECASE)
        # Capitalize first letter
        if name:
            name = name[0].upper() + name[1:]
    else:
        # Already mixed case, just ensure first letter is capitalized
        if name:
            name = name[0].upper() + name[1:]
    
    return name


def build_menu_url(date_str: str, location_num: str = "03", location_name: str = "Livingston Dining Commons") -> str:
    # FoodPro expects M/D/YYYY
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        dt_param = f"{dt.month}/{dt.day}/{dt.year}"
    except ValueError:
        # Assume already M/D/YYYY
        dt_param = date_str
    params = {
        "sName": "Rutgers University Dining",
        "dtdate": dt_param,
        "locationNum": location_num,
        "locationName": location_name,
        "mealName": "",
        "naFlag": "1",
    }
    return f"{BASE_PICKMENU}?{urlencode(params)}"


def fetch(url: str, timeout: float = 15.0) -> BeautifulSoup:
    r = SESSION.get(url, timeout=timeout)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


# --- New robust parsing helpers ---

CATEGORY_RE = re.compile(r"--\s*(.+?)\s*--")
# Allow caps with spaces and punctuation, but exclude digits
ALL_CAPS_NO_DIGITS_RE = re.compile(r"^[A-Z /&'()\-]{4,}$")
NUTRITION_HREF_RE = re.compile(r"(label\.aspx|nutritionlabel\.aspx|nutritionlabel|nutrition\.aspx)", re.I)


EXCLUDED_CATEGORY_TERMS = {
    "QUICK LINKS",
    "NUTRITIONAL INFORMATION",
    "NUTRITION INFORMATION",
    "NUTRITION FACTS",
    "NUTRITIONIST",
    "SPECIALS",
}

UNITS_OR_QTY_TERMS = {
    "EACH", "OZ", "CUP", "CUPS", "TBSP", "TSP", "SLICE", "SLICES", "PC", "PCS", "EA", "ML", "G", "LB", "LBS",
}

# Avoid choosing these UI labels as item names
BAD_NAME_LABELS = {"PORTION SIZE", "SERVING SIZE", "PORTION", "SIZE", "ITEM", "MENU ITEM"}


def _is_category_text(text: str) -> str | None:
    """Return normalized category name if the text looks like a category header."""
    t = _clean(text)
    if not t:
        return None
    m = CATEGORY_RE.search(t)
    if m:
        cand = _clean(m.group(1))
        if cand.upper() not in EXCLUDED_CATEGORY_TERMS:
            return cand
        return None
    # Avoid matching 'NUTRITIONAL INFORMATION', 'QUICK LINKS', etc.
    if any(term in t.upper() for term in EXCLUDED_CATEGORY_TERMS):
        return None
    # Exclude lines that contain digits or quantity-like tokens
    if re.search(r"\d", t):
        return None
    tokens = t.upper().split()
    if any(tok in UNITS_OR_QTY_TERMS for tok in tokens):
        return None
    # All caps with no digits and reasonable length
    if ALL_CAPS_NO_DIGITS_RE.fullmatch(t) and len(t) <= 80 and " " in t:
        return t
    return None


def _extract_name_and_portion_from_container(container, anchor=None) -> tuple[str | None, str | None]:
    """Given a row-like container, extract item name and serving size.
    - Prefer grid layout siblings where name is under div.col-1 and portion under div.col-2.
    - Strip any leading quantity/unit from the name candidate (e.g., '2 oz', '1 each').
    - Fallbacks keep previous behavior but avoid choosing quantity-like or label-like text as the name.
    """
    name: str | None = None
    serving_size: str | None = None

    def _looks_like_quantity_text(s: str) -> bool:
        if not s:
            return False
        t = _clean(s)
        return bool(re.search(r"\b(\d+(?:\.\d+)?(?:\s+\d+/\d+)?)\s*(oz|fl\.?\s*oz|cup|cups|tbsp|tsp|slice|slices|pc|pcs|each|ea|ml|g|gram|grams|lb|lbs|serving|portion)\b", t, re.I))

    def _strip_leading_quantity(s: str) -> str:
        t = _clean(s)
        m = re.match(r"^\s*(\d+(?:\.\d+)?(?:\s+\d+/\d+)?)\s*(oz|fl\.?\s*oz|cup|cups|tbsp|tsp|slice|slices|pc|pcs|each|ea|ml|g|gram|grams|lb|lbs|serving|portion)s?\b\s*([\-–:|•]\s*)?", t, re.I)
        if m:
            rest = _clean(t[m.end():])
            # Only return rest if there is something meaningful left
            if rest and not _looks_like_quantity_text(rest):
                return rest
        return t

    def _is_bad_name_label(s: str) -> bool:
        t = _clean(s).upper()
        # normalize trailing colon
        t = t[:-1] if t.endswith(":") else t
        return t in BAD_NAME_LABELS

    def _strip_leading_labels(s: str) -> str:
        # Remove leading UI labels like 'Item', 'Menu Item', 'Portion Size', etc.
        t = _clean(s)
        t = re.sub(r"^\s*(item|menu\s*item|portion\s*size|serving\s*size|portion|serving)\s*:?-?\s*", "", t, flags=re.I)
        # If the string starts with the label alone, drop it
        if _is_bad_name_label(t):
            return ""
        return t

    def _pick_non_label_line(text: str) -> str:
        # Split on newlines or 2+ spaces and pick the last non-label, non-quantity line
        parts = [ _clean(p) for p in re.split(r"[\r\n]+|\s{2,}", text) if _clean(p) ]
        parts = [p for p in parts if not _is_bad_name_label(p) and 'nutrit' not in p.lower()]
        if not parts:
            return _clean(text)
        # Prefer the longest meaningful piece
        parts.sort(key=len, reverse=True)
        for p in parts:
            cand = _strip_leading_quantity(_strip_leading_labels(p))
            if cand and not _looks_like_quantity_text(cand) and not _is_bad_name_label(cand):
                return cand
        return _clean(text)

    # If we have the anchor, try to rely on content immediately before it
    if anchor is not None:
        # First, handle common grid: anchor inside div.col-3; name label in div.col-1; portion in div.col-2
        try:
            col3 = anchor.find_parent(lambda tag: getattr(tag, 'name', None) == 'div' and 'col-3' in (tag.get('class') or [])) if hasattr(anchor, 'find_parent') else None
        except Exception:
            col3 = None
        if col3 and col3.parent and not name:
            row_container = col3.parent
            # Prefer label within col-1
            try:
                el_col1 = row_container.find(lambda tag: getattr(tag, 'name', None) == 'div' and 'col-1' in (tag.get('class') or []))
            except Exception:
                el_col1 = None
            if el_col1:
                lbl = el_col1.find('label') if hasattr(el_col1, 'find') else None
                if lbl:
                    cand = lbl.get('aria-label') or _clean(lbl.get_text(' '))
                else:
                    cand = _clean(el_col1.get_text(' '))
                cand = _strip_leading_quantity(_strip_leading_labels(_clean(cand)))
                if cand and 'nutrit' not in cand.lower() and not _looks_like_quantity_text(cand) and not _is_bad_name_label(cand):
                    name = cand
            # Portion from col-2
            if not serving_size:
                try:
                    el_col2 = row_container.find(lambda tag: getattr(tag, 'name', None) == 'div' and 'col-2' in (tag.get('class') or []))
                except Exception:
                    el_col2 = None
                if el_col2:
                    tp = _strip_leading_labels(_clean(el_col2.get_text(' ')))
                    if tp and 'nutrit' not in tp.lower() and (_looks_like_quantity_text(tp) or 'portion' in tp.lower() or 'serving' in tp.lower()):
                        serving_size = tp

        # Try anchor title attribute, often like: Nutritional Information for "Grilled Chicken"
        if not name:
            title = anchor.get("title")
            if title:
                t = _clean(title)
                # Remove the leading nutrition phrase if present
                t = re.sub(r"\bNutrition(al)?\s+Information\s+for\s+", " ", t, flags=re.I)
                # Extract quoted content if available
                m_title = re.search(r"[\"“](.+?)[\"”]", t)
                if m_title:
                    cand = _clean(m_title.group(1))
                    cand = _strip_leading_quantity(_strip_leading_labels(cand))
                    if cand and not _is_bad_name_label(cand):
                        name = cand
                else:
                    t = _strip_leading_quantity(_strip_leading_labels(t))
                    if t and "nutrit" not in t.lower() and not _looks_like_quantity_text(t) and not _is_bad_name_label(t):
                        name = t

        # Prefer direct lookup in the same container: name in div.col-1, portion in div.col-2
        if not name and hasattr(container, 'find'):
            try:
                el_name = container.find(lambda tag: getattr(tag, 'name', None) in ('div', 'span', 'td') and 'col-1' in (' '.join(tag.get('class') or [])))
            except Exception:
                el_name = None
            if el_name:
                t = _clean(el_name.get_text(" "))
                # Try to pick content line, skipping label like 'Item'
                t = _pick_non_label_line(t)
                t = _strip_leading_quantity(_strip_leading_labels(t))
                if t and 'nutrit' not in t.lower() and not _looks_like_quantity_text(t) and not _is_bad_name_label(t):
                    name = t
            if not serving_size:
                try:
                    el_portion = container.find(lambda tag: getattr(tag, 'name', None) in ('div', 'span', 'td') and 'col-2' in (' '.join(tag.get('class') or [])))
                except Exception:
                    el_portion = None
                if el_portion:
                    tp = _clean(el_portion.get_text(" "))
                    tp = _strip_leading_labels(tp)
                    if tp and 'nutrit' not in tp.lower() and (_looks_like_quantity_text(tp) or 'portion' in tp.lower() or 'serving' in tp.lower()):
                        serving_size = tp

        # Grid layout (anchor inside col-3): walk previous siblings to find col-1 name and col-2 portion
        if anchor and not name:
            col3 = anchor.find_parent(lambda tag: getattr(tag, 'name', None) == 'div' and 'col-3' in (tag.get('class') or [])) if hasattr(anchor, 'find_parent') else None
            if col3 and col3.parent:
                prev = col3.previous_sibling
                found_name = None
                found_portion = None
                safety = 0
                while prev is not None and safety < 10:
                    safety += 1
                    try:
                        classes = prev.get('class') if hasattr(prev, 'get') else None
                        class_str = ' '.join(classes) if classes else ''
                        text_val = _clean(prev.get_text(" ")) if hasattr(prev, 'get_text') else ""
                    except Exception:
                        class_str = ""
                        text_val = ""
                    if class_str:
                        # Name should be under col-1
                        if ('col-1' in class_str or ' item' in class_str) and not found_name:
                            cand = _pick_non_label_line(text_val)
                            cand = _strip_leading_quantity(_strip_leading_labels(cand))
                            if cand and 'nutrit' not in cand.lower() and not _looks_like_quantity_text(cand) and not _is_bad_name_label(cand):
                                found_name = cand
                        # Portion typically under col-2
                        if 'col-2' in class_str and not found_portion:
                            text_val = _strip_leading_labels(text_val)
                            if text_val and 'nutrit' not in text_val.lower() and (_looks_like_quantity_text(text_val) or 'portion' in text_val.lower() or 'serving' in text_val.lower()):
                                found_portion = text_val
                    prev = prev.previous_sibling
                if found_name:
                    name = found_name
                if found_portion and not serving_size:
                    serving_size = found_portion

    # Prefer TDs that don't contain the nutrition link and aren't labels
    tds = container.find_all("td") if hasattr(container, "find_all") else []
    td_texts = []
    for td in tds:
        t = _clean(td.get_text(" "))
        if not t:
            continue
        if re.search(r"nutrit(ion|ional)", t, re.I):
            continue
        if _is_bad_name_label(t):
            continue
        t = _strip_leading_labels(t)
        if not t or _is_bad_name_label(t):
            continue
        td_texts.append(t)
    if not name and td_texts:
        # Heuristic: choose the longest non-nutrition text but not too long
        cand = _strip_leading_quantity(_strip_leading_labels(max(td_texts, key=len)))
        if len(cand) > 2 and not _looks_like_quantity_text(cand) and not _is_bad_name_label(cand):
            name = cand

    if not name:
        # Fallback: whole container text without nutrition phrase or labels
        txt = _clean(container.get_text(" ")) if hasattr(container, "get_text") else ""
        txt = re.sub(r"\bNutritional\s+Information\b", " ", txt, flags=re.I)
        txt = re.sub(r"\bNutrition\s+Information\b", " ", txt, flags=re.I)
        txt = re.sub(r"\bNutrition\s+Facts\b", " ", txt, flags=re.I)
        txt = re.sub(r"\b(Portion\s+Size|Serving\s+Size|Item|Menu\s*Item)\b", " ", txt, flags=re.I)
        txt = _clean(txt)
        # Often name is before a dash/pipe or before multiple spaces
        candidate = _strip_leading_quantity(_strip_leading_labels(_clean(re.split(r"\s{2,}|\s\|\s| - ", txt)[0]) if txt else ""))
        if candidate and not _looks_like_quantity_text(candidate) and not _is_bad_name_label(candidate):
            name = candidate

    if name:
        name = _clean(re.sub(r"\bNutritional\s+Information\b", " ", name, flags=re.I))
        name = _clean(re.sub(r"\bNutrition\s+Information\b", " ", name, flags=re.I))
        name = _clean(re.sub(r"\bNutrition\s+Facts\b", " ", name, flags=re.I))
        # Remove stray pipes or multiple spaces
        name = _clean(name.replace("|", " "))

    return name if name else None, serving_size


def resolve_url(base_url: str, href: str | None) -> str | None:
    if not href:
        return None
    return urljoin(base_url, href)


def fetch_nutrition(url: str) -> dict:
    """Fetch nutrition info from a FoodPro nutrition page.
    Returns { calories, protein_g, serving_size, item_name } when found.
    """
    try:
        soup = fetch(url)
    except Exception:
        return {}
    text = soup.get_text(" ", strip=True)

    # Attempt to extract item/recipe title from the label page
    item_name = None
    try:
        # Common ID/class patterns used by FoodPro
        by_id = soup.find(attrs={"id": re.compile(r"(lblItemName|itemName|ItemName|recipeName|lblRecipeName|lblShortDesc|lblItemDesc|lblRecipeDesc|ctl00_Body_lblItemName)", re.I)})
        if by_id:
            item_name = _clean(by_id.get_text(" "))
        if not item_name:
            by_class = soup.find(["div", "span", "h1", "h2"], class_=re.compile(r"(item.?name|recipe.?name|item.?desc|recipe.?desc)", re.I))
            if by_class:
                item_name = _clean(by_class.get_text(" "))
        if not item_name:
            hdr = soup.find(["h1", "h2", "h3"])  # common header tag for item name
            if hdr:
                t = _clean(hdr.get_text(" "))
                if t and "nutrition" not in t.lower():
                    item_name = t
        if not item_name and soup.title and soup.title.string:
            t = _clean(soup.title.string)
            # Often title contains the item name before a dash or pipe
            t = _clean(re.split(r"\s[-|]\s", t)[0])
            if t and "nutrition" not in t.lower():
                item_name = t
        # Table-based fallbacks for name (e.g., rows like 'Item Name: Grilled Chicken')
        if not item_name:
            for row in soup.find_all(["tr", "div", "p"]):
                # Prefer th/td structure
                label = None
                value = None
                th = row.find("th")
                tds = row.find_all("td")
                if th and tds:
                    label = _clean(th.get_text(" "))
                    value = _clean(tds[-1].get_text(" "))
                else:
                    t = _clean(row.get_text(" "))
                    # Try to split on ':' pattern
                    m = re.match(r"([^:]{3,}):\s*(.+)", t)
                    if m:
                        label = _clean(m.group(1))
                        value = _clean(m.group(2))
                if not label or not value:
                    continue
                if re.search(r"^(item|recipe)\s+name$", label, re.I) or re.fullmatch(r"^(item|recipe)$", label, re.I):
                    if value and "nutrition" not in value.lower():
                        item_name = value
                        break
        # Query-string fallback
        if not item_name:
            try:
                qs = parse_qs(urlparse(url).query)
                for key in ("RecipeName", "recipename", "ItemName", "itemname", "MenuItem", "menuitem"):
                    if key in qs and qs[key]:
                        cand = _clean(qs[key][0])
                        if cand:
                            item_name = cand
                            break
            except Exception:
                pass
    except Exception:
        pass

    # Helper to get value by label from page text/rows
    def table_lookup(label: str) -> str | None:
        lbl = label.lower()
        for row in soup.find_all(["tr", "div", "p"]):
            t = _clean(row.get_text(" "))
            if not t:
                continue
            if lbl in t.lower():
                # Extract the first number/unit after the label
                m = re.search(rf"{re.escape(label)}\s*[:\-]?\s*([^|]+)", t, flags=re.I)
                if m:
                    return _clean(m.group(1))
        return None

    calories = None
    protein_g = None
    fat_g = None
    carbs_g = None
    serving = None

    # Regex across full text as fallback
    m = re.search(r"Calories\s*[:\-]?\s*(\d{1,4})", text, re.I)
    if m:
        calories = float(m.group(1))
    else:
        val = table_lookup("Calories")
        if val:
            m2 = re.search(r"(\d{1,4})", val)
            if m2:
                calories = float(m2.group(1))

    m = re.search(r"Protein\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*g", text, re.I)
    if m:
        protein_g = float(m.group(1))
    else:
        val = table_lookup("Protein")
        if val:
            m2 = re.search(r"(\d+(?:\.\d+)?)\s*g", val)
            if m2:
                protein_g = float(m2.group(1))
    
    # Extract fat
    m = re.search(r"Total\s+Fat\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*g", text, re.I)
    if m:
        fat_g = float(m.group(1))
    else:
        val = table_lookup("Total Fat")
        if val:
            m2 = re.search(r"(\d+(?:\.\d+)?)\s*g", val)
            if m2:
                fat_g = float(m2.group(1))
    
    # Extract carbs - handles "Tot. Carb.", "Total Carbohydrate", etc.
    m = re.search(r"Tot(?:al)?\.?\s*Carb(?:ohydrate)?s?\.?\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*g", text, re.I)
    if m:
        carbs_g = float(m.group(1))
    else:
        val = table_lookup("Tot. Carb")
        if not val:
            val = table_lookup("Total Carbohydrate")
        if not val:
            val = table_lookup("Total Carbs")
        if not val:
            val = table_lookup("Carbohydrate")
        if not val:
            val = table_lookup("Carbs")
        if val:
            m2 = re.search(r"(\d+(?:\.\d+)?)\s*g", val)
            if m2:
                carbs_g = float(m2.group(1))

    m = re.search(r"Serving\s*Size\s*[:\-]?\s*([^|\n\r]+)", text, re.I)
    if m:
        serving = _clean(m.group(1))
    else:
        val = table_lookup("Serving Size")
        if val:
            serving = _clean(val)

    # Trim noisy serving like "1 EACH Calories 85 ..."
    if serving:
        serving = _clean(re.split(r"\b(Calories|Amount/Serving|Protein|Total\s+Fat|Sodium)\b", serving)[0])

    out: dict = {}
    if calories is not None:
        out["calories"] = calories
    if protein_g is not None:
        out["protein_g"] = protein_g
    if fat_g is not None:
        out["g_fat"] = fat_g
    if carbs_g is not None:
        out["g_carbs"] = carbs_g
    if serving:
        out["serving_size"] = serving
    if item_name:
        out["item_name"] = item_name
    return out


# --- NEW: formatting helpers to match meals_trimmed copy.json schema ---
SERVING_PART_RE = re.compile(r"^(?P<amount>(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?))\s*(?P<unit>[A-Za-z]+)\b")


def serving_to_obj(serving: str | None) -> dict | None:
    if not serving:
        return None
    s = _clean(serving)
    # Drop any trailing parenthetical e.g., "3 oz (85g)"
    s = re.sub(r"\s*\([^)]*\)$", "", s)
    m = SERVING_PART_RE.search(s)
    if m:
        amt = _clean(m.group("amount"))
        unit = _clean(m.group("unit")).lower()
        return {"serving_size_amount": amt, "serving_size_unit": unit}
    # Handle cases like "each", "serving"
    tokens = s.lower().split()
    if len(tokens) == 1 and tokens[0] in {"each", "serving", "portion"}:
        return {"serving_size_unit": tokens[0]}
    return None


def to_meals_output(categorized: dict, meal_type: str, date: str) -> dict:
    meal_type = (meal_type or "breakfast").lower()
    top = {"breakfast": {}, "lunch": {}, "dinner": {}}
    out_list = []
    for cat, foods in categorized.items():
        block = {"category": cat, "foods": []}
        for f in foods:
            food_obj = {
                "name": title_case_name(f.get("name")),
                "calories": f.get("calories"),
                "g_protein": f.get("protein_g"),
                "g_fat": f.get("g_fat"),
                "g_carbs": f.get("g_carbs"),
                "meal_type": meal_type,
                "date": date,
            }
            sv = serving_to_obj(f.get("serving_size")) if f.get("serving_size") else None
            if sv:
                food_obj["serving_size"] = sv
            block["foods"].append(food_obj)
        out_list.append(block)
    # Always include the date key even if empty list
    if meal_type not in top:
        meal_type = "breakfast"
    top[meal_type][date] = out_list
    return top


def _iter_rows_in_order(soup: BeautifulSoup):
    """Yield likely menu row containers in document order.
    Prefer <tr>, but also fall back to <li>, <div>, and <p> structures when needed.
    """
    rows = soup.find_all("tr")
    if rows:
        for tr in rows:
            yield tr
        return
    # Fallbacks if no table rows are present
    for tag_name in ("li", "div", "p"):
        for el in soup.find_all(tag_name):
            yield el


def _nearest_prev_category(node) -> str | None:
    """Walk backwards to find the nearest category-looking text above this node."""
    for prev in node.find_all_previous(["tr", "td", "div", "p", "h2", "h3", "h4"]):
        txt = _clean(prev.get_text(" "))
        cat = _is_category_text(txt)
        if cat:
            return cat
    return None


def _looks_like_nutrition_phrase(name: str | None) -> bool:
    if not name:
        return False
    return bool(re.search(r"^\s*(nutrition|nutritional)\b", name, re.I))


# Configuration for parallel nutrition fetching
MAX_NUTRITION_WORKERS = 15  # Max concurrent requests
NUTRITION_FETCH_TIMEOUT = 8.0  # Reduced timeout for individual nutrition requests


def fetch_nutrition_batch(urls: list[str]) -> dict[str, dict]:
    """Fetch nutrition info for multiple URLs in parallel.

    Returns a dict mapping URL -> nutrition data.
    Uses ThreadPoolExecutor for concurrent HTTP requests.
    """
    if not urls:
        return {}

    results = {}

    def _fetch_single(url: str) -> tuple[str, dict]:
        try:
            # Use shorter timeout for batch fetching
            r = SESSION.get(url, timeout=NUTRITION_FETCH_TIMEOUT)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            # Extract nutrition data (simplified version of fetch_nutrition)
            text = soup.get_text(" ", strip=True)

            # Extract item name
            item_name = None
            by_id = soup.find(attrs={"id": re.compile(r"(lblItemName|itemName|ItemName|recipeName|lblRecipeName|lblShortDesc|lblItemDesc|lblRecipeDesc|ctl00_Body_lblItemName)", re.I)})
            if by_id:
                item_name = _clean(by_id.get_text(" "))
            if not item_name:
                by_class = soup.find(["div", "span", "h1", "h2"], class_=re.compile(r"(item.?name|recipe.?name|item.?desc|recipe.?desc)", re.I))
                if by_class:
                    item_name = _clean(by_class.get_text(" "))
            if not item_name:
                hdr = soup.find(["h1", "h2", "h3"])
                if hdr:
                    t = _clean(hdr.get_text(" "))
                    if t and "nutrition" not in t.lower():
                        item_name = t

            # Extract macros
            calories = None
            protein_g = None
            fat_g = None
            carbs_g = None
            serving = None

            m = re.search(r"Calories\s*[:\-]?\s*(\d{1,4})", text, re.I)
            if m:
                calories = float(m.group(1))

            m = re.search(r"Protein\s*[:\-]?\s*(\d{1,3}(?:\.\d+)?)\s*g?", text, re.I)
            if m:
                protein_g = float(m.group(1))

            m = re.search(r"(?:Total\s+)?Fat\s*[:\-]?\s*(\d{1,3}(?:\.\d+)?)\s*g", text, re.I)
            if m:
                fat_g = float(m.group(1))

            m = re.search(r"(?:Total\s+)?Carb(?:ohydrate)?s?\s*[:\-]?\s*(\d{1,3}(?:\.\d+)?)\s*g", text, re.I)
            if m:
                carbs_g = float(m.group(1))

            m = re.search(r"Serving\s+Size\s*[:\-]?\s*(.+?)(?:\s{2,}|$)", text, re.I)
            if m:
                serving = _clean(m.group(1))

            return (url, {
                "item_name": item_name,
                "calories": calories,
                "protein_g": protein_g,
                "g_fat": fat_g,
                "g_carbs": carbs_g,
                "serving_size": serving,
            })
        except Exception:
            return (url, {})

    # Use ThreadPoolExecutor for parallel fetching
    with ThreadPoolExecutor(max_workers=MAX_NUTRITION_WORKERS) as executor:
        futures = {executor.submit(_fetch_single, url): url for url in urls}
        for future in as_completed(futures):
            try:
                url, data = future.result()
                results[url] = data
            except Exception:
                results[futures[future]] = {}

    return results


def parse_menu(html: str, meal_type: str, date: str, base_url: str) -> dict:
    """Parse menu HTML and fetch nutrition data in parallel for much faster performance.

    This optimized version:
    1. First pass: collect all items with their nutrition URLs (no HTTP requests)
    2. Batch fetch all nutrition data in parallel using ThreadPoolExecutor
    3. Merge the nutrition data into items
    """
    soup = BeautifulSoup(html, "html.parser")

    # Phase 1: Collect all items with their nutrition URLs (no fetching yet)
    pending_items: list[dict] = []  # Items with nutrition_url but no nutrition data yet
    current_category: str | None = None
    seen_urls: set[str] = set()

    # Primary strategy: iterate row-like elements in order and maintain current_category
    for row in _iter_rows_in_order(soup):
        raw_text = _clean(row.get_text(" "))
        if not raw_text:
            continue

        # Find all item anchors within this row/container
        anchors_in_row = row.find_all("a", href=NUTRITION_HREF_RE)
        if anchors_in_row:
            for a in anchors_in_row:
                href = a.get("href")
                full = resolve_url(base_url, href)
                if not full or not NUTRITION_HREF_RE.search(full):
                    continue
                if full in seen_urls:
                    continue

                name, serving_size = _extract_name_and_portion_from_container(row, a)
                if not name or len(name) < 2:
                    parent_row = a.find_parent(["tr", "li", "div", "p"]) or row
                    name, serving_size = _extract_name_and_portion_from_container(parent_row, a)

                cat_for_item = current_category or _nearest_prev_category(row) or "Uncategorized"

                item = {
                    "name": title_case_name(name),
                    "category": cat_for_item,
                    "nutrition_url": full,
                    "_anchor": a,  # Keep reference for fallback name extraction
                    "_row": row,
                }
                if serving_size:
                    item["serving_size"] = serving_size

                seen_urls.add(full)
                pending_items.append(item)
            continue

        # If no item link, detect and update category
        cat = _is_category_text(raw_text)
        if cat:
            current_category = cat
            continue

    # Fallback: if still empty, directly collect all nutrition anchors
    if not pending_items:
        anchors = soup.find_all("a", href=NUTRITION_HREF_RE)
        for a in anchors:
            href = a.get("href")
            full = resolve_url(base_url, href)
            if not full or not NUTRITION_HREF_RE.search(full):
                continue
            if full in seen_urls:
                continue
            container = a.find_parent(["tr", "li", "div", "p"]) or a
            name, serving_size = _extract_name_and_portion_from_container(container, a)
            if not name:
                title = a.get("title")
                if title:
                    t = _clean(re.sub(r"\bNutrition(al)?\s+Information\s+for\s+", " ", title, flags=re.I))
                    m_title = re.search(r'["\u201c\u201d](.+?)["\u201c\u201d]', t)
                    if m_title:
                        name = _clean(m_title.group(1))
            if not name:
                ar = a.get("aria-label")
                if ar and "nutrit" not in ar.lower():
                    name = _clean(ar)
            if not name:
                at = _clean(a.get_text(" "))
                if at and "nutrit" not in at.lower():
                    name = at
            if not name:
                name = _clean(container.get_text(" ")) if hasattr(container, "get_text") else None
            cat_for_item = _nearest_prev_category(container) or "Uncategorized"

            item = {
                "name": title_case_name(name),
                "category": cat_for_item,
                "nutrition_url": full,
                "_anchor": a,
                "_row": container,
            }
            if serving_size:
                item["serving_size"] = serving_size

            seen_urls.add(full)
            pending_items.append(item)

    # Phase 2: Batch fetch all nutrition data in parallel
    nutrition_urls = [item["nutrition_url"] for item in pending_items if item.get("nutrition_url")]
    nutrition_data = fetch_nutrition_batch(nutrition_urls) if nutrition_urls else {}

    # Phase 3: Merge nutrition data into items and build final output
    items: list[dict] = []
    categorized: dict[str, list[dict]] = {}

    for item in pending_items:
        url = item.get("nutrition_url")
        nut = nutrition_data.get(url, {}) if url else {}

        # Apply nutrition data
        label_name = _clean(nut.get("item_name")) if nut.get("item_name") else None
        if label_name:
            item["name"] = title_case_name(label_name)
        if nut.get("calories") is not None:
            item["calories"] = nut.get("calories")
        if nut.get("protein_g") is not None:
            item["protein_g"] = nut.get("protein_g")
        if nut.get("g_fat") is not None:
            item["g_fat"] = nut.get("g_fat")
        if nut.get("g_carbs") is not None:
            item["g_carbs"] = nut.get("g_carbs")
        if nut.get("serving_size"):
            item["serving_size"] = nut.get("serving_size")

        # Fallback name extraction if name is still invalid
        if not item.get("name") or len(str(item.get("name"))) < 2 or _looks_like_nutrition_phrase(item.get("name")):
            a = item.get("_anchor")
            row = item.get("_row")
            if a:
                title = a.get("title")
                if title:
                    t = _clean(re.sub(r"\bNutrition(al)?\s+Information\s+for\s+", " ", title, flags=re.I))
                    m_title = re.search(r'["\u201c\u201d](.+?)["\u201c\u201d]', t)
                    if m_title:
                        item["name"] = title_case_name(_clean(m_title.group(1)))
                if not item.get("name"):
                    ar = a.get("aria-label")
                    if ar and "nutrit" not in ar.lower():
                        item["name"] = title_case_name(_clean(ar))
                if not item.get("name"):
                    at = _clean(a.get_text(" "))
                    if at and "nutrit" not in at.lower():
                        item["name"] = title_case_name(at)
            if not item.get("name") and row:
                trow = _clean(row.get_text(" "))
                trow = re.sub(r"\bNutritional\s+Information\b", " ", trow, flags=re.I)
                trow = re.sub(r"\bNutrition\s+Information\b", " ", trow, flags=re.I)
                trow = re.sub(r"\bNutrition\s+Facts\b", " ", trow, flags=re.I)
                trow = _clean(re.split(r"\s{2,}|\s\|\s| - ", trow)[0]) if trow else None
                if trow and "nutrit" not in trow.lower():
                    item["name"] = title_case_name(trow)

        # Skip items without valid names
        if _looks_like_nutrition_phrase(item.get("name")) or not item.get("name") or len(str(item.get("name"))) < 2:
            continue

        # Clean up temporary keys
        item.pop("_anchor", None)
        item.pop("_row", None)

        cat_for_item = item.get("category", "Uncategorized")
        items.append(item)
        categorized.setdefault(cat_for_item, []).append({
            "name": title_case_name(item.get("name")),
            "calories": item.get("calories"),
            "protein_g": item.get("protein_g"),
            "g_fat": item.get("g_fat"),
            "g_carbs": item.get("g_carbs"),
            "serving_size": item.get("serving_size"),
            "nutrition_url": item.get("nutrition_url"),
        })

    # Return meals_trimmed-style schema
    return to_meals_output(categorized, meal_type, date)


def main(argv: list[str]) -> int:
    # Defaults: today at Livingston Dining Commons
    today_iso = datetime.now().strftime("%Y-%m-%d")
    # Simple CLI parsing
    date = today_iso
    location_num = "03"
    location_name = "Livingston Dining Commons"
    meal_type = "breakfast"  # default to breakfast to match schema

    # Very basic arg parsing: --date YYYY-MM-DD, --location-name "...", --location-num 03
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--date" and i + 1 < len(argv):
            date = argv[i + 1]
            i += 2
        elif arg == "--location-name" and i + 1 < len(argv):
            location_name = argv[i + 1]
            i += 2
        elif arg == "--location-num" and i + 1 < len(argv):
            location_num = argv[i + 1]
            i += 2
        elif arg == "--meal" and i + 1 < len(argv):
            meal_type = argv[i + 1]
            i += 2
        else:
            i += 1

    menu_url = build_menu_url(date, location_num, location_name)
    soup = fetch(menu_url)
    menu_parsed = parse_menu(str(soup), meal_type=meal_type, date=date, base_url=menu_url)

    # menu_parsed is already in the required schema
    meals_output = menu_parsed

    with open("meals_trimmed.json", "w", encoding="utf-8") as f:
        json.dump(meals_output, f, indent=2, ensure_ascii=False)
    # Count foods for the requested meal/date
    blocks = meals_output.get(meal_type.lower(), {}).get(date, []) if isinstance(meals_output, dict) else []
    total_items = sum(len(b.get("foods", [])) for b in blocks)
    print(f"✅ Wrote meals_trimmed.json with {total_items} foods for {location_name} on {date} ({meal_type})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
