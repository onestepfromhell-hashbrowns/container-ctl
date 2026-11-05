from container_ctl.port_conflict import declared_binds, find_port_conflicts

PS = [{"Names": ["svc-prod-x-0.3.2"], "Ports": [{"host_ip": "127.0.0.2", "host_port": 9443, "container_port": 9443, "protocol": "tcp", "range": 1}]},
      {"Names": ["other"], "Ports": [{"host_ip": "127.0.0.2", "host_port": 8082, "protocol": "tcp"}, {"container_port": 80, "protocol": "tcp"}]}]


def test_declared_binds_skips_random_and_ranges():
    assert declared_binds(["127.0.0.2:9443:9443", "8000:80/udp", "80", "127.0.0.1:1000-1005:1000-1005"]) == [("127.0.0.2", 9443, "tcp"), ("", 8000, "udp")]


def test_old_version_instance_conflicts_but_own_name_does_not():
    ports = ["127.0.0.2:9443:9443"]
    assert find_port_conflicts(ports, {"svc-prod-x-0.3.5"}, PS) == ["127.0.0.2:9443/tcp 已被運行中的 svc-prod-x-0.3.2 佔用"]
    assert find_port_conflicts(ports, {"svc-prod-x-0.3.2"}, PS) == []


def test_different_ip_and_proto_do_not_conflict_wildcard_does():
    assert find_port_conflicts(["127.0.0.3:9443:9443"], set(), PS) == []
    assert find_port_conflicts(["127.0.0.2:9443:9443/udp"], set(), PS) == []
    assert len(find_port_conflicts(["9443:9443"], set(), PS)) == 1


def test_needs_stop_first_only_when_names_differ_and_fixed_port():
    from container_ctl.port_conflict import needs_stop_first
    cfg = {"ports": ["127.0.0.2:9443:9443"]}
    assert needs_stop_first("svc-x-0.3.2", "svc-x", cfg)
    assert not needs_stop_first("svc-x", "svc-x", cfg)
    assert not needs_stop_first(None, "svc-x", cfg)
    assert not needs_stop_first("svc-x-0.3.2", "svc-x", {"ports": ["8000"]})


def test_precheck_excludes_old_unit_that_stop_first_will_stop():
    from container_ctl.port_conflict import precheck_own_names
    cfg = {"ports": ["127.0.0.2:9443:9443"]}
    own, notice = precheck_own_names("svc-prod-x-0.3.2", "svc-prod-x-0.3.5", cfg)
    assert own == {"svc-prod-x-0.3.5", "svc-prod-x-0.3.2"} and "先停舊單元 svc-prod-x-0.3.2" in notice
    assert find_port_conflicts(cfg["ports"], own, PS) == []  # 換版不再被預檢擋下
    # 其他服務佔埠、或舊名對不上（不是本服務登記的舊單元）仍是衝突
    own, notice = precheck_own_names("svc-other-1.0", "svc-prod-x-0.3.5", cfg)
    assert notice and len(find_port_conflicts(cfg["ports"], {"svc-prod-x-0.3.5"}, PS)) == 1
    # 無固定埠或同名單元：不放寬
    assert precheck_own_names("svc-prod-x-0.3.5", "svc-prod-x-0.3.5", cfg) == ({"svc-prod-x-0.3.5"}, None)
    assert precheck_own_names("svc-a", "svc-b", {"ports": []}) == ({"svc-b"}, None)
