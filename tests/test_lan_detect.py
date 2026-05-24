"""Unit tests for LAN IPv4 selection (Raspberry Pi / Linux friendly)."""

from rtt_alhuda.lan_detect import pick_preferred_lan_ipv4


def test_pick_prefers_rfc1918_over_public() -> None:
    assert (
        pick_preferred_lan_ipv4(["203.0.113.5", "192.168.1.40", "10.0.0.2"])
        == "192.168.1.40"
    )


def test_pick_prefers_private_over_link_local() -> None:
    assert (
        pick_preferred_lan_ipv4(["169.254.12.3", "192.168.4.2"]) == "192.168.4.2"
    )


def test_pick_skips_loopback() -> None:
    assert pick_preferred_lan_ipv4(["127.0.0.1", "127.0.1.1", "10.0.0.5"]) == "10.0.0.5"


def test_pick_none_when_only_loopback() -> None:
    assert pick_preferred_lan_ipv4(["127.0.0.1", "127.0.1.1"]) is None


def test_pick_first_non_link_local_if_no_private() -> None:
    assert pick_preferred_lan_ipv4(["203.0.113.2", "169.254.1.1"]) == "203.0.113.2"
