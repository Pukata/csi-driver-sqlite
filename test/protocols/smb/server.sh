#!/bin/sh
set -eu
mkdir -p /run/samba
{ cat /run/password; cat /run/password; } | smbpasswd -s -a smbtest >/dev/null
exec smbd --foreground --no-process-group --debug-stdout
