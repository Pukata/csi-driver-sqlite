FROM debian:13-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
RUN apt-get update && apt-get install -y --no-install-recommends nfs-ganesha-vfs rpcbind && rm -rf /var/lib/apt/lists/* && mkdir -p /run/ganesha /export /var/lib/nfs/ganesha
COPY test/nas/ganesha.conf /etc/ganesha/ganesha.conf
ENTRYPOINT ["/bin/sh", "-ec", "rpcbind -w; exec ganesha.nfsd -F -L STDOUT -f /etc/ganesha/ganesha.conf"]
