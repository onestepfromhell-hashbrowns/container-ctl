# Broker configuration location

`net_broker.py` is tool code. Its configuration is site data and lives outside the repository:

| Kind | Location |
|---|---|
| Tool code | the installed package directory (`@CODE_DIR@` in the unit template) |
| Configuration | `/etc/container-ctl/broker-config.json` (override with `--config`) |

See `broker-netattach.service.template` for the unit.
