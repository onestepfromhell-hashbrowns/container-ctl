#!/bin/sh
# Install the read-only Extension payload into Antigravity's own plugin
# registry.  `agy plugin list` is the corresponding non-model discovery probe.
set -eu

plugin_root=/run/ext/antigravity-plugins/ext-cleanroom-sentinel
[ -f "$plugin_root/plugin.json" ]
agy plugin validate "$plugin_root"
agy plugin install "$plugin_root"
