.PHONY: fmt verify test build chart image e2e

IMAGE ?= sqlite-roam:e2e

fmt:
	gofmt -w *.go

verify:
	test -z "$$(gofmt -l *.go)"
	go vet ./...
	go mod verify

test:
	go test -race -shuffle=on -count=1 -timeout=2m -covermode=atomic -coverprofile=coverage.out ./...
	go tool cover -func=coverage.out

build:
	CGO_ENABLED=0 go build -trimpath -o bin/sqlite-roam .

chart:
	helm lint --strict charts/sqlite-roam
	helm lint --strict charts/sqlite-roam --set failover.enabled=true --set failover.configSecret=test-fencing

image:
	docker build -t $(IMAGE) .

e2e:
	python3 test/e2e/run.py --image $(IMAGE)
