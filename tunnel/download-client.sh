#!/usr/bin/env bash
# Pin and checksum-verify the complete OpenAI release, including cloudflared.
set -euo pipefail
umask 077
version=${TUNNEL_VERSION:-v0.0.16}
dest=${1:-"$HOME/.local/bin"}
case "$version" in
  v[0-9]*.[0-9]*.[0-9]*) ;;
  *) echo 'Unexpected release version' >&2; exit 1 ;;
esac
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || {
  echo 'This script currently supports Linux x86_64 only' >&2; exit 1;
}
asset="tunnel-client-$version-linux-amd64.zip"
base="https://github.com/openai/tunnel-client/releases/download/$version"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
curl -fsSL --retry 2 --max-time 120 "$base/$asset" -o "$work/$asset"
curl -fsSL --retry 2 --max-time 30 "$base/SHA256SUMS.txt" -o "$work/SHA256SUMS.txt"
(cd "$work" && grep -F "  $asset" SHA256SUMS.txt | sha256sum -c -)
for entry in tunnel-client cloudflared; do
  unzip -Z -1 "$work/$asset" | grep -Fxq "$entry" || {
    echo "Archive missing $entry" >&2; exit 1;
  }
done
mkdir -p "$work/extracted" "$dest"
unzip -jq "$work/$asset" tunnel-client cloudflared -d "$work/extracted"
for entry in tunnel-client cloudflared; do
  install -m 0755 "$work/extracted/$entry" "$dest/$entry"
done
echo "Installed complete $version release to $dest"
"$dest/tunnel-client" --version
