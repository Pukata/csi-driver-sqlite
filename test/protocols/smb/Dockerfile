FROM debian:13-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
RUN apt-get update && apt-get install -y --no-install-recommends samba smbclient python3 && rm -rf /var/lib/apt/lists/* && useradd --system --no-create-home smbtest && mkdir /export
COPY test/protocols/smb/server.conf /etc/samba/smb.conf
COPY test/protocols/smb/server.sh /server.sh
ENTRYPOINT ["sh", "/server.sh"]
