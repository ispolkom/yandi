#!/bin/bash
cat > /usr/lib/firefox-esr/distribution/policies.json << 'POLICIES'
{
  "policies": {
    "Preferences": {
      "xpinstall.signatures.required": {
        "Value": false,
        "Status": "locked"
      }
    },
    "ExtensionSettings": {
      "council-bridge@yandi.local": {
        "installation_mode": "force_installed",
        "install_url": "file:///media/iam/DATASET/claude/yandi/pet/council_bridge.xpi"
      }
    }
  }
}
POLICIES
echo "OK: $(cat /usr/lib/firefox-esr/distribution/policies.json)"
