"""
Backward compatibility across the UnixNodes → TGD Bot rebrand.

Renaming a brand touches two kinds of identifier, and only one of them is safe
to change freely:

* **Display strings** — bot name, banner, docs.  Free to change.
* **Runtime identifiers** — Docker container labels and volume names.  Docker
  labels are immutable after creation and **volumes cannot be renamed at all**,
  so a naive rename orphans every existing customer volume and makes the bot
  report its own containers as unmanaged orphans.

The rebrand therefore keeps matching the old identifiers.  These tests pin that:
a VPS created before the rename must still be found, listed, mounted and
deletable, with no data copied or moved.
"""

from __future__ import annotations

import pytest

from tgdbot import config
from tgdbot.docker_backend import containers as ctr
from tgdbot.docker_backend import volumes


# ── the fake must model the real API ────────────────────────────────────────

def test_label_list_is_ored_not_anded(docker_client):
    """
    Multiple values for the SAME Docker filter key are OR'd, not AND'd.

    ``filters={"label": ["a=1", "b=2"]}`` matches a container carrying *either*
    label.  The fake previously AND'd them, which silently hid the rebrand
    breakage in both directions — so this is pinned explicitly.
    """
    docker_client.containers.create(
        "ubuntu:24.04", name="only-a",
        labels={"tgdbot.managed": "true"})
    docker_client.containers.create(
        "ubuntu:24.04", name="only-b",
        labels={"unixnodes.managed": "true"})

    either = docker_client.containers.list(
        all=True, filters={"label": ["tgdbot.managed=true", "unixnodes.managed=true"]})
    assert {c.name for c in either} == {"only-a", "only-b"}

    # A label no container carries still matches nothing.
    none = docker_client.containers.list(
        all=True, filters={"label": ["nope.managed=true"]})
    assert none == []


# ── containers created before the rebrand ───────────────────────────────────

def make_container(client, name, *, legacy):
    managed = config.LEGACY_LABEL_MANAGED if legacy else config.LABEL_MANAGED
    container = config.LEGACY_LABEL_CONTAINER if legacy else config.LABEL_CONTAINER
    owner = config.LEGACY_LABEL_OWNER if legacy else config.LABEL_OWNER
    return client.containers.create(
        "ubuntu:24.04", name=name,
        labels={managed: "true", container: name, owner: "111"})


def test_legacy_labelled_container_is_still_managed(docker_client):
    make_container(docker_client, "old-vps-1", legacy=True)
    found = ctr.list_managed(docker_client)
    assert [c["name"] for c in found] == ["old-vps-1"]


def test_new_and_legacy_containers_are_found_together(docker_client):
    make_container(docker_client, "old-vps-1", legacy=True)
    make_container(docker_client, "new-vps-2", legacy=False)
    found = {c["name"]: c for c in ctr.list_managed(docker_client)}
    assert set(found) == {"old-vps-1", "new-vps-2"}
    # Labels resolve through the alias table, so owner/container_name are right.
    assert found["old-vps-1"]["owner"] == "111"
    assert found["old-vps-1"]["container_name"] == "old-vps-1"
    assert found["new-vps-2"]["container_name"] == "new-vps-2"


def test_state_counts_include_legacy_containers(docker_client):
    make_container(docker_client, "old-vps-1", legacy=True)
    make_container(docker_client, "new-vps-2", legacy=False)
    counts = ctr.count_by_state(docker_client)
    assert counts["total"] == 2, "a pre-rebrand container was not counted"


def test_unmanaged_container_is_never_claimed(docker_client):
    """The fallback must not become a wildcard that adopts foreign containers."""
    docker_client.containers.create("ubuntu:24.04", name="someone-elses",
                                    labels={"app": "nginx"})
    assert ctr.list_managed(docker_client) == []


def test_label_value_prefers_current_then_legacy():
    assert config.label_value({config.LABEL_OWNER: "new"}, config.LABEL_OWNER) == "new"
    assert config.label_value({config.LEGACY_LABEL_OWNER: "old"}, config.LABEL_OWNER) == "old"
    # Current wins when both are somehow present.
    both = {config.LABEL_OWNER: "new", config.LEGACY_LABEL_OWNER: "old"}
    assert config.label_value(both, config.LABEL_OWNER) == "new"


def test_label_value_default_when_absent():
    assert config.label_value({}, config.LABEL_OWNER, "fallback") == "fallback"
    assert config.label_value(None, config.LABEL_OWNER, "fallback") == "fallback"


def test_legacy_and_current_labels_are_distinct():
    """Sanity: the alias table would be pointless if these were the same."""
    assert config.LABEL_MANAGED != config.LEGACY_LABEL_MANAGED
    assert config.DOCKER_VOLUME_PREFIX != config.LEGACY_VOLUME_PREFIX
    assert config.LEGACY_LABEL_MANAGED == "unixnodes.managed"
    assert config.LEGACY_VOLUME_PREFIX == "unixnodes_"


# ── volumes created before the rebrand ──────────────────────────────────────

def test_new_vps_gets_the_current_prefix(docker_client):
    volumes.create(docker_client, "fresh-vps")
    assert volumes.resolve_volume_name(docker_client, "fresh-vps") == "tgdbot_fresh-vps"


def test_legacy_volume_is_found_not_orphaned(docker_client):
    """
    The core guarantee: a volume created before the rename is still resolved,
    so the customer's data is not silently abandoned.
    """
    docker_client.volumes.create(name="unixnodes_legacy-vps")
    assert volumes.exists(docker_client, "legacy-vps") is True
    assert volumes.resolve_volume_name(docker_client, "legacy-vps") == "unixnodes_legacy-vps"


def test_legacy_volume_mountpoint_is_readable(docker_client):
    docker_client.volumes.create(name="unixnodes_legacy-vps")
    assert volumes.mountpoint(docker_client, "legacy-vps")


def test_database_recorded_name_wins(docker_client):
    """The DB column is authoritative — it beats both computed prefixes."""
    docker_client.volumes.create(name="something_else_entirely")
    resolved = volumes.resolve_volume_name(
        docker_client, "vps", stored="something_else_entirely")
    assert resolved == "something_else_entirely"


def test_current_prefix_wins_over_legacy(docker_client):
    docker_client.volumes.create(name="unixnodes_vps")
    docker_client.volumes.create(name="tgdbot_vps")
    assert volumes.resolve_volume_name(docker_client, "vps") == "tgdbot_vps"


def test_missing_volume_resolves_to_the_current_prefix(docker_client):
    """Nothing exists yet, so the caller creates it under the new name."""
    assert volumes.resolve_volume_name(docker_client, "brand-new") == "tgdbot_brand-new"


def test_list_managed_invents_both_prefixes(docker_client):
    docker_client.volumes.create(name="unixnodes_old-vps")
    docker_client.volumes.create(name="tgdbot_new-vps")
    docker_client.volumes.create(name="not_ours_at_all")

    found = {v["name"]: v["container"] for v in volumes.list_managed(docker_client)}
    assert found == {"unixnodes_old-vps": "old-vps", "tgdbot_new-vps": "new-vps"}


def test_removing_a_legacy_volume_targets_the_right_one(docker_client):
    """Deletion must hit the real volume, not a same-named volume that never existed."""
    docker_client.volumes.create(name="unixnodes_doomed")
    assert volumes.remove(docker_client, "doomed") is True
    assert volumes.exists(docker_client, "doomed") is False


def test_removing_a_missing_volume_fails_cleanly(docker_client):
    assert volumes.remove(docker_client, "never-existed") is False


def test_mounts_use_the_resolved_volume(docker_client):
    docker_client.volumes.create(name="unixnodes_mounted")
    binds = volumes.mounts_for("mounted", stored="unixnodes_mounted")
    assert "unixnodes_mounted" in binds
    assert binds["unixnodes_mounted"]["bind"] == volumes.MOUNT_POINT
    assert binds["unixnodes_mounted"]["mode"] == "rw"
