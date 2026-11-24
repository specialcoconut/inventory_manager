"""Two-tier (Major/Subcategory) handling of Grocy product groups."""

from __future__ import annotations

from typing import Iterable, Optional

UNCATEGORIZED = "Uncategorized"

# Suggested kitchen category tree, seeded into Grocy as "Major<delimiter>Sub"
# product groups via POST /api/categories/seed-defaults (existing groups are kept).
DEFAULT_CATEGORY_TREE: dict[str, tuple[str, ...]] = {
    "Bakery": (
        "Bread & Rolls",
        "Wraps, Flatbreads & Pita",
        "Crumpets, Muffins & Bagels",
        "Pastries & Cakes",
    ),
    "Condiments": (
        "Table Sauces",
        "Asian & Specialty Sauces",
        "Spreads, Jams & Honey",
        "Pickles, Olives & Relishes",
        "Salad Dressings",
    ),
    "Cooking": (
        "Oils, Ghee & Cooking Sprays",
        "Vinegars & Cooking Wine",
        "Flour, Sugar & Baking Mixes",
        "Herbs, Spices & Seasonings",
        "Nuts, Seeds & Dried Fruit",
    ),
    "Drinks": (
        "Coffee & Tea",
        "Soft Drinks & Mixers",
        "Juice & Cordial",
        "Water & Sparkling Water",
        "Sports & Energy Drinks",
    ),
    "Frozen": (
        "Frozen Meat & Seafood",
        "Frozen Vegetables & Fruit",
        "Frozen Meals & Pizza",
        "Chips, Wedges & Pastry",
        "Ice Cream & Frozen Desserts",
    ),
    "Meat": (
        "Beef & Lamb",
        "Poultry",
        "Pork",
        "Seafood & Fish",
        "Deli & Cured Meats",
    ),
    # Products in this major get circle-a-day "Use by" labels and the
    # consume-or-open question at the kiosk.
    "Long-Life": (
        "UHT Milk & Plant Milks",
        "Long-Life Cream & Custard",
        "Liquid Stocks & Broths",
        "Jarred Simmer & Pasta Sauces",
        "Spices & Herbs",
        "Long-Life Juice & Coconut Milk/Cream",
        "Canned Meals, Beans & Tomatoes",
    ),
    "Pantry": (
        "Pasta, Rice & Grains",
        "Breakfast Cereals & Oats",
        "Dry Pulses, Lentils & Noodles",
        "Crackers, Breadcrumbs & Wraps",
    ),
    "Snacks": (
        "Chips, Popcorn & Savoury Snacks",
        "Biscuits & Sweet Treats",
        "Chocolate & Lollies",
        "Muesli Bars & Fruit Snacks",
    ),
    "Uncategorised": ("Needs Sorting",),
}


def default_group_names(delimiter: str = "/") -> list[str]:
    """Full Grocy product group names for the default category tree."""
    return [f"{major}{delimiter}{sub}" for major, subs in DEFAULT_CATEGORY_TREE.items() for sub in subs]


def split_group_name(name: Optional[str], delimiter: str = "/") -> tuple[str, Optional[str]]:
    """Split a Grocy product group name like ``Dairy/Butter`` into ``("Dairy", "Butter")``.

    Only the first delimiter is used, so ``Long Life/Sauces/Pasta`` becomes
    ``("Long Life", "Sauces/Pasta")``. Groups without a delimiter have no subcategory.
    """
    if not name or not name.strip():
        return UNCATEGORIZED, None
    major, sep, sub = name.partition(delimiter)
    major = major.strip() or UNCATEGORIZED
    sub = sub.strip() if sep else ""
    return major, (sub or None)


def build_category_tree(groups: Iterable[dict], delimiter: str = "/") -> list[dict]:
    """Build ``[{"name": major, "subcategories": [{"name", "group_ids"}], "group_ids": [...]}]``."""
    majors: dict[str, dict] = {}
    for group in groups:
        major, sub = split_group_name(group.get("name"), delimiter)
        entry = majors.setdefault(major, {"name": major, "group_ids": [], "subcategories": {}})
        gid = int(group["id"])
        entry["group_ids"].append(gid)
        if sub:
            sub_entry = entry["subcategories"].setdefault(sub, {"name": sub, "group_ids": []})
            sub_entry["group_ids"].append(gid)
    tree = []
    for major in sorted(majors.values(), key=lambda m: m["name"].lower()):
        tree.append(
            {
                "name": major["name"],
                "group_ids": sorted(major["group_ids"]),
                "subcategories": sorted(major["subcategories"].values(), key=lambda s: s["name"].lower()),
            }
        )
    return tree
