.PHONY: fmt verify test build chart image e2e integration-test benchmark nas-image nas-test maintenance-test mutation-test smb-protocol-image smb-protocol-test

IMAGE ?= sqlite-csi:e2e
BASELINE_IMAGE ?= sqlite-csi:baseline

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
	CGO_ENABLED=0 go build -trimpath -o bin/sqlite-csi .

chart:
	helm lint --strict charts/sqlite-csi
	@if output=$$(helm template sqlite-csi charts/sqlite-csi --set failover.enabled=true 2>&1); then \
		echo "obsolete failover configuration was accepted"; exit 1; \
	else \
		echo "$$output" | grep -q 'automatic failover was removed'; \
	fi

image:
	docker build -t $(IMAGE) .

e2e:
	python3 test/e2e/run.py --image $(IMAGE)

mutation-test:
	python3 test/ownership/mutations.py

maintenance-test:
	test -n "$(BASELINE_REPO)"
	python3 test/maintenance/run.py --baseline-repo "$(BASELINE_REPO)" --baseline-image "$(BASELINE_IMAGE)" --image "$(IMAGE)"

integration-test:
	python3 -m unittest discover -s tools -p 'test_*.py' -v
	python3 -m unittest discover -s test/nas -p 'test_*.py' -v

nas-image:
	docker build -f test/nas/Dockerfile -t sqlite-csi-nas:e2e .

nas-test:
	python3 test/nas/run.py --image $(IMAGE)

benchmark:
	mkdir -p .e2e/artifacts .e2e/bench-work
	chmod 0777 .e2e/bench-work
	docker run --rm --network=none --read-only --user=1000:1000 --memory=512m --cpus=2 -e TMPDIR=/work -e DRIVER_REVISION=$$(git rev-parse HEAD) -v "$$(pwd)/.e2e/bench-work:/work" -v "$$(pwd):/src:ro" --entrypoint python3 $(IMAGE) /src/test/bench/run.py > .e2e/artifacts/benchmark.json

smb-protocol-image:
	docker build -f test/protocols/smb/Dockerfile -t sqlite-smb-protocol:local .

smb-protocol-test:
	python3 -m unittest discover -s test/protocols/smb -p 'test_*.py' -v
	python3 test/protocols/smb/run.py
