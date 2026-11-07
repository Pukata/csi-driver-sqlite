package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

// NodeMapping binds the Kubernetes object to the identity understood by the
// independent fencing authority. A reused node name is never sufficient.
type NodeMapping struct {
	NodeUID   string `json:"nodeUID"`
	MachineID string `json:"machineID"`
}

type FencingConfig struct {
	Endpoint          string                 `json:"endpoint"`
	CAFile            string                 `json:"caFile"`
	TokenFile         string                 `json:"tokenFile"`
	ManualPowerOnOnly bool                   `json:"manualPowerOnOnly"`
	GraceSeconds      int                    `json:"graceSeconds"`
	Nodes             map[string]NodeMapping `json:"nodes"`
}

type HTTPFencer struct {
	config FencingConfig
	client *http.Client
	now    func() time.Time
}

type fenceRequest struct {
	NodeName    string `json:"nodeName"`
	NodeUID     string `json:"nodeUID"`
	MachineID   string `json:"machineID"`
	OperationID string `json:"operationID"`
	Challenge   string `json:"challenge"`
}

type fenceResponse struct {
	fenceRequest
	State            string    `json:"state"`
	RestartInhibited bool      `json:"restartInhibited"`
	ObservedAt       time.Time `json:"observedAt"`
}

func loadFencer(path string) (*HTTPFencer, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var config FencingConfig
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err = decoder.Decode(&config); err != nil {
		return nil, err
	}
	if err = decoder.Decode(&struct{}{}); err != io.EOF {
		return nil, fmt.Errorf("fencing configuration must contain one JSON object")
	}
	parsed, err := url.Parse(config.Endpoint)
	if err != nil || parsed.Scheme != "https" || parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, fmt.Errorf("fencing endpoint must be a credential-free HTTPS URL")
	}
	if !config.ManualPowerOnOnly || config.GraceSeconds < 1 || config.TokenFile == "" || len(config.Nodes) == 0 {
		return nil, fmt.Errorf("explicit node mappings, grace period and manual-power-on guarantee required")
	}
	seen := map[string]bool{}
	for name, mapping := range config.Nodes {
		if name == "" || mapping.NodeUID == "" || mapping.MachineID == "" || seen[mapping.MachineID] {
			return nil, fmt.Errorf("invalid or duplicate fencing identity mapping")
		}
		seen[mapping.MachineID] = true
	}
	roots, err := x509.SystemCertPool()
	if err != nil {
		roots = x509.NewCertPool()
	}
	if config.CAFile != "" {
		ca, err := os.ReadFile(config.CAFile)
		if err != nil {
			return nil, err
		}
		if !roots.AppendCertsFromPEM(ca) {
			return nil, fmt.Errorf("invalid fencing CA")
		}
	}
	return &HTTPFencer{config: config, now: time.Now, client: &http.Client{
		Timeout:       60 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return fmt.Errorf("fencing redirects are forbidden") },
		Transport:     &http.Transport{TLSClientConfig: &tls.Config{RootCAs: roots, MinVersion: tls.VersionTLS12}},
	}}, nil
}

// PowerOff accepts only fresh, challenge-bound confirmation of physical stop
// and inhibited restart. A transport timeout, cordon or stale Lease is no proof.
func (f *HTTPFencer) PowerOff(ctx context.Context, node FenceNode, operation string) (PowerOffProof, error) {
	mapping, ok := f.config.Nodes[node.Name]
	if !ok || mapping.NodeUID != node.UID || mapping.MachineID == "" || operation == "" {
		return PowerOffProof{}, fmt.Errorf("node identity is not mapped for fencing")
	}
	nonce := make([]byte, 32)
	if _, err := rand.Read(nonce); err != nil {
		return PowerOffProof{}, err
	}
	request := fenceRequest{NodeName: node.Name, NodeUID: node.UID, MachineID: mapping.MachineID, OperationID: operation, Challenge: hex.EncodeToString(nonce)}
	body, err := json.Marshal(request)
	if err != nil {
		return PowerOffProof{}, err
	}
	token, err := os.ReadFile(f.config.TokenFile)
	if err != nil {
		return PowerOffProof{}, err
	}
	value := strings.TrimSpace(string(token))
	if value == "" || strings.ContainsAny(value, "\r\n") {
		return PowerOffProof{}, fmt.Errorf("invalid fencing token")
	}
	started := f.now()
	req, err := http.NewRequestWithContext(ctx, "POST", strings.TrimRight(f.config.Endpoint, "/")+"/v1/fence", bytes.NewReader(body))
	if err != nil {
		return PowerOffProof{}, err
	}
	req.Header.Set("Authorization", "Bearer "+value)
	req.Header.Set("Content-Type", "application/json")
	response, err := f.client.Do(req)
	if err != nil {
		return PowerOffProof{}, fmt.Errorf("fencing request failed: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return PowerOffProof{}, fmt.Errorf("fencing API HTTP %d", response.StatusCode)
	}
	var proof fenceResponse
	decoder := json.NewDecoder(io.LimitReader(response.Body, 16384))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&proof); err != nil {
		return PowerOffProof{}, fmt.Errorf("invalid fencing response: %w", err)
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return PowerOffProof{}, fmt.Errorf("fencing response must contain one JSON object")
	}
	now := f.now()
	if proof.fenceRequest != request || proof.State != "off" || !proof.RestartInhibited || proof.ObservedAt.IsZero() ||
		proof.ObservedAt.Before(started.Add(-5*time.Second)) || proof.ObservedAt.After(now.Add(5*time.Second)) || now.Sub(proof.ObservedAt) > time.Minute {
		return PowerOffProof{}, fmt.Errorf("fencing authority did not provide matching fresh power-off confirmation")
	}
	return PowerOffProof{NodeUID: node.UID, MachineID: mapping.MachineID, OperationID: operation, ObservedAt: proof.ObservedAt}, nil
}
