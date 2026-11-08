#!/bin/sh
set -eu
{ cat /run/smb-password; cat /run/smb-password; } | smbpasswd -s -a sqlite >/dev/null
exec smbd --foreground --no-process-group --debug-stdout
