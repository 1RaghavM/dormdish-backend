"""Pydantic models for the food photo scan feature.

Endpoints covered:
  POST /food-scan/analyze   – multipart form (request handled via FastAPI File/Form)
  POST /food-scan/feedback  – JSON body
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


# ── Shared / Building-block models ───────────────────────────────────────────


class FoodScanItem(BaseModel):
    """A single food item identified in the scanned photo.

    Each item may optionally be matched to a known dining-hall menu item.
    The ``confidence`` field indicates how certain the vision model is about
    the identification (``"high"``, ``"medium"``, or ``"low"``).
    """

    name: str
    matched_menu_item: Optional[str] = None
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float
    servings: float
    serving_size: Optional[str] = None
    confidence: str  # "high", "medium", "low"


class FoodScanTotals(BaseModel):
    """Aggregate macro-nutrient totals across all items in a scan.

    Calculated server-side by summing the per-item values.
    """

    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float


# ── Analyze endpoint (response only – request uses multipart Form/File) ─────


class FoodScanResponse(BaseModel):
    """Response returned by ``POST /food-scan/analyze``.

    Contains the list of detected food items, their aggregated totals,
    and metadata about the dining hall and date of the scan.
    """

    success: bool
    scan_id: str
    items: List[FoodScanItem]
    totals: FoodScanTotals
    dining_hall: str
    date: str


# ── Feedback endpoint ───────────────────────────────────────────────────────


class FoodScanFeedbackRequest(BaseModel):
    """Request body for ``POST /food-scan/feedback``.

    Submitted by the client after a user reviews (and optionally edits) the
    scan results.  Both the original and edited item lists are sent so the
    backend can compute a diff for model-improvement analytics.
    """

    scan_id: str
    accurate: bool
    original_items: List[FoodScanItem]
    edited_items: List[FoodScanItem]
    dining_hall: str
    date: str


# ── Gemini structured-output schema ─────────────────────────────────────────


class FoodScanGeminiItem(BaseModel):
    """Schema for a single item inside the Gemini structured-output response.

    Mirrors ``FoodScanItem`` but omits ``matched_menu_item`` (menu matching
    is performed server-side after the vision model returns).
    """

    name: str
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float
    servings: float
    serving_size: Optional[str] = None
    confidence: str  # "high", "medium", "low"


class FoodScanGeminiSchema(BaseModel):
    """Top-level schema used for Gemini JSON-mode (structured output).

    The vision model returns only the detected items; totals are computed
    server-side to ensure consistent rounding with the rest of the nutrition
    engine.
    """

    items: List[FoodScanGeminiItem]
