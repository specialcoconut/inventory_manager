"""Two-tier (Major/Subcategory) handling of Grocy product groups."""

from __future__ import annotations

from typing import Iterable, Optional

UNCATEGORIZED = "Uncategorized"


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
