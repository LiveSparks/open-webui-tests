"""Journey: groups nested inside groups, as the admin panel and the member's own lists show them.

An admin puts a group under another when creating it or later by editing it, takes it back to
the top level with a null parent and moves it to another parent. An unknown parent is a 404, an
empty-string parent, a group as its own parent and a move under its own descendant are refused,
and a refused edit leaves the whole tree as it was. Deleting a middle group lifts its subgroups
into its parent, takes its grants and direct memberships with it and leaves the grandchild's
members with what the top group gave and without what only the deleted group gave. The members
view of a group lists the people added to it and the people in its subgroups, each with the
subgroups they come through, and a member's own group list marks every group as direct or
inherited. A group whose sharing is set to Members offers itself to a subgroup's members.

Discriminates: in a backend copy, dropping the cycle check from `validate_parent` turns the
refused-move tests red (a group becomes its own parent and its own descendant's child), making
`delete_group_by_id` leave the subgroups where they were turns both delete tests red (the leaf
keeps a parent that no longer exists), and making `user_group_memberships` ignore
`include_inherited` turns the member list, the Members sharing test and the delete test red (only
direct groups come back).
"""

from __future__ import annotations

import uuid

import pytest

from harness.access import grant, make_group
from harness.group_tree import Chain, build_chain, group_of, move_group
from harness.knowledge_bases import knowledge_base

pytestmark = [pytest.mark.journey, pytest.mark.api, pytest.mark.requires_source]


@pytest.fixture
def chain(admin, make_user):
    tree = build_chain(admin, make_user)
    yield tree
    with admin.client() as client:
        for group_id in (tree.leaf, tree.middle, tree.top):
            client.delete(f"/api/v1/groups/id/{group_id}/delete")


def _parent_of(admin, group_id: str):
    return group_of(admin, group_id)["parent_group_id"]


def _tree(admin, chain: Chain) -> dict:
    return {name: _parent_of(admin, getattr(chain, name)) for name in ("top", "middle", "leaf")}


def test_a_group_created_with_a_parent_shows_it(admin, chain):
    assert _tree(admin, chain) == {"top": None, "middle": chain.top, "leaf": chain.middle}


def test_a_group_moves_to_another_parent_and_to_the_top_level(admin, chain):
    other = make_group(admin, [])

    moved = move_group(admin, chain.leaf, other)
    assert moved.status_code == 200, moved.text
    assert moved.json()["parent_group_id"] == other
    assert _parent_of(admin, chain.leaf) == other

    top_level = move_group(admin, chain.leaf, None)
    assert top_level.status_code == 200, top_level.text
    assert _parent_of(admin, chain.leaf) is None
    with admin.client() as client:
        client.delete(f"/api/v1/groups/id/{other}/delete")


def test_an_edit_without_a_parent_keeps_the_parent(admin, chain):
    with admin.client() as client:
        renamed = client.post(
            f"/api/v1/groups/id/{chain.leaf}/update",
            json={"name": "renamed leaf", "description": "no parent in the form"},
        )
    assert renamed.status_code == 200, renamed.text
    assert _parent_of(admin, chain.leaf) == chain.middle


def test_an_unknown_parent_is_refused_on_create_and_on_update(admin, chain):
    with admin.client() as client:
        created = client.post(
            "/api/v1/groups/create",
            json={"name": "orphan", "description": "", "parent_group_id": "no-such-group"},
        )
    moved = move_group(admin, chain.leaf, "no-such-group")

    assert created.status_code == 404, created.text
    assert moved.status_code == 404, moved.text
    assert _parent_of(admin, chain.leaf) == chain.middle


def test_an_empty_parent_is_refused_on_create_and_on_update(admin, chain):
    with admin.client() as client:
        created = client.post(
            "/api/v1/groups/create",
            json={"name": "blank parent", "description": "", "parent_group_id": ""},
        )
    moved = move_group(admin, chain.leaf, "")

    assert created.status_code == 400, created.text
    assert moved.status_code == 400, moved.text
    assert _parent_of(admin, chain.leaf) == chain.middle


def test_a_group_cannot_be_its_own_parent(admin, chain):
    refused = move_group(admin, chain.middle, chain.middle)

    assert refused.status_code == 400, (
        f"a group became its own parent: HTTP {refused.status_code} {refused.text}"
    )
    assert _tree(admin, chain) == {"top": None, "middle": chain.top, "leaf": chain.middle}


@pytest.mark.parametrize("descendant", ["middle", "leaf"])
def test_a_group_cannot_move_under_its_own_descendant(admin, chain, descendant):
    refused = move_group(admin, chain.top, getattr(chain, descendant))

    assert refused.status_code == 400, (
        f"the top group moved under its own {descendant}: HTTP {refused.status_code} {refused.text}"
    )
    assert _tree(admin, chain) == {"top": None, "middle": chain.top, "leaf": chain.middle}


def test_deleting_the_middle_group_lifts_the_leaf_and_takes_its_grants(admin, chain):
    on_top = grant("group", chain.top, "read")
    on_middle = grant("group", chain.middle, "read")
    tag = uuid.uuid4().hex[:8]
    with (
        admin.client() as client,
        knowledge_base(client, f"Top only {tag}", [on_top]) as top_knowledge,
        knowledge_base(client, f"Middle only {tag}", [on_middle]) as middle_knowledge,
    ):

        def read_status(actor, knowledge_id) -> int:
            with actor.client() as own:
                return own.get(f"/api/v1/knowledge/{knowledge_id}").status_code

        def group_ids(actor) -> list[str]:
            stored = client.get(f"/api/v1/users/{actor.id}").json()
            return [entry["id"] for entry in stored["groups"]]

        assert read_status(chain.leaf_member, middle_knowledge) == 200
        assert read_status(chain.leaf_member, top_knowledge) == 200
        assert read_status(chain.middle_member, middle_knowledge) == 200
        assert chain.middle in group_ids(chain.middle_member)

        assert client.delete(f"/api/v1/groups/id/{chain.middle}/delete").json() is True

        assert _parent_of(admin, chain.leaf) == chain.top, (
            "the leaf group was not moved up into the deleted group's parent"
        )
        assert read_status(chain.leaf_member, top_knowledge) == 200, (
            "the leaf lost the top group's grant"
        )
        assert read_status(chain.leaf_member, middle_knowledge) != 200, (
            "the deleted group's grant still works"
        )
        assert read_status(chain.middle_member, middle_knowledge) != 200
        assert read_status(chain.middle_member, top_knowledge) != 200
        remaining = client.get(f"/api/v1/knowledge/{middle_knowledge}").json()["access_grants"]
        assert chain.middle not in {entry["principal_id"] for entry in remaining}
        assert group_ids(chain.middle_member) == []


def test_deleting_the_top_group_makes_the_middle_group_top_level(admin, chain):
    with admin.client() as client:
        assert client.delete(f"/api/v1/groups/id/{chain.top}/delete").json() is True

    assert _parent_of(admin, chain.middle) is None
    assert _parent_of(admin, chain.leaf) == chain.middle
    with chain.leaf_member.client() as client:
        assert {entry["id"] for entry in client.get("/api/v1/users/groups").json()} == {chain.leaf}


def test_the_members_view_lists_direct_and_inherited_members(admin, chain):
    def members(group_id: str, membership: str) -> dict:
        with admin.client() as client:
            listed = client.get(
                f"/api/v1/groups/id/{group_id}/members", params={"membership": membership}
            )
        assert listed.status_code == 200, listed.text
        return listed.json()

    effective = members(chain.top, "effective")
    by_id = {item["id"]: item for item in effective["items"]}
    assert set(by_id) == {chain.top_member.id, chain.middle_member.id, chain.leaf_member.id}
    assert effective["counts"] == {"direct": 1, "effective": 3, "inherited": 2}
    assert effective["total"] == 3
    assert by_id[chain.top_member.id]["membership_type"] == "direct"
    assert by_id[chain.top_member.id]["via_group_ids"] == []
    assert by_id[chain.middle_member.id]["membership_type"] == "inherited"
    assert by_id[chain.middle_member.id]["via_group_ids"] == [chain.middle]
    assert by_id[chain.leaf_member.id]["via_group_ids"] == [chain.leaf]

    direct = members(chain.top, "direct")
    assert [item["id"] for item in direct["items"]] == [chain.top_member.id]
    inherited = members(chain.top, "inherited")
    assert {item["id"] for item in inherited["items"]} == {
        chain.middle_member.id,
        chain.leaf_member.id,
    }
    assert members(chain.leaf, "inherited")["items"] == []
    assert members(chain.middle, "effective")["counts"] == {
        "direct": 1,
        "effective": 2,
        "inherited": 1,
    }
    assert group_of(admin, chain.top)["member_count"] == 1, "the group list counts direct members"


def test_a_member_in_two_branches_is_listed_once_with_both_routes(admin, make_user, chain):
    sibling = make_group(admin, [chain.leaf_member], parent_id=chain.top)
    with admin.client() as client:
        listed = client.get(
            f"/api/v1/groups/id/{chain.top}/members", params={"membership": "inherited"}
        )
        client.delete(f"/api/v1/groups/id/{sibling}/delete")

    leaf_rows = [item for item in listed.json()["items"] if item["id"] == chain.leaf_member.id]
    assert len(leaf_rows) == 1
    assert leaf_rows[0]["via_group_ids"] == sorted([chain.leaf, sibling])


def test_the_members_view_is_for_the_admin_and_knows_its_groups(admin, chain):
    with chain.top_member.client() as client:
        refused = client.get(f"/api/v1/groups/id/{chain.top}/members")
    with admin.client() as client:
        missing = client.get("/api/v1/groups/id/no-such-group/members")

    assert refused.status_code in (401, 403)
    assert missing.status_code == 404


def test_a_members_group_list_marks_each_group_direct_or_inherited(admin, chain):
    with chain.leaf_member.client() as client:
        plain = client.get("/api/v1/users/groups").json()
        marked = client.get("/api/v1/users/groups", params={"include_inherited": "true"}).json()
        refused = client.get(f"/api/v1/users/{chain.leaf_member.id}/groups")
    with admin.client() as client:
        by_admin = client.get(
            f"/api/v1/users/{chain.leaf_member.id}/groups", params={"include_inherited": "true"}
        ).json()
        by_admin_plain = client.get(f"/api/v1/users/{chain.leaf_member.id}/groups").json()

    expected = {chain.leaf: "direct", chain.middle: "inherited", chain.top: "inherited"}
    assert {entry["id"] for entry in plain} == {chain.leaf}
    assert {entry["id"] for entry in by_admin_plain} == {chain.leaf}
    assert {entry["id"]: entry["membership_type"] for entry in marked} == expected
    assert {entry["id"]: entry["membership_type"] for entry in by_admin} == expected
    assert refused.status_code in (401, 403)


def test_the_top_members_list_stays_direct_only(chain):
    with chain.top_member.client() as client:
        marked = client.get("/api/v1/users/groups", params={"include_inherited": "true"}).json()

    assert [(entry["id"], entry["membership_type"]) for entry in marked] == [(chain.top, "direct")]


def test_a_members_only_group_is_offered_to_a_subgroups_members(admin, make_user, chain):
    outsider = make_user()
    with admin.client() as client:
        updated = client.post(
            f"/api/v1/groups/id/{chain.top}/update",
            json={
                "name": group_of(admin, chain.top)["name"],
                "description": "",
                "data": {"config": {"share": "members"}},
            },
        )
    assert updated.status_code == 200, updated.text

    def offered(actor) -> set[str]:
        with actor.client() as client:
            return {
                entry["id"]
                for entry in client.get("/api/v1/groups/", params={"share": "true"}).json()
            }

    assert chain.top in offered(chain.top_member)
    assert chain.top in offered(chain.leaf_member), "a subgroup's member cannot share to the parent"
    assert chain.top not in offered(outsider)
