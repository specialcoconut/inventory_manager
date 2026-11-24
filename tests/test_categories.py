import time

from fastapi.testclient import TestClient

from app.categories import UNCATEGORIZED, build_category_tree, default_group_names, split_group_name
from app.grocy import GrocyClient
from app.main import create_app


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_split_group_name_major_and_sub():
    assert split_group_name("Dairy/Butter") == ("Dairy", "Butter")
    assert split_group_name(" Long Life / Sauces ") == ("Long Life", "Sauces")


def test_split_group_name_only_first_delimiter_is_used():
    assert split_group_name("Dry/Pasta/Long") == ("Dry", "Pasta/Long")


def test_split_group_name_without_sub_or_name():
    assert split_group_name("Dry") == ("Dry", None)
    assert split_group_name("Dry/") == ("Dry", None)
    assert split_group_name(None) == (UNCATEGORIZED, None)
    assert split_group_name("  ") == (UNCATEGORIZED, None)


def test_split_group_name_custom_delimiter():
    assert split_group_name("Meat > Chicken", " > ") == ("Meat", "Chicken")
    assert split_group_name("Meat/Chicken", " > ") == ("Meat/Chicken", None)


def test_build_category_tree_groups_and_sorts():
    groups = [
        {"id": 3, "name": "Meat/Chicken"},
        {"id": 1, "name": "Dairy/Milk"},
        {"id": 2, "name": "Long Life/Sauces"},
        {"id": 4, "name": "Dairy/Butter"},
        {"id": 5, "name": "Dairy"},
    ]
    tree = build_category_tree(groups)
    assert [c["name"] for c in tree] == ["Dairy", "Long Life", "Meat"]
    dairy = tree[0]
    assert dairy["group_ids"] == [1, 4, 5]
    assert [s["name"] for s in dairy["subcategories"]] == ["Butter", "Milk"]
    assert dairy["subcategories"][0]["group_ids"] == [4]


def test_default_group_names_use_delimiter():
    names = default_group_names()
    assert "Bakery/Bread & Rolls" in names
    assert "Long-Life/UHT Milk & Plant Milks" in names
    assert len(names) == len(set(names))
    custom = default_group_names(" > ")
    assert "Bakery > Bread & Rolls" in custom


def test_seed_default_categories(client, fake_grocy):
    # one default already exists (different case) and must not be duplicated
    fake_grocy.product_groups.append({"id": 50, "name": "bakery/bread & rolls"})
    res = client.post("/api/categories/seed-defaults")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["skipped"] == 1
    assert "Bakery/Bread & Rolls" not in body["created"]
    assert "Long-Life/UHT Milk & Plant Milks" in body["created"]
    names = [g["name"] for g in fake_grocy.product_groups]
    assert names.count("bakery/bread & rolls") == 1
    assert "Snacks/Chocolate & Lollies" in names
    # idempotent: running again creates nothing
    again = client.post("/api/categories/seed-defaults").json()
    assert again["created"] == []
    # seeded Long-Life groups mark their products as long-life in the catalog
    group_id = next(g["id"] for g in fake_grocy.product_groups if g["name"] == "Long-Life/Jarred Simmer & Pasta Sauces")
    updated = client.post("/api/products/13/category", json={"product_group_id": group_id}).json()
    assert updated["long_life"] is True


def test_startup_seeds_default_categories(settings, fake_grocy, printer, fake_off):
    from app.external import ExternalProductLookup

    settings.seed_default_categories = True
    grocy = GrocyClient(settings.grocy_api_base, settings.grocy_api_key, transport=fake_grocy.transport())
    external = ExternalProductLookup("http://off.test", transport=fake_off.transport())
    app = create_app(settings, grocy=grocy, printer=printer, external=external)
    with TestClient(app):
        names = lambda: [g["name"] for g in fake_grocy.product_groups]  # noqa: E731
        assert _wait_for(lambda: "Bakery/Bread & Rolls" in names())
        assert _wait_for(lambda: set(default_group_names()) <= set(names()))
        # pre-existing groups are untouched
        assert "Dairy/Butter" in names()
