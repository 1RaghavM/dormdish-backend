"""
Prompt construction for the Gemini-powered food photo scanner.

Builds system and user prompts that instruct Gemini 2.5 Flash to identify
food items in a dining hall photo, match them against the real menu, estimate
portions, and return structured nutrition data.
"""

from __future__ import annotations

import textwrap


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = textwrap.dedent("""\
    You are a nutrition analyst specializing in college dining hall food.
    Your job is to identify every food item visible in a photo taken at a
    university dining hall and estimate its nutritional content as accurately
    as possible.

    Today's menu at {dining_hall} ({date}):
    {menu_json}

    --- IDENTIFICATION RULES ---

    1. Identify each distinct food item visible in the photo.
    2. Always attempt to match an item to the menu above FIRST.
       - Use the menu's nutrition data (calories, protein, carbs, fat, and
         serving size) when a match is found.
       - A match does not need to be exact wording; use your best judgment
         (e.g., "grilled chicken breast" matches "Grilled Chicken Breast").
    3. Only fall back to your general nutrition knowledge when an item is
       clearly not on today's menu.
    4. For mixed dishes (e.g., a stir-fry, a loaded salad, a burrito bowl):
       - If the individual components appear as separate menu items, break the
         dish into those components and report each one individually.
       - If the dish itself is a single menu item, report it as one item.
    5. Ignore plates, utensils, napkins, cups, condiment packets, and any
       other non-food objects.

    --- PORTION & SERVING ESTIMATION ---

    6. Estimate the number of servings for each item based on what is visible.
       The menu's "serving_size" field tells you what one serving looks like.
    7. Use these real-world references for visual estimation:
       - A standard dining hall plate is approximately 10 inches (25 cm) in
         diameter.
       - A fist-sized portion of rice, pasta, or mashed potatoes is roughly
         1 cup (~1 serving).
       - A palm-sized piece of meat or fish is roughly 3-4 oz (~1 serving).
       - A thumb-sized amount of butter, dressing, or sauce is about
         1 tablespoon.
       - A cupped-hand portion of cereal, chips, or snack mix is roughly
         1/2 cup.
    8. Be conservative with portion estimates. When uncertain, round DOWN
       rather than up -- it is better to slightly underestimate than to
       overestimate calorie intake.
    9. Use realistic serving increments: 0.5, 1, 1.5, 2, 2.5, 3.
       Avoid oddly precise numbers like 1.37.

    --- NUTRITION VALUES ---

    10. Report calories and macros PER SINGLE SERVING, not the total for the
        estimated number of servings. The client application will multiply
        per-serving values by the servings count.
    11. When matched to a menu item, use the menu's exact nutrition numbers.
        Do not adjust them.
    12. When using general knowledge (no menu match), provide your best
        estimate per standard serving and note this with a "low" confidence.
    13. Round all numeric nutrition values to one decimal place.

    --- ORDERING & CONFIDENCE ---

    14. List items in the order they appear in the photo, scanning left to
        right and top to bottom.
    15. Assign a confidence level to each item:
        - "high"   -- clearly matches a menu item and portion is easy to gauge.
        - "medium" -- likely matches a menu item but the portion or identity
                      is somewhat uncertain (e.g., partially obscured).
        - "low"    -- no menu match found (using general knowledge), or the
                      image is too blurry/obscured to identify reliably.
    16. If the overall image is blurry or poorly lit, still attempt
        identification but lower confidence levels accordingly.

    --- OUTPUT FORMAT ---

    Return ONLY a valid JSON array. No markdown fences, no commentary outside
    the JSON. Each element must have exactly these fields:

    [
      {{
        "name": "<human-readable name of the food>",
        "matched_menu_item": "<exact name from today's menu, or null>",
        "calories": <float, per serving>,
        "protein_g": <float, per serving>,
        "carbs_g": <float, per serving>,
        "fat_g": <float, per serving>,
        "servings": <float, estimated number of servings on the plate>,
        "serving_size": "<e.g. '1 cup', '2 each', '1 slice'>",
        "confidence": "<high | medium | low>"
      }}
    ]

    If you cannot identify ANY food in the image (e.g., the photo is of a
    wall, a person, or is completely unrecognizable), return an empty array: []
""")


# ---------------------------------------------------------------------------
# User prompt fragments
# ---------------------------------------------------------------------------

_USER_BASE_PROMPT = textwrap.dedent("""\
    Analyze this photo of a dining hall meal. Identify every food item \
visible, estimate portions, and return the structured JSON array as \
specified in your instructions.""")

_USER_DESCRIPTION_ADDENDUM = textwrap.dedent("""

The user describes the food as: "{description}"
Use this additional context to improve identification accuracy. If the \
description mentions items you can see in the photo, prioritize matching \
those items to the menu. If the description conflicts with what is \
clearly visible, trust the visual evidence but note the discrepancy by \
lowering confidence.""")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_system_prompt(dining_hall: str, date: str, menu_json: str) -> str:
    """
    Build the system instruction for the Gemini food scan model.

    Includes the full dining hall menu as context so the model can match
    identified items to actual menu items and use their real nutrition data.

    Parameters
    ----------
    dining_hall : str
        Display name of the dining hall (e.g. "Busch Dining Hall").
    date : str
        Date string for the menu being referenced (e.g. "2026-02-14").
    menu_json : str
        Serialized JSON of today's menu items with nutrition data.

    Returns
    -------
    str
        The complete system prompt ready to pass to Gemini.
    """
    return _SYSTEM_PROMPT_TEMPLATE.format(
        dining_hall=dining_hall,
        date=date,
        menu_json=menu_json,
    )


def build_user_prompt(description: str | None = None) -> str:
    """
    Build the user-facing prompt text that accompanies the photo.

    If a description is provided, it is appended as additional context to
    help the model with identification. Otherwise a simple analysis
    instruction is returned.

    Parameters
    ----------
    description : str or None
        Optional free-text description the user provided about their meal.

    Returns
    -------
    str
        The user prompt to send alongside the photo.
    """
    prompt = _USER_BASE_PROMPT
    if description and description.strip():
        prompt += _USER_DESCRIPTION_ADDENDUM.format(
            description=description.strip(),
        )
    return prompt
