from app.categories import UNCATEGORIZED, build_category_tree, split_group_name


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
