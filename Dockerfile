FROM golang:1.27.3-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go test -mod=readonly ./... && CGO_ENABLED=0 go build -mod=readonly -trimpath -o /driver .
FROM docker.io/litestream/litestream:0.5.18@sha256:513bff62a88fc0c86c73d27333c41c54c89fe890a7c32a551600adff9309b11b AS litestream
FROM python:3.15.0-alpine3.24@sha256:2699b90f4d562a22e2dd076c82f964032f64814204bff36d271a54ba4cc2c7e2
RUN apk add --no-cache util-linux
COPY --from=build /driver /usr/local/bin/sqlite-csi
COPY --from=litestream /usr/local/bin/litestream /usr/local/bin/litestream
ENTRYPOINT ["/usr/local/bin/sqlite-csi"]
