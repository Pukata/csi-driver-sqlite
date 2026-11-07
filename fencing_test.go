package main

import (
	"context"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"
)

func fencingFixture(t *testing.T, mutate func(*fenceResponse), status int) (*HTTPFencer, *atomic.Int32) {
	t.Helper()
	calls := &atomic.Int32{}
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Method != "POST" || r.URL.Path != "/v1/fence" || r.Header.Get("Authorization") != "Bearer test-token" {
			t.Error("incorrect fencing request")
		}
		if status != 200 {
			w.WriteHeader(status)
			return
		}
		var request fenceRequest
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Error(err)
		}
		if len(request.Challenge) != 64 {
			t.Error("missing fresh challenge")
		}
		response := fenceResponse{fenceRequest: request, State: "off", RestartInhibited: true, ObservedAt: time.Now()}
		if mutate != nil {
			mutate(&response)
		}
		json.NewEncoder(w).Encode(response)
	}))
	t.Cleanup(server.Close)
	dir := t.TempDir()
	certificate, err := x509.ParseCertificate(server.TLS.Certificates[0].Certificate[0])
	if err != nil {
		t.Fatal(err)
	}
	ca := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: certificate.Raw})
	caPath, tokenPath, configPath := filepath.Join(dir, "ca.pem"), filepath.Join(dir, "token"), filepath.Join(dir, "config.json")
	if err := os.WriteFile(caPath, ca, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(tokenPath, []byte("test-token"), 0600); err != nil {
		t.Fatal(err)
	}
	config := FencingConfig{Endpoint: server.URL, CAFile: caPath, TokenFile: tokenPath, ManualPowerOnOnly: true, GraceSeconds: 1,
		Nodes: map[string]NodeMapping{"worker-a": {NodeUID: "uid-a", MachineID: "machine-a"}}}
	data, _ := json.Marshal(config)
	if err := os.WriteFile(configPath, data, 0600); err != nil {
		t.Fatal(err)
	}
	fencer, err := loadFencer(configPath)
	if err != nil {
		t.Fatal(err)
	}
	return fencer, calls
}

func TestFencingRequiresFreshIdentityBoundPowerOff(t *testing.T) {
	f, calls := fencingFixture(t, nil, 200)
	proof, err := f.PowerOff(context.Background(), FenceNode{Name: "worker-a", UID: "uid-a"}, "operation-a")
	if err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 1 || proof.NodeUID != "uid-a" || proof.MachineID != "machine-a" || proof.OperationID != "operation-a" {
		t.Fatal(proof, calls.Load())
	}
}

func TestFencingRejectsUnsafeOrReplayedConfirmations(t *testing.T) {
	cases := map[string]func(*fenceResponse){
		"running":           func(p *fenceResponse) { p.State = "running" },
		"restart allowed":   func(p *fenceResponse) { p.RestartInhibited = false },
		"node replaced":     func(p *fenceResponse) { p.NodeUID = "different" },
		"machine replaced":  func(p *fenceResponse) { p.MachineID = "different" },
		"wrong operation":   func(p *fenceResponse) { p.OperationID = "different" },
		"replayed response": func(p *fenceResponse) { p.Challenge = "old-challenge" },
		"stale proof":       func(p *fenceResponse) { p.ObservedAt = time.Now().Add(-time.Minute) },
		"future proof":      func(p *fenceResponse) { p.ObservedAt = time.Now().Add(time.Minute) },
		"no time":           func(p *fenceResponse) { p.ObservedAt = time.Time{} },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			f, _ := fencingFixture(t, mutate, 200)
			if _, err := f.PowerOff(context.Background(), FenceNode{Name: "worker-a", UID: "uid-a"}, "operation-a"); err == nil {
				t.Fatal("unsafe confirmation accepted")
			}
		})
	}
}

func TestUnmappedFencingNeverCallsAuthority(t *testing.T) {
	for _, node := range []FenceNode{{Name: "worker-a", UID: "replacement"}, {Name: "worker-b", UID: "uid-a"}} {
		f, calls := fencingFixture(t, nil, 200)
		if _, err := f.PowerOff(context.Background(), node, "operation-a"); err == nil || calls.Load() != 0 {
			t.Fatal("unmapped node fenced")
		}
	}
}

func TestUnavailableFencingNeverProvesPowerOff(t *testing.T) {
	for _, status := range []int{401, 403, 503} {
		f, _ := fencingFixture(t, nil, status)
		if _, err := f.PowerOff(context.Background(), FenceNode{Name: "worker-a", UID: "uid-a"}, "operation-a"); err == nil {
			t.Fatal("failed authority accepted")
		}
	}
}

func TestFencingDoesNotUseMissingTokenOrUntrustedTLS(t *testing.T) {
	f, calls := fencingFixture(t, nil, 200)
	if err := os.Remove(f.config.TokenFile); err != nil {
		t.Fatal(err)
	}
	if _, err := f.PowerOff(context.Background(), FenceNode{Name: "worker-a", UID: "uid-a"}, "operation-a"); err == nil || calls.Load() != 0 {
		t.Fatal("missing token accepted")
	}
	f, _ = fencingFixture(t, nil, 200)
	f.client = &http.Client{Timeout: time.Second}
	if _, err := f.PowerOff(context.Background(), FenceNode{Name: "worker-a", UID: "uid-a"}, "operation-a"); err == nil {
		t.Fatal("untrusted TLS accepted")
	}
}
