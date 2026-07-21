#!/usr/bin/env bash
set -euo pipefail
sudo useradd --system --home /var/lib/drive-to-flickr --shell /usr/sbin/nologin flickruploader || true
sudo mkdir -p /opt/drive-to-flickr /var/lib/drive-to-flickr/staging /etc/drive-to-flickr
sudo chown -R flickruploader:flickruploader /var/lib/drive-to-flickr /etc/drive-to-flickr
sudo chmod 750 /var/lib/drive-to-flickr /etc/drive-to-flickr
sudo cp deploy/drive-to-flickr.service /etc/systemd/system/drive-to-flickr.service
sudo systemctl daemon-reload
