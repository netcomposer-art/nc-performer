#!/bin/sh
# Cloudflare injects an ephemeral CA at runtime when HTTPS egress is intercepted.
# It is not in the image. Append it to the system bundle before Python starts.
# NC_SYSTEM_CA, NC_CLOUDFLARE_CA, and NC_CA_BUNDLE override the paths in tests.
set -eu
sys_ca="${NC_SYSTEM_CA:-/etc/ssl/certs/ca-certificates.crt}"
cf_ca="${NC_CLOUDFLARE_CA:-/etc/cloudflare/certs/cloudflare-containers-ca.crt}"
# /home/nc is writable by the runtime user. /tmp is not, in this image.
bundle="${NC_CA_BUNDLE:-/home/nc/nc-ca-bundle.crt}"
if [ -r "$sys_ca" ]; then
  cat "$sys_ca" > "$bundle"
else
  : > "$bundle"
fi
if [ -r "$cf_ca" ]; then
  cat "$cf_ca" >> "$bundle"
fi
if [ -s "$bundle" ]; then
  export SSL_CERT_FILE="$bundle"
  export REQUESTS_CA_BUNDLE="$bundle"
  export CURL_CA_BUNDLE="$bundle"
fi
exec "$@"
