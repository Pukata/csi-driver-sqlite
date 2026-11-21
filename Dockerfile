FROM golang:1.27.3-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
ARG VERSION=0.2.0-dev
RUN go test -mod=readonly ./... && CGO_ENABLED=0 go build -mod=readonly -trimpath -ldflags "-X main.version=${VERSION}" -o /driver .
FROM docker.io/litestream/litestream:0.5.18@sha256:513bff62a88fc0c86c73d27333c41c54c89fe890a7c32a551600adff9309b11b AS litestream
FROM python:3.15.2-alpine3.24@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd
RUN apk add --no-cache util-linux
COPY --from=build /driver /usr/local/bin/sqlite-csi
COPY --from=litestream /usr/local/bin/litestream /usr/local/bin/litestream
ENTRYPOINT ["/usr/local/bin/sqlite-csi"]
